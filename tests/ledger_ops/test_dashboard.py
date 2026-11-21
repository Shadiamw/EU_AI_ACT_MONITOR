"""Phase 5 Part 3, M7: GET /v1/ops/dashboard (spec 7.1, 7.2, 7.4, D14; decision 305).

Driven through the real app with real keys and the fake clock. The body has exactly the spec 7.2
keys, in order; `clock` only when ENVIRONMENT=test; and ids, counts, codes and timestamps only,
checked by scanning the whole body for a seeded payload value, a ledger hash, a webhook URL and
the reviewer's rationale. Expected values come from the spec, not from output.
"""

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine

from app import clock
from app.formats import api_time
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.ledger_ops.test_projection import breach, dispose, view
from tests.part1.conftest import get, rows
from tests.worker.conftest import FakeClock, add_webhook

URL = "/v1/ops/dashboard"
WALL = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)

UNAUTHORIZED = {"error_code": "MISSING_OR_INVALID_KEY", "message": "missing or invalid API key"}
FORBIDDEN = {"error_code": "FORBIDDEN_ROLE", "message": "this role may not call this endpoint"}

# Spec 7.2, in order.
TOP_KEYS = [
    "generated_at",
    "version",
    "environment",
    "worker",
    "ledger",
    "events",
    "deadlines",
    "reports",
    "retention",
    "clock",
    "alerts",
]
WORKER_KEYS = [
    "sweeper_last_ran_seconds_ago",
    "pending_deliveries",
    "oldest_pending_delivery_seconds",
    "failed_deliveries_24h",
    "overdue_escalations",
]
LEDGER_KEYS = ["entries", "head_seq", "last_verification", "last_verification_age_seconds"]
EVENTS_KEYS = ["by_state", "open_gaps"]
STATES = ["FLAGGED", "UNDER_REVIEW", "DISPOSITIONED", "CLOSED"]
DEADLINE_KEYS = ["due_within_24h_unsubmitted", "passed_unsubmitted", "passed_submitted"]
REPORT_KEYS = ["assembled", "submitted", "superseded"]
RETENTION_KEYS = ["past_retain_until_not_erased", "erased"]
CLOCK_KEYS = ["simulated_time", "offset_seconds"]
ALERT_KEYS = ["code", "severity", "since", "detail"]

SECRET_URL = "https://hooks.example.test/s3cr3t-webhook-path"
RATIONALE = "reviewer note: patient transferred to ward nine"


def test_dashboard_roles(client: ApiClient) -> None:
    """Spec 7.4: no key 401, SYSTEM 403, VIEWER 200, REVIEWER 200."""
    response = client.get(URL)
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    response = client.get(URL, headers=bearer("sys-42"))
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    for actor in ("viewer-1", "rev-1"):
        response = get(client, URL, actor=actor)
        assert response.status_code == 200, response.text
        assert list(response.json()) == TOP_KEYS


def detail_is_ids_and_counts(alert: dict[str, Any]) -> bool:
    return all(
        value is None or isinstance(value, (bool, int, str)) for value in alert["detail"].values()
    )


def test_dashboard_shape(
    client: ApiClient,
    fake_clock: FakeClock,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_VERSION", "abc123")
    add_webhook(owner_engine, "wh-secret", SECRET_URL, ["flag_notification"])
    event_id = breach(client, fake_clock)  # accuracy_drift 0.34 on sys-42
    view(client, event_id, "rev-1")
    assert dispose(client, event_id, "DEATH", RATIONALE).status_code == 200
    monkeypatch.setattr(clock, "wall_now", lambda: WALL)
    run = client.request("POST", "/v1/ledger/verifications", headers=bearer("rev-1")).json()
    monkeypatch.setattr(clock, "wall_now", lambda: WALL + timedelta(hours=1))
    entries = rows(owner_engine, "SELECT seq, hash FROM ledger_entry ORDER BY seq")

    for actor in ("viewer-1", "rev-1"):
        response = get(client, URL, actor=actor)
        assert response.status_code == 200, response.text
        body = response.json()

        # Exactly the 7.2 keys, in order, at every level.
        assert list(body) == TOP_KEYS
        assert list(body["worker"]) == WORKER_KEYS
        assert list(body["ledger"]) == LEDGER_KEYS
        assert list(body["events"]) == EVENTS_KEYS
        assert list(body["events"]["by_state"]) == STATES
        assert list(body["deadlines"]) == DEADLINE_KEYS
        assert list(body["reports"]) == REPORT_KEYS
        assert list(body["retention"]) == RETENTION_KEYS
        assert list(body["clock"]) == CLOCK_KEYS
        assert all(list(alert) == ALERT_KEYS for alert in body["alerts"])

        assert (body["version"], body["environment"]) == ("abc123", "test")
        assert body["generated_at"] == "2026-11-12T04:00:00Z"  # wall clock
        assert body["ledger"]["entries"] == len(entries)
        assert body["ledger"]["head_seq"] == entries[-1]["seq"]
        assert body["ledger"]["last_verification"]["run_id"] == run["run_id"]
        assert body["ledger"]["last_verification_age_seconds"] == 3600
        assert body["events"]["by_state"] == {
            "FLAGGED": 0,
            "UNDER_REVIEW": 0,
            "DISPOSITIONED": 1,
            "CLOSED": 0,
        }
        assert body["events"]["open_gaps"] == 0
        # Decision 305: no report tables on this branch, so every report count is 0 and the
        # event counts as unsubmitted; its deadline is days away.
        assert body["deadlines"] == {
            "due_within_24h_unsubmitted": 0,
            "passed_unsubmitted": 0,
            "passed_submitted": 0,
        }
        assert body["reports"] == {"assembled": 0, "submitted": 0, "superseded": 0}
        assert body["retention"] == {"past_retain_until_not_erased": 0, "erased": 0}
        assert body["clock"] == {"simulated_time": api_time(fake_clock.now()), "offset_seconds": 0}
        assert all(detail_is_ids_and_counts(alert) for alert in body["alerts"])

        # Ids, counts, codes and timestamps only: no payload value, hash, webhook URL or
        # reviewer text anywhere in the body, for either role.
        leaks = ["0.34", "accuracy_drift", SECRET_URL, "hooks.example.test", RATIONALE]
        leaks += [entry["hash"] for entry in entries] + [run["head_hash"]]
        assert [value for value in leaks if value in response.text] == []

    # Outside the test environment there is no clock block.
    monkeypatch.setenv("ENVIRONMENT", "production")
    body = get(client, URL, actor="viewer-1").json()
    assert list(body) == [key for key in TOP_KEYS if key != "clock"]
    assert body["environment"] == "production"
