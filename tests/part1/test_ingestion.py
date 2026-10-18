"""Part 1 contract, detection, replay, evidence, transactions and integration."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import gaps, notifications
from app.ledger.canonical import LedgerError
from tests.api.conftest import ApiClient, owner_rows
from tests.part1.conftest import T0, headers
from tests.worker.conftest import Receiver, app_sessions_in_transaction


def sample(**changes: Any) -> dict[str, Any]:
    return {
        "system_id": "sys-42",
        "metric_name": "accuracy_drift",
        "metric_type": "drift",
        "value": 0.34,
        "timestamp": "2026-10-17T12:00:00Z",
        **changes,
    }


def post(client: ApiClient, **changes: Any) -> Any:
    return client.request("POST", "/v1/metrics", headers=headers(), json=sample(**changes))


def query(engine: Engine, sql: str) -> list[dict[str, Any]]:
    with engine.connect() as conn:
        return [dict(row._mapping) for row in conn.execute(text(sql))]


def test_breach_detail_audit_and_independent_ledger_verification(
    client: ApiClient,
    owner_engine: Engine,
) -> None:
    response = post(client)
    assert response.status_code == 201
    assert set(response.json()) == {"sample_id", "flagged_event_id"}
    event_id = response.json()["flagged_event_id"]
    flagged = owner_rows(owner_engine)
    assert [r["entry_type"] for r in flagged] == ["EVENT_FLAGGED", "NOTIFICATION_REQUESTED"]
    assert flagged[0]["actor_id"] == "sys-42"
    assert flagged[0]["payload"] == {
        "event_id": event_id,
        "system_id": "sys-42",
        "metric_name": "accuracy_drift",
        "observed_value": "0.34",
        "threshold_value": "0.30",
        "threshold_version_id": "thr-1",
    }
    detail = client.get(f"/v1/events/{event_id}", headers=headers("VIEWER"))
    assert detail.status_code == 200
    body = detail.json()
    assert set(body) == {
        "event_id",
        "system_id",
        "type",
        "metric_name",
        "observed_value",
        "threshold_value",
        "detected_at",
        "state",
        "disposition",
        "deadline_utc",
        "revisions",
        "harm_fields",
    }
    assert (body["observed_value"], body["threshold_value"], body["state"]) == (
        0.34,
        0.30,
        "FLAGGED",
    )
    assert body["detected_at"] == "2026-10-17T12:00:00Z"
    assert body["disposition"] is None and body["deadline_utc"] is None
    assert body["revisions"] == []
    for harm in body["harm_fields"].values():
        assert harm["status"] == "PREFILLED_PENDING_CONFIRMATION"
        assert "accuracy_drift" in harm["evidence"]
        assert harm["confirmed_by"] is None and harm["confirmed_at"] is None
    trail = client.get(f"/v1/events/{event_id}/audit", headers=headers("REVIEWER")).json()
    assert trail[-1]["action"] == "EVENT_VIEWED"
    assert trail[-1]["actor_id"] == "viewer-1"
    assert client.get("/v1/ledger/verify", headers=headers("VIEWER")).json() == {
        "valid": True,
        "entries_checked": 4,
        "first_mismatch": None,
    }
    assert (
        query(owner_engine, "SELECT last_received_at FROM monitored_system")[0]["last_received_at"]
        == T0
    )


@pytest.mark.parametrize(
    ("name", "kind", "value", "breach"),
    [
        ("accuracy_drift", "drift", 0.3, False),
        ("accuracy_drift", "drift", 0.299, False),
        ("accuracy_drift", "drift", 0.301, True),
        ("accuracy", "performance", 0.8, False),
        ("accuracy", "performance", 0.801, False),
        ("accuracy", "performance", 0.799, True),
    ],
)
def test_threshold_direction_and_equality(
    client: ApiClient,
    name: str,
    kind: str,
    value: float,
    breach: bool,
) -> None:
    response = post(client, metric_name=name, metric_type=kind, value=value)
    assert response.status_code == 201
    assert ("flagged_event_id" in response.json()) == breach


@pytest.mark.parametrize("value", [None, True, "0.34", [], {}, float("inf")])
def test_invalid_value_is_malformed(client: ApiClient, value: Any) -> None:
    # Raw JSON permits deliberate Infinity tokens, rejected by our parser/model.
    response = client.request(
        "POST", "/v1/metrics", headers=headers(), content=json.dumps(sample(value=value))
    )
    assert response.status_code == 400
    assert response.json()["error_code"] == "MALFORMED_BODY"


@pytest.mark.parametrize(
    "changes",
    [
        {"timestamp": "2026-10-17T12:00:00"},
        {"timestamp": 123},
        {"timestamp": "2026-10-17T12:00:00-04:00"},
        {"metric_name": ""},
        {"metric_name": "\x00"},
        {"metric_type": "unknown"},
        {"unexpected": "field"},
    ],
)
def test_invalid_body_writes_nothing(
    client: ApiClient,
    owner_engine: Engine,
    changes: dict[str, Any],
) -> None:
    assert post(client, **changes).status_code == 400
    assert query(owner_engine, "SELECT * FROM metric_sample") == []
    assert owner_rows(owner_engine) == []


def test_authentication_authorization_and_system_binding(client: ApiClient) -> None:
    for credential in ({}, {"X-API-Key": "invalid"}):
        response = client.request("POST", "/v1/metrics", headers=credential, content="broken")
        assert response.status_code == 401
    for role in ("REVIEWER", "VIEWER"):
        response = client.request("POST", "/v1/metrics", headers=headers(role), content="broken")
        assert response.status_code == 403
    assert post(client, system_id="other").status_code == 403
    assert client.get("/v1/events/missing", headers=headers()).status_code == 403
    assert client.get("/v1/events/missing").status_code == 401
    assert client.get("/v1/events/missing", headers=headers("VIEWER")).status_code == 404


def test_disabled_key_and_hash_storage(client: ApiClient, owner_engine: Engine) -> None:
    rows = query(owner_engine, "SELECT key_hash FROM api_key")
    assert all(len(row["key_hash"]) == 64 for row in rows)
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE api_key SET enabled = false WHERE role = 'SYSTEM'"))
    assert post(client).status_code == 401


def test_duplicate_replay_and_conflict_do_not_change_heartbeat_or_ledger(
    client: ApiClient,
    owner_engine: Engine,
) -> None:
    original = post(client).json()
    before = owner_rows(owner_engine)
    heartbeat = T0 - timedelta(seconds=500)
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE monitored_system SET last_received_at = :at"), {"at": heartbeat})
    assert post(client).json() == original
    assert post(client, value=0.35).status_code == 409
    assert owner_rows(owner_engine) == before
    assert len(query(owner_engine, "SELECT * FROM metric_sample")) == 1
    assert (
        query(owner_engine, "SELECT last_received_at FROM monitored_system")[0]["last_received_at"]
        == heartbeat
    )


def test_series_occurrences_recovery_and_late_healthy_sample(
    client: ApiClient,
    owner_engine: Engine,
) -> None:
    first = post(client).json()["flagged_event_id"]
    assert post(client, timestamp="2026-10-17T12:05:00Z").json()["flagged_event_id"] == first
    assert (
        "flagged_event_id" not in post(client, value=0.1, timestamp="2026-10-17T11:59:00Z").json()
    )
    assert post(client, timestamp="2026-10-17T12:10:00Z").json()["flagged_event_id"] == first
    post(client, value=0.1, timestamp="2026-10-17T12:15:00Z")
    second = post(client, timestamp="2026-10-17T12:20:00Z").json()["flagged_event_id"]
    assert second != first
    assert len(query(owner_engine, "SELECT * FROM notification")) == 2
    assert len(query(owner_engine, "SELECT * FROM metric_sample WHERE event_id IS NOT NULL")) == 4
    assert [r["entry_type"] for r in owner_rows(owner_engine)] == [
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
    ]


def test_threshold_versions_are_selected_at_sample_time_and_pinned(
    client: ApiClient,
    owner_engine: Engine,
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO threshold_version VALUES"
                " ('thr-new', 'sys-42', 'accuracy_drift', 'drift', 'gt', 0.50, :at)"
            ),
            {"at": T0},
        )
    assert "flagged_event_id" not in post(client).json()
    event_id = post(client, timestamp="2026-10-17T11:59:00Z").json()["flagged_event_id"]
    row = query(owner_engine, "SELECT * FROM event")[0]
    assert row["event_id"] == event_id and row["threshold_version_id"] == "thr-1"
    assert Decimal(row["threshold_value"]) == Decimal("0.30")


def test_missing_threshold_and_unknown_system(client: ApiClient, owner_engine: Engine) -> None:
    assert post(client, metric_name="unconfigured").status_code == 422
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM monitored_system"))
    assert post(client).status_code == 404
    assert query(owner_engine, "SELECT * FROM metric_sample") == []


def test_evidence_snapshot_preserves_decimal_and_preceding_hour(
    client: ApiClient,
    owner_engine: Engine,
) -> None:
    for stamp in ("2026-10-17T10:59:59Z", "2026-10-17T11:00:00Z", "2026-10-17T12:00:01Z"):
        assert post(client, timestamp=stamp, value=0.1).status_code == 201
    payload = json.dumps(sample()).replace("0.34", "0.340000000000000000000000000001")
    assert (
        client.request("POST", "/v1/metrics", headers=headers(), content=payload).status_code == 201
    )
    event = query(owner_engine, "SELECT * FROM event")[0]
    evidence = event["evidence_bundle"]
    assert [item["timestamp"] for item in evidence] == [
        "2026-10-17T11:00:00Z",
        "2026-10-17T12:00:00Z",
    ]
    assert evidence[-1]["value"] == "0.340000000000000000000000000001"
    post(client, timestamp="2026-10-17T12:05:00Z")
    assert (
        query(owner_engine, "SELECT evidence_bundle FROM event")[0]["evidence_bundle"] == evidence
    )


def test_ingestion_closes_gap_then_flags_and_enqueues(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE monitored_system SET monitoring_started_at = :at"),
            {"at": T0 - timedelta(seconds=900)},
        )
    gap_id = gaps.sweep(app_engine)[0]
    response = post(client)
    assert response.status_code == 201
    assert [r["entry_type"] for r in owner_rows(owner_engine)] == [
        "GAP_OPENED",
        "NOTIFICATION_REQUESTED",
        "GAP_CLOSED",
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
    ]
    detail = client.get(f"/v1/events/{gap_id}", headers=headers("VIEWER")).json()
    assert detail["state"] == "CLOSED" and detail["metric_name"] is None
    assert all(
        harm["status"] == "PREFILLED_PENDING_CONFIRMATION"
        for harm in detail["harm_fields"].values()
    )
    assert (
        client.get("/v1/events?type=monitoring_gap", headers=headers("VIEWER")).json()[0][
            "event_id"
        ]
        == gap_id
    )


def test_failure_rolls_back_sample_gap_event_notification_and_heartbeat(
    client: ApiClient,
    owner_engine: Engine,
    app_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE monitored_system SET monitoring_started_at = :at"),
            {"at": T0 - timedelta(seconds=900)},
        )
    gaps.sweep(app_engine)
    before = owner_rows(owner_engine)

    def fail(*args: Any, **kwargs: Any) -> str:
        raise LedgerError("must not escape")

    monkeypatch.setattr(notifications, "enqueue", fail)
    response = post(client)
    assert response.status_code == 500
    assert response.json() == {
        "error_code": "INTERNAL_ERROR",
        "message": "an internal error occurred",
    }
    assert owner_rows(owner_engine) == before
    assert query(owner_engine, "SELECT * FROM metric_sample") == []
    assert query(owner_engine, "SELECT state FROM event") == [{"state": "FLAGGED"}]
    assert query(owner_engine, "SELECT closed_at FROM monitoring_gap") == [{"closed_at": None}]
    assert query(owner_engine, "SELECT last_received_at FROM monitored_system") == [
        {"last_received_at": None}
    ]


def test_concurrent_duplicate_requests_create_one_sample_event_and_notification(
    client: ApiClient,
    owner_engine: Engine,
) -> None:
    barrier = threading.Barrier(12)

    def send(_: int) -> Any:
        barrier.wait(timeout=10)
        return post(client)

    with ThreadPoolExecutor(max_workers=12) as pool:
        responses = list(pool.map(send, range(12)))
    assert [r.status_code for r in responses] == [201] * 12
    assert all(r.json() == responses[0].json() for r in responses)
    for table in ("metric_sample", "event", "notification"):
        assert len(query(owner_engine, f"SELECT * FROM {table}")) == 1
    assert len(owner_rows(owner_engine)) == 2


def test_listing_filters_pagination_and_role_scope(client: ApiClient, owner_engine: Engine) -> None:
    first = post(client).json()["flagged_event_id"]
    post(client, value=0.1, timestamp="2026-10-17T12:05:00Z")
    second = post(client, timestamp="2026-10-17T12:10:00Z").json()["flagged_event_id"]
    ordered = sorted([first, second])  # Detection times both use the fixed receipt clock.
    response = client.get(
        "/v1/events?limit=1&offset=1&type=threshold_breach&state=FLAGGED", headers=headers("VIEWER")
    )
    assert response.status_code == 200
    assert [item["event_id"] for item in response.json()] == ordered[1:]
    assert response.headers["X-Total-Count"] == "2"
    assert client.get("/v1/events?since=2026-10-17T12:00:01Z", headers=headers()).json() == []
    assert client.get("/v1/events?offset=20", headers=headers()).headers["X-Total-Count"] == "2"
    assert client.get("/v1/events?system_id=other", headers=headers()).status_code == 403
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE event SET system_id = 'other'"))
    assert client.get("/v1/events", headers=headers()).json() == []
    assert len(client.get("/v1/events", headers=headers("REVIEWER")).json()) == 2
    for path in ("?limit=0", "?offset=-1", "?limit=201", "?state=bad", "?since=bad"):
        assert client.get("/v1/events" + path, headers=headers("VIEWER")).status_code == 400


def test_ingestion_notification_reaches_a_real_registered_webhook(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    receiver: Receiver,
) -> None:
    receiver.during = lambda: app_sessions_in_transaction(owner_engine)
    registered = client.request(
        "POST",
        "/v1/webhooks",
        headers=headers("REVIEWER"),
        json={"url": receiver.url, "event_types": ["flag_notification"]},
    )
    assert registered.status_code == 201
    assert len(receiver.calls) == 1  # verification ping
    event_id = post(client).json()["flagged_event_id"]
    assert notifications.deliver_due(app_engine) == 1
    assert len(receiver.calls) == 2
    assert receiver.calls[-1].body["event_id"] == event_id
    assert receiver.calls[-1].body["type"] == "flag_notification"
    assert all(call.observed == 0 for call in receiver.calls)
    trail = client.get(f"/v1/events/{event_id}/audit", headers=headers("REVIEWER")).json()
    assert trail[-1]["action"] == "NOTIFICATION_DELIVERED"


@pytest.mark.parametrize("table", ["api_key", "threshold_version"])
def test_application_cannot_provision_or_mutate_credentials_and_thresholds(
    client: ApiClient,
    app_engine: Engine,
    table: str,
) -> None:
    from sqlalchemy.exc import ProgrammingError

    with app_engine.connect() as conn:
        for privilege in ("INSERT", "UPDATE", "DELETE"):
            assert not conn.execute(
                text("SELECT has_table_privilege(current_user, :table, :privilege)"),
                {"table": table, "privilege": privilege},
            ).scalar_one()
        conn.rollback()
        with pytest.raises(ProgrammingError):
            conn.execute(text(f"DELETE FROM {table}"))
        conn.rollback()
