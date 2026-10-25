"""Part 1: request log lines, counters and threshold-evaluation logging (spec 1.8;
decision 126)."""

import json
import logging
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import observability
from app.platform import with_phase4_tables
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.part1.conftest import get, metric, post, post_raw

LOG_KEYS = {"ts", "level", "request_id", "actor_id", "method", "path", "status", "duration_ms"}


def request_lines(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [json.loads(r.getMessage()) for r in caplog.records if r.name == "api.request"]


def test_one_json_log_line_per_request(client: ApiClient, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="api.request")
    event_id = post(client).json()["flagged_event_id"]
    get(client, f"/v1/events/{event_id}?ignored=1")
    get(client, "/v1/events", "rev-1")
    client.get("/v1/no-such-route")

    lines = request_lines(caplog)
    assert len(lines) == 4
    assert all(set(line) >= LOG_KEYS for line in lines)
    assert len({line["request_id"] for line in lines}) == 4
    ingest, detail, listing, unknown = lines
    assert {k: ingest[k] for k in ("ts", "level", "actor_id", "method", "path", "status")} == {
        "ts": "2026-09-27T10:05:00Z",  # clock.now()
        "level": "INFO",
        "actor_id": "sys-42",
        "method": "POST",
        "path": "/v1/metrics",
        "status": 201,
    }
    assert ingest["event_id"] == event_id
    assert isinstance(ingest["duration_ms"], int) and ingest["duration_ms"] >= 0
    assert (detail["actor_id"], detail["path"], detail["status"], detail["event_id"]) == (
        "viewer-1",
        f"/v1/events/{event_id}",
        200,
        event_id,
    )
    assert (listing["actor_id"], listing["status"]) == ("rev-1", 200)
    assert "event_id" not in listing
    assert (unknown["actor_id"], unknown["status"], unknown["error_code"]) == (
        None,
        404,
        "NOT_FOUND",
    )


def test_counters(client: ApiClient) -> None:
    observability.reset_counters()
    post(client)  # breach
    post(client, timestamp="2026-09-27T10:10:00Z")  # series join
    post(client, timestamp="2026-09-27T10:10:00Z")  # duplicate
    post(client, value=0.10, timestamp="2026-09-27T10:15:00Z")  # healthy
    post_raw(client, "not json")  # 400
    client.request("POST", "/v1/metrics", content="{}")  # 401
    post(client, actor="viewer-1")  # 403
    get(client, "/v1/events/evt-none")  # 404
    assert observability.counter_values() == {
        "samples_accepted": 3,
        "duplicates": 1,
        "breaches": 1,
        "series_joins": 1,
        "4xx:MALFORMED_BODY": 1,
        "4xx:MISSING_OR_INVALID_KEY": 1,
        "4xx:FORBIDDEN_ROLE": 1,
        "4xx:NOT_FOUND": 1,
    }


def test_threshold_evaluation_debug_line(
    client: ApiClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG, logger="api.detection")
    post(client)
    healthy = json.dumps(metric(timestamp="2026-09-27T10:10:00Z", value="V"))
    post_raw(client, healthy.replace('"V"', "0.10"))  # sent as 0.10, not 0.1
    post(client, metric_name="no_threshold", timestamp="2026-09-27T10:10:00Z")
    lines = [r.getMessage() for r in caplog.records if r.name == "api.detection"]
    assert len(lines) == 2  # no line for a metric with no threshold
    assert all(r.levelno == logging.DEBUG for r in caplog.records if r.name == "api.detection")
    for line, value, breached in ((lines[0], "0.34", "True"), (lines[1], "0.10", "False")):
        assert "threshold_version_id=thr-1" in line
        assert "comparator=gt" in line
        assert "limit=0.30" in line
        assert f"value={value}" in line
        assert f"breached={breached}" in line


def test_request_log_never_contains_the_authorization_header(
    client: ApiClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    headers = bearer("rev-1")
    client.get("/v1/events", headers=headers)
    assert headers["Authorization"].split()[1] not in caplog.text


def test_a_stub_501_is_logged_at_info_and_a_500_at_error(
    client: ApiClient, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="api.request")
    client.request("POST", "/v1/events/evt-x/disposition", headers=bearer("rev-1"))
    with owner_engine.begin() as conn:
        if "harm_context" in with_phase4_tables(conn):  # its system_id references the system
            conn.execute(text("DELETE FROM harm_context"))
        conn.execute(text("DELETE FROM monitored_system"))
    post(client)  # a valid key for a system with no row: 500 (spec 1.4 step 4)
    stub, failure = request_lines(caplog)
    assert (stub["status"], stub["level"], stub["error_code"]) == (501, "INFO", "NOT_IMPLEMENTED")
    assert (failure["status"], failure["level"], failure["error_code"]) == (
        500,
        "ERROR",
        "INTERNAL_ERROR",
    )
