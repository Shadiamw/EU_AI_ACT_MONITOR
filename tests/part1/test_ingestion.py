"""Part 1: POST /v1/metrics and threshold detection (spec 1.4, 1.5, 1.9; AC-01, AC-03).

Every expected value below comes from the spec or the contract and is written out here, not
read back from the code under test. The seeded thresholds are thr-1 accuracy_drift gt 0.30 and
thr-2 accuracy lt 0.70 (spec 4, Seed fixtures). Times are on the fake clock, which starts at
contract Example 1's timestamp, 2026-09-27T10:05:00Z.
"""

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app import gaps, ledger, notifications
from app.platform import with_phase4_tables
from tests.api.conftest import INTERNAL, MALFORMED, ApiClient
from tests.db_helpers import HARM_FIELDS, bearer
from tests.part1.conftest import T0, entry_types, get, metric, post, post_raw, rows
from tests.worker.conftest import FakeClock, Receiver, app_sessions_in_transaction

PREFILLED = {
    "status": "PREFILLED_PENDING_CONFIRMATION",
    "evidence": None,
    "confirmed_by": None,
    "confirmed_at": None,
}
NOT_APPLICABLE = {
    "status": "NOT_APPLICABLE",
    "evidence": None,
    "confirmed_by": None,
    "confirmed_at": None,
}


def exact(response: Any) -> Any:
    """The response body with JSON numbers read as Decimal, so 0.30 stays 0.30."""
    return json.loads(response.text, parse_float=Decimal)


def raw_metric(value: str, **changes: Any) -> str:
    """Example 1 as JSON text with `value` written verbatim (for example 0.30 or NaN)."""
    body = json.dumps(metric(value="VALUE", **changes))
    return body.replace('"VALUE"', value)


def at(minutes: float) -> str:
    """The API timestamp `minutes` after 2026-09-27T10:05:00Z (negative for before)."""
    moment = T0 + timedelta(minutes=minutes)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def count(engine: Engine, table: str) -> int:
    return len(rows(engine, f"SELECT 1 FROM {table}"))


# Spec 1.9: Contract Example 1


def test_contract_example_1_flags_and_detail_shows_the_breach(
    client: ApiClient, owner_engine: Engine
) -> None:
    # The request body exactly as contract Example 1 prints it.
    response = post_raw(
        client,
        '{\n"system_id": "sys-42",\n"metric_name": "accuracy_drift", "metric_type": "drift",\n'
        '"value": 0.34,\n"timestamp": "2026-09-27T10:05:00Z"\n}',
    )
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"sample_id", "flagged_event_id"}
    assert body["sample_id"].startswith("smp-")
    assert body["flagged_event_id"].startswith("evt-")

    detail = get(client, f"/v1/events/{body['flagged_event_id']}")
    assert detail.status_code == 200
    assert exact(detail) == {
        "event_id": body["flagged_event_id"],
        "system_id": "sys-42",
        "type": "threshold_breach",
        "metric_name": "accuracy_drift",
        "observed_value": Decimal("0.34"),
        "threshold_value": Decimal("0.30"),
        "detected_at": "2026-09-27T10:05:00Z",
        "state": "FLAGGED",
        "disposition": None,
        "deadline_utc": None,
        "revisions": [],
        "harm_fields": {key: PREFILLED for key in HARM_FIELDS},
    }


# Spec 1.9: the boundaries (AC-03, both metric types)


@pytest.mark.parametrize(
    ("value", "flagged"),
    [
        ("0.30", False),  # exactly at the limit must not flag
        ("0.3", False),  # the same number, written differently
        ("0.31", True),
        ("0.30000000000000000001", True),  # past the limit only in Decimal, not in float
        ("0.29", False),
    ],
)
def test_drift_boundary(client: ApiClient, owner_engine: Engine, value: str, flagged: bool) -> None:
    response = post_raw(client, raw_metric(value))
    assert response.status_code == 201
    keys = {"sample_id", "flagged_event_id"} if flagged else {"sample_id"}
    assert set(response.json()) == keys
    assert count(owner_engine, "event") == (1 if flagged else 0)


