"""Phase 5 Part 3, M5: the ledger-verification endpoints (spec 6.1, 7.4, delta 5).

POST /v1/ledger/verifications (REVIEWER) runs a MANUAL verification and returns 201 with the
stored run; GET /v1/ledger/verifications?limit=n (REVIEWER or VIEWER) lists runs newest first.
Driven through the real app with real keys; the wall clock (which stamps a run) is patched as
app.clock.wall_now so the order of runs is fixed. Expected values come from the spec, not from
output.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import clock
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer, trigger_disabled
from tests.ledger_ops.test_projection import breach, view
from tests.part1.conftest import get, rows
from tests.worker.conftest import FakeClock

URL = "/v1/ledger/verifications"

# Exactly the twelve columns of spec 6.1, nothing more and nothing less.
RUN_KEYS = {
    "run_id",
    "trigger",
    "started_at",
    "finished_at",
    "valid",
    "entries_checked",
    "first_mismatch",
    "head_seq",
    "head_hash",
    "head_check",
    "projection_mismatches",
    "report_mismatches",
}

UNAUTHORIZED = {"error_code": "MISSING_OR_INVALID_KEY", "message": "missing or invalid API key"}
FORBIDDEN = {"error_code": "FORBIDDEN_ROLE", "message": "this role may not call this endpoint"}
MALFORMED_QUERY = {
    "error_code": "MALFORMED_QUERY",
    "message": "limit must be an integer from 1 to 100",
}

# The API's time format (spec, "Shared rules"): UTC, Z, fractional seconds only when present.
API_TIME = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]*[1-9])?Z")

WALL = datetime(2026, 11, 12, 3, 0, tzinfo=UTC)


def ledger(engine: Engine) -> list[dict[str, Any]]:
    return rows(engine, "SELECT seq, hash FROM ledger_entry ORDER BY seq")


def stored_runs(engine: Engine) -> list[dict[str, Any]]:
    return rows(engine, "SELECT run_id, trigger FROM ledger_verification_run ORDER BY started_at")


def post_run(client: ApiClient, monkeypatch: pytest.MonkeyPatch, moment: datetime) -> Any:
    monkeypatch.setattr(clock, "wall_now", lambda: moment)
    response = client.request("POST", URL, headers=bearer("rev-1"))
    assert response.status_code == 201, response.text
    return response.json()


def test_verification_routes_roles_and_shapes(
    client: ApiClient,
    fake_clock: FakeClock,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    event_id = breach(client, fake_clock)
    view(client, event_id, "rev-1")
    view(client, event_id, "viewer-1")  # the head is an EVENT_VIEWED, outside the projection
    entries = ledger(owner_engine)

    # The 7.4 matrix. No key: 401 on both routes; SYSTEM: 403 on both; VIEWER: GET only;
    # REVIEWER: both. Nothing is stored by a refused call.
    for method in ("GET", "POST"):
        response = client.request(method, URL)
        assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
        response = client.request(method, URL, headers=bearer("sys-42"))
        assert (response.status_code, response.json()) == (403, FORBIDDEN)
    response = client.request("POST", URL, headers=bearer("viewer-1"))
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    for actor in ("viewer-1", "rev-1"):
        response = get(client, URL, actor=actor)
        assert (response.status_code, response.json()) == (200, [])  # a bare array, empty
    assert stored_runs(app_engine) == []

    # REVIEWER POST: 201 with the stored run, exactly the twelve columns. One MANUAL row is
    # added and no ledger entry.
    first = post_run(client, monkeypatch, WALL)
    assert set(first) == RUN_KEYS
    assert first["run_id"].startswith("lvr-")
    assert first["trigger"] == "MANUAL"
    assert first["started_at"] == first["finished_at"] == "2026-11-12T03:00:00Z"
    assert (first["valid"], first["entries_checked"], first["first_mismatch"]) == (
        True,
        len(entries),
        None,
    )
    assert (first["head_seq"], first["head_hash"]) == (entries[-1]["seq"], entries[-1]["hash"])
    assert first["head_check"] == "FIRST_RUN"
    assert (first["projection_mismatches"], first["report_mismatches"]) == ([], [])
    assert stored_runs(app_engine) == [{"run_id": first["run_id"], "trigger": "MANUAL"}]
    assert ledger(owner_engine) == entries

    # A MANUAL run is never skipped: a second one on the same day is stored too.
    second = post_run(client, monkeypatch, WALL + timedelta(minutes=1, microseconds=500000))
    assert second["started_at"] == "2026-11-12T03:01:00.5Z"
    assert API_TIME.fullmatch(second["finished_at"])
    assert second["head_check"] == "OK"
    assert len(stored_runs(app_engine)) == 2
    assert ledger(owner_engine) == entries

    # GET: newest first by started_at, each item the same object the POST returned.
    for actor in ("viewer-1", "rev-1"):
        response = get(client, URL, actor=actor)
        assert (response.status_code, response.json()) == (200, [second, first])

    # A failing MANUAL run is still 201, and logs the single LEDGER_INTEGRITY line of M4.
    with trigger_disabled(owner_engine) as conn:
        conn.execute(text("DELETE FROM ledger_entry WHERE seq = :seq"), {"seq": entries[-1]["seq"]})
    with caplog.at_level(logging.INFO):
        failing = post_run(client, monkeypatch, WALL + timedelta(minutes=2))
    assert (failing["valid"], failing["head_check"]) == (True, "REGRESSED")
    assert failing["projection_mismatches"] == []
    lines = [r for r in caplog.records if "alert: code=LEDGER_INTEGRITY" in r.getMessage()]
    assert [(r.levelno, failing["run_id"] in r.getMessage()) for r in lines] == [
        (logging.ERROR, True)
    ]

    # limit: default 10, at most 100, newest first. Twelve runs in all.
    for minute in range(3, 12):
        post_run(client, monkeypatch, WALL + timedelta(minutes=minute))
    assert len(stored_runs(app_engine)) == 12
    newest_first = get(client, f"{URL}?limit=100", actor="viewer-1").json()
    assert len(newest_first) == 12
    assert [run["started_at"] for run in newest_first] == sorted(
        (run["started_at"] for run in newest_first), reverse=True
    )
    assert newest_first[-2:] == [second, first]
    assert get(client, URL, actor="viewer-1").json() == newest_first[:10]
    assert get(client, f"{URL}?limit=1", actor="rev-1").json() == newest_first[:1]
    for bad in ("0", "101", "abc", "", "-1", "1.5"):
        response = get(client, f"{URL}?limit={bad}", actor="viewer-1")
        assert (response.status_code, response.json()) == (400, MALFORMED_QUERY), bad
    response = get(client, f"{URL}?limit=1&limit=2", actor="viewer-1")
    assert (response.status_code, response.json()) == (400, MALFORMED_QUERY)

    # GET /v1/ledger/verify is unchanged: exactly its three fields (Part 2 addendum rule 6).
    response = get(client, "/v1/ledger/verify", actor="viewer-1")
    assert response.status_code == 200
    assert set(response.json()) == {"valid", "entries_checked", "first_mismatch"}