@pytest.mark.parametrize(
    ("value", "flagged"),
    [
        ("0.70", False),  # exactly at the limit must not flag
        ("0.69", True),
        ("0.69999999999999999999", True),  # below the limit only in Decimal
        ("0.71", False),
    ],
)
def test_performance_boundary(
    client: ApiClient, owner_engine: Engine, value: str, flagged: bool
) -> None:
    response = post_raw(
        client, raw_metric(value, metric_name="accuracy", metric_type="performance")
    )
    assert response.status_code == 201
    keys = {"sample_id", "flagged_event_id"} if flagged else {"sample_id"}
    assert set(response.json()) == keys
    assert count(owner_engine, "event") == (1 if flagged else 0)
    if flagged:
        [event] = rows(owner_engine, "SELECT threshold_version_id, threshold_value FROM event")
        assert event == {"threshold_version_id": "thr-2", "threshold_value": Decimal("0.70")}


# Spec 1.9: sustained breach and series reset (spec 1.4 steps 9 to 11)


def test_sustained_breach_joins_the_open_series(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    first = post(client).json()
    fake_clock.advance(300)
    second = post(client, value=0.35, timestamp=at(5))
    assert second.status_code == 201
    assert set(second.json()) == {"sample_id", "flagged_event_id"}
    assert second.json()["flagged_event_id"] == first["flagged_event_id"]
    assert second.json()["sample_id"] != first["sample_id"]

    listed = get(client, "/v1/events").json()
    assert [item["event_id"] for item in listed] == [first["flagged_event_id"]]
    assert count(owner_engine, "notification") == 1
    assert entry_types(owner_engine) == ["SEED", "EVENT_FLAGGED", "NOTIFICATION_REQUESTED"]
    assert rows(owner_engine, "SELECT occurrence_count, closed_at FROM breach_series") == [
        {"occurrence_count": 2, "closed_at": None}
    ]
    linked = rows(owner_engine, "SELECT event_id FROM metric_sample")
    assert linked == [{"event_id": first["flagged_event_id"]}] * 2


def test_series_reset_creates_a_new_event(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    first = post(client).json()["flagged_event_id"]
    fake_clock.advance(300)
    healthy = post(client, value=0.10, timestamp=at(5))
    assert healthy.status_code == 201
    assert set(healthy.json()) == {"sample_id"}
    fake_clock.advance(300)
    again = post(client, timestamp=at(10)).json()
    assert again["flagged_event_id"] != first

    assert count(owner_engine, "event") == 2
    assert count(owner_engine, "notification") == 2
    series = rows(
        owner_engine, "SELECT event_id, opened_at, closed_at FROM breach_series ORDER BY opened_at"
    )
    assert series == [
        {"event_id": first, "opened_at": T0, "closed_at": T0 + timedelta(seconds=300)},
        {
            "event_id": again["flagged_event_id"],
            "opened_at": T0 + timedelta(seconds=600),
            "closed_at": None,
        },
    ]
    assert entry_types(owner_engine) == [
        "SEED",
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
    ]


def test_any_healthy_sample_closes_the_series(client: ApiClient, owner_engine: Engine) -> None:
    """Spec 1.4 step 9 has no condition on the sample's timestamp: a healthy sample timestamped
    before the breach still closes the open series (decision 129)."""
    first = post(client).json()["flagged_event_id"]
    assert set(post(client, value=0.10, timestamp=at(-5)).json()) == {"sample_id"}
    second = post(client, timestamp=at(5)).json()["flagged_event_id"]
    assert second != first


# Spec 1.9: duplicates (spec 1.4 step 5; decision 116)


def test_duplicate_returns_200_with_the_original_ids(
    client: ApiClient, owner_engine: Engine
) -> None:
    original = post(client, value=0.10)
    assert original.status_code == 201
    duplicate = post(client, value=0.10)
    assert duplicate.status_code == 200
    assert duplicate.json() == original.json()
    assert count(owner_engine, "metric_sample") == 1
    assert entry_types(owner_engine) == ["SEED"]


def test_duplicate_of_a_breach_returns_the_original_flagged_event_id(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    original = post(client)
    assert original.status_code == 201
    fake_clock.advance(60)
    duplicate = post(client)
    assert duplicate.status_code == 200
    assert duplicate.json() == original.json()
    assert set(duplicate.json()) == {"sample_id", "flagged_event_id"}

    for table in ("metric_sample", "event", "notification", "breach_series"):
        assert count(owner_engine, table) == 1, table
    assert entry_types(owner_engine) == ["SEED", "EVENT_FLAGGED", "NOTIFICATION_REQUESTED"]
    # Step 5: the duplicate sets last_received_at to clock.now().
    assert rows(owner_engine, "SELECT last_received_at FROM monitored_system") == [
        {"last_received_at": T0 + timedelta(seconds=60)}
    ]
    assert rows(owner_engine, "SELECT occurrence_count FROM breach_series") == [
        {"occurrence_count": 1}
    ]


def test_duplicate_with_a_changed_value_is_still_a_duplicate(
    client: ApiClient, owner_engine: Engine
) -> None:
    """The identity is (system_id, metric_name, timestamp) only (spec 1.4 step 5). A healthy
    value with a breach's identity neither closes the series nor changes the stored value."""
    original = post(client).json()
    changed = post(client, value=0.10)
    assert changed.status_code == 200
    assert changed.json() == original
    assert rows(owner_engine, "SELECT value FROM metric_sample") == [{"value": Decimal("0.34")}]
    assert rows(owner_engine, "SELECT closed_at FROM breach_series") == [{"closed_at": None}]
    assert post(client, timestamp=at(5)).json()["flagged_event_id"] == original["flagged_event_id"]


def test_duplicate_does_not_close_an_open_gap(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Step 5 returns before step 6, so a duplicate does not call gaps.close_if_open
    (decision 116); the next new sample does."""
    post(client, value=0.10)
    fake_clock.advance(900)
    [gap] = gaps.sweep(app_engine)
    assert post(client, value=0.10).status_code == 200
    assert rows(owner_engine, "SELECT state FROM event WHERE event_id = :e", e=gap) == [
        {"state": "FLAGGED"}
    ]
    assert "GAP_CLOSED" not in entry_types(owner_engine)
    assert rows(owner_engine, "SELECT last_received_at FROM monitored_system") == [
        {"last_received_at": T0 + timedelta(seconds=900)}
    ]

    assert post(client, value=0.10, timestamp=at(15)).status_code == 201
    assert rows(owner_engine, "SELECT state FROM event WHERE event_id = :e", e=gap) == [
        {"state": "CLOSED"}
    ]
    assert entry_types(owner_engine)[-1] == "GAP_CLOSED"


def test_concurrent_duplicates_create_one_sample_event_and_notification(
    client: ApiClient, owner_engine: Engine
) -> None:
    barrier = threading.Barrier(8)

    def send(_: int) -> Any:
        barrier.wait(timeout=10)
        return post(client)

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(send, range(8)))
    assert sorted(r.status_code for r in responses) == [200] * 7 + [201]
    assert all(r.json() == responses[0].json() for r in responses)
    for table in ("metric_sample", "event", "notification"):
        assert count(owner_engine, table) == 1, table
    assert entry_types(owner_engine) == ["SEED", "EVENT_FLAGGED", "NOTIFICATION_REQUESTED"]


def test_concurrent_breaches_create_one_event(client: ApiClient, owner_engine: Engine) -> None:
    barrier = threading.Barrier(8)

    def send(i: int) -> Any:
        barrier.wait(timeout=10)
        return post(client, timestamp=at(i))

    with ThreadPoolExecutor(max_workers=8) as pool:
        responses = list(pool.map(send, range(8)))
    assert [r.status_code for r in responses] == [201] * 8
    assert len({r.json()["flagged_event_id"] for r in responses}) == 1
    assert count(owner_engine, "event") == 1
    assert rows(owner_engine, "SELECT occurrence_count FROM breach_series") == [
        {"occurrence_count": 8}
    ]


# Spec 1.9: cadence (AC-01, receiving side)


def test_ac01_cadence_twelve_samples_no_gap(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    for i in range(12):
        if i:
            fake_clock.advance(300)
            assert gaps.sweep(app_engine) == []
        response = post(client, value=0.10, timestamp=at(5 * i))
        assert response.status_code == 201, i
    fake_clock.advance(300)
    assert gaps.sweep(app_engine) == []
    assert count(owner_engine, "metric_sample") == 12
    assert get(client, "/v1/events?type=monitoring_gap").json() == []


# Spec 1.9: validation matrix (spec 1.4 step 2)

VALID = metric()


def without(field: str) -> str:
    return json.dumps({k: v for k, v in VALID.items() if k != field})


INVALID_BODIES = {
    "not-json": '{"system_id": "sys-42",',
    "empty": "",
    "array": json.dumps([VALID]),
    "missing-system_id": without("system_id"),
    "missing-metric_name": without("metric_name"),
    "missing-metric_type": without("metric_type"),
    "missing-value": without("value"),
    "missing-timestamp": without("timestamp"),
    "metric_type-bad": json.dumps(metric(metric_type="latency")),
    "value-abc": json.dumps(metric(value="abc")),
    "value-string-number": json.dumps(metric(value="0.34")),
    "value-NaN": raw_metric("NaN"),
    "value-Infinity": raw_metric("Infinity"),
    "value-minus-Infinity": raw_metric("-Infinity"),
    "value-true": raw_metric("true"),
    "value-null": raw_metric("null"),
    "value-array": raw_metric("[0.34]"),
    "timestamp-bad": json.dumps(metric(timestamp="yesterday")),
    "timestamp-no-zone": json.dumps(metric(timestamp="2026-09-27T10:05:00")),
    "timestamp-offset": json.dumps(metric(timestamp="2026-09-27T12:05:00+02:00")),
    "timestamp-number": json.dumps(metric(timestamp=1790503500)),
    "system_id-number": json.dumps(metric(system_id=42)),
    "metric_name-null": json.dumps(metric(metric_name=None)),
    "metric_name-nul-character": json.dumps(metric(metric_name="accuracy\u0000drift")),
}


@pytest.mark.parametrize("content", INVALID_BODIES.values(), ids=INVALID_BODIES.keys())
def test_validation_matrix(client: ApiClient, owner_engine: Engine, content: str) -> None:
    response = post_raw(client, content)
    assert response.status_code == 400
    assert response.json() == MALFORMED
    assert count(owner_engine, "metric_sample") == 0
    assert entry_types(owner_engine) == ["SEED"]


@pytest.mark.parametrize("timestamp", ["2026-09-27T10:05:00Z", "2026-09-27T10:05:00+00:00"])
def test_both_utc_forms_are_accepted(
    client: ApiClient, owner_engine: Engine, timestamp: str
) -> None:
    assert post(client, timestamp=timestamp).status_code == 201
    assert rows(owner_engine, "SELECT timestamp FROM metric_sample") == [{"timestamp": T0}]


def test_unknown_extra_field_is_ignored(client: ApiClient, owner_engine: Engine) -> None:
    response = post(client, unexpected="field", value=0.10)
    assert response.status_code == 201
    assert set(response.json()) == {"sample_id"}
    assert count(owner_engine, "metric_sample") == 1


@pytest.mark.parametrize("name", ["", "m" * 1000])
def test_long_and_empty_names_are_accepted(
    client: ApiClient, owner_engine: Engine, name: str
) -> None:
    """The spec sets no length rule for metric_name (decision 121). Neither has a threshold."""
    response = post(client, metric_name=name)
    assert response.status_code == 201
    assert set(response.json()) == {"sample_id"}
    assert rows(owner_engine, "SELECT metric_name FROM metric_sample") == [{"metric_name": name}]


def test_no_threshold_stores_the_sample_and_returns_201(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    fake_clock.advance(7)
    response = post(client, metric_name="latency_p99", metric_type="performance", value=9.5)
    assert response.status_code == 201
    assert set(response.json()) == {"sample_id"}
    assert rows(owner_engine, "SELECT metric_name, value, event_id FROM metric_sample") == [
        {"metric_name": "latency_p99", "value": Decimal("9.5"), "event_id": None}
    ]
    assert count(owner_engine, "event") == 0
    assert entry_types(owner_engine) == ["SEED"]
    assert rows(owner_engine, "SELECT last_received_at FROM monitored_system") == [
        {"last_received_at": T0 + timedelta(seconds=7)}
    ]


def test_unknown_system_after_a_valid_key_is_500(client: ApiClient, owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        if "harm_context" in with_phase4_tables(conn):  # its system_id references the system
            conn.execute(text("DELETE FROM harm_context"))
        conn.execute(text("DELETE FROM monitored_system"))
    response = post(client)
    assert response.status_code == 500
    assert response.json() == INTERNAL
    assert count(owner_engine, "metric_sample") == 0
    assert entry_types(owner_engine) == ["SEED"]


# Spec 1.5: what a new breach event contains


def test_new_event_content(client: ApiClient, owner_engine: Engine, fake_clock: FakeClock) -> None:
    fake_clock.advance(7)  # received 7 s after the sample's own timestamp
    body = post(client).json()
    event_id = body["flagged_event_id"]
    [event] = rows(
        owner_engine,
        "SELECT event_id, system_id, type, metric_name, observed_value, threshold_value,"
        " threshold_version_id, detected_at, received_at, state, deadline_utc, retain_until"
        " FROM event",
    )
    assert event == {
        "event_id": event_id,
        "system_id": "sys-42",
        "type": "threshold_breach",
        "metric_name": "accuracy_drift",
        "observed_value": Decimal("0.34"),
        "threshold_value": Decimal("0.30"),
        "threshold_version_id": "thr-1",
        "detected_at": T0,  # the request's timestamp
        "received_at": T0 + timedelta(seconds=7),  # clock.now()
        "state": "FLAGGED",
        "deadline_utc": None,
        # Six calendar months is 2027-03-27; 183 days is 2027-03-29; the later applies.
        "retain_until": datetime(2027, 3, 29, 10, 5, tzinfo=UTC),
    }
    harm = rows(
        owner_engine,
        "SELECT field_key, status, evidence, confirmed_by, confirmed_at FROM harm_field"
        " ORDER BY field_key",
    )
    assert harm == [{"field_key": key, **PREFILLED} for key in sorted(HARM_FIELDS)]
    assert rows(
        owner_engine,
        "SELECT system_id, metric_name, event_id, opened_at, closed_at, occurrence_count"
        " FROM breach_series",
    ) == [
        {
            "system_id": "sys-42",
            "metric_name": "accuracy_drift",
            "event_id": event_id,
            "opened_at": T0 + timedelta(seconds=7),
            "closed_at": None,
            "occurrence_count": 1,
        }
    ]
    assert rows(owner_engine, "SELECT sample_id, event_id, received_at FROM metric_sample") == [
        {
            "sample_id": body["sample_id"],
            "event_id": event_id,
            "received_at": T0 + timedelta(seconds=7),
        }
    ]


def test_evidence_bundle_window_and_hash_reproduce(client: ApiClient, owner_engine: Engine) -> None:
    """The 60 minutes up to and including the triggering sample, this metric only (spec 1.5;
    decision 119: both ends inclusive, values as decimal strings)."""
    for timestamp, value, name in (
        ("2026-09-27T09:04:59Z", "0.11", "accuracy_drift"),  # 60 min 1 s before: outside
        ("2026-09-27T09:05:00Z", "0.10", "accuracy_drift"),  # exactly 60 min before: inside
        ("2026-09-27T09:40:00Z", "0.90", "accuracy"),  # another metric: outside
        ("2026-09-27T09:50:00Z", "0.20", "accuracy_drift"),
        ("2026-09-27T10:05:01Z", "0.12", "accuracy_drift"),  # after the trigger: outside
    ):
        kind = "drift" if name == "accuracy_drift" else "performance"
        content = raw_metric(value, timestamp=timestamp, metric_name=name, metric_type=kind)
        assert post_raw(client, content).status_code == 201
    trigger = post_raw(client, raw_metric("0.340000000000000000000000000001"))
    event_id = trigger.json()["flagged_event_id"]

    expected = {
        "window": {"start": "2026-09-27T09:05:00Z", "end": "2026-09-27T10:05:00Z"},
        "samples": [
            {"metric_name": "accuracy_drift", "value": "0.10", "timestamp": "2026-09-27T09:05:00Z"},
            {"metric_name": "accuracy_drift", "value": "0.20", "timestamp": "2026-09-27T09:50:00Z"},
            {
                "metric_name": "accuracy_drift",
                "value": "0.340000000000000000000000000001",
                "timestamp": "2026-09-27T10:05:00Z",
            },
        ],
    }
    # Part 2's canonical JSON: keys sorted, no spaces, UTF-8 (decision 119).
    canonical = json.dumps(expected, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    [event] = rows(owner_engine, "SELECT evidence_bundle, evidence_sha256 FROM event")
    assert event == {
        "evidence_bundle": expected,
        "evidence_sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
    }

    # A sample joining the series later does not change the snapshot.
    assert post(client, timestamp=at(5)).json()["flagged_event_id"] == event_id
    assert rows(owner_engine, "SELECT evidence_bundle FROM event") == [
        {"evidence_bundle": expected}
    ]


def test_newest_threshold_version_is_used(client: ApiClient, owner_engine: Engine) -> None:
    """The newest version is the highest `version` (decision 120). An event keeps the version
    it was created under."""
    first = post(client).json()["flagged_event_id"]
    post(client, value=0.10, timestamp=at(5))  # closes the series
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO threshold_version (threshold_version_id, system_id, metric_name,"
                " metric_type, comparator, limit_value, version, valid_from) VALUES"
                " ('thr-3', 'sys-42', 'accuracy_drift', 'drift', 'gt', 0.50, 2, :at)"
            ),
            {"at": T0},
        )
    assert set(post(client, timestamp=at(10)).json()) == {"sample_id"}  # 0.34 is not > 0.50
    second = post(client, value=0.51, timestamp=at(15)).json()["flagged_event_id"]
    assert rows(
        owner_engine,
        "SELECT event_id, threshold_version_id, threshold_value FROM event ORDER BY detected_at",
    ) == [
        {"event_id": first, "threshold_version_id": "thr-1", "threshold_value": Decimal("0.30")},
        {"event_id": second, "threshold_version_id": "thr-3", "threshold_value": Decimal("0.50")},
    ]


# Spec 1.4 step 11, spec 2.4 and spec 7: the ledger, atomicity and lock order


def test_ledger_entries_for_a_new_breach_in_order(client: ApiClient, owner_engine: Engine) -> None:
    event_id = post(client).json()["flagged_event_id"]
    [notification_id] = [
        r["notification_id"] for r in rows(owner_engine, "SELECT * FROM notification")
    ]
    entries = rows(
        owner_engine,
        "SELECT entry_type, actor_id, subject_id, payload FROM ledger_entry ORDER BY seq",
    )
    assert entries[1:] == [
        {
            "entry_type": "EVENT_FLAGGED",
            "actor_id": "sys-42",
            "subject_id": event_id,
            "payload": {
                "event_id": event_id,
                "system_id": "sys-42",
                "metric_name": "accuracy_drift",
                "observed_value": "0.34",
                "threshold_value": "0.30",
                "threshold_version_id": "thr-1",
            },
        },
        {
            # Part 3's enqueue appends it with actor `system` (spec 6; decision 93).
            "entry_type": "NOTIFICATION_REQUESTED",
            "actor_id": "system",
            "subject_id": event_id,
            "payload": {
                "notification_id": notification_id,
                "event_id": event_id,
                "notification_type": "flag_notification",
            },
        },
    ]
    assert get(client, "/v1/ledger/verify").json() == {
        "valid": True,
        "entries_checked": 3,
        "first_mismatch": None,
    }


def test_failure_after_enqueue_rolls_back_everything(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    post(client, value=0.10)
    fake_clock.advance(900)
    [gap] = gaps.sweep(app_engine)
    before = {
        table: rows(owner_engine, f"SELECT * FROM {table}")
        for table in (
            "ledger_entry",
            "metric_sample",
            "event",
            "harm_field",
            "breach_series",
            "notification",
            "monitored_system",
        )
    }
    real_enqueue = notifications.enqueue

    def enqueue_then_fail(session: Session, event_id: str, *args: Any) -> str:
        real_enqueue(session, event_id, *args)
        raise RuntimeError("failure after enqueue")

    monkeypatch.setattr(notifications, "enqueue", enqueue_then_fail)
    fake_clock.advance(10)
    response = post(client, timestamp=at(15))
    assert response.status_code == 500
    assert response.json() == INTERNAL
    for table, content in before.items():
        assert rows(owner_engine, f"SELECT * FROM {table}") == content, table
    assert rows(owner_engine, "SELECT state FROM event WHERE event_id = :e", e=gap) == [
        {"state": "FLAGGED"}
    ]


def test_open_series_is_locked_before_the_ledger_lock(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 7: event rows before the ledger lock. With an open series and an open gap, the
    first ledger.append of the ingest (GAP_CLOSED, inside gaps.close_if_open) must find the
    series row already locked (decision 122)."""
    post(client)  # opens a breach series
    fake_clock.advance(900)
    gaps.sweep(app_engine)
    seen: list[str] = []
    real_append = ledger.append

    def checking_append(session: Session, entry_type: str, *args: Any) -> Any:
        with owner_engine.connect() as other:
            try:
                other.execute(
                    text("SELECT 1 FROM breach_series WHERE closed_at IS NULL FOR UPDATE NOWAIT")
                )
                seen.append(f"{entry_type}: series unlocked")
            except OperationalError:
                seen.append(f"{entry_type}: series locked")
            other.rollback()
        return real_append(session, entry_type, *args)

    monkeypatch.setattr(ledger, "append", checking_append)
    response = post(client, timestamp=at(15))
    assert response.status_code == 201
    assert seen == ["GAP_CLOSED: series locked"]


def test_ingestion_closes_gap_then_flags_and_enqueues(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    post(client, value=0.10)
    fake_clock.advance(900)
    [gap] = gaps.sweep(app_engine)
    response = post(client, timestamp=at(15))
    assert response.status_code == 201
    assert entry_types(owner_engine) == [
        "SEED",
        "GAP_OPENED",
        "NOTIFICATION_REQUESTED",
        "GAP_CLOSED",
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
    ]
    detail = exact(get(client, f"/v1/events/{gap}"))
    assert detail["state"] == "CLOSED"
    assert (detail["metric_name"], detail["observed_value"], detail["threshold_value"]) == (
        None,
        None,
        None,
    )
    assert detail["harm_fields"] == {key: NOT_APPLICABLE for key in HARM_FIELDS}


def test_ingestion_notification_reaches_a_real_registered_webhook(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, receiver: Receiver
) -> None:
    receiver.during = lambda: app_sessions_in_transaction(owner_engine)
    registered = client.request(
        "POST",
        "/v1/webhooks",
        headers=bearer("rev-1"),
        json={"url": receiver.url, "event_types": ["flag_notification"]},
    )
    assert registered.status_code == 201
    assert len(receiver.calls) == 1  # the verification ping
    event_id = post(client).json()["flagged_event_id"]
    assert notifications.deliver_due(app_engine) == 1
    assert len(receiver.calls) == 2
    body = receiver.calls[-1].body
    assert (body["notification_type"], body["event_id"], body["type"]) == (
        "flag_notification",
        event_id,
        "threshold_breach",
    )
    assert all(call.observed == 0 for call in receiver.calls)
    trail = get(client, f"/v1/events/{event_id}/audit", "rev-1").json()
    assert [item["action"] for item in trail] == [
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
        "NOTIFICATION_DELIVERED",
    ]
