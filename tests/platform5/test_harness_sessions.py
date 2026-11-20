"""Fix round 2, D-1: one active system session at a time (Cross-team Testing Interface,
"Session state and concurrency": teams "must prevent unintended interference between tests and
sessions"; "systems allowing only one active session at a time may return 409 for conflicting
setup requests").

A system session is active from its setup until it ends: at once when a tester run of the same
session id finishes or aborts (a self-run), otherwise when SESSION_IDLE_SECONDS pass with no
activity (its setup, its system report reads, its tester run events). While one is active,
POST /system/test/setup for another id is 409 with source "system" and changes nothing. The
window is checked with clock.wall_now(), which these tests move; nothing sleeps.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import Any

import pytest

from app import clock, config, harness
from tests.api.conftest import ApiClient
from tests.platform5.harness_asgi import SELF_BASE, harness_client, target_keys

__all__ = ["harness_client"]

DEFAULT_IDLE_SECONDS = 600


class WallClock:
    """clock.wall_now(), moved by hand."""

    def __init__(self) -> None:
        self.now = clock.wall_now()

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


@pytest.fixture
def wall(monkeypatch: pytest.MonkeyPatch) -> Iterator[WallClock]:
    fake = WallClock()
    monkeypatch.setattr(clock, "wall_now", fake)
    yield fake


def _system_setup(client: ApiClient, session_id: str) -> Any:
    return client.request("POST", "/system/test/setup", json={"session_id": session_id})


def _new_id() -> str:
    return f"staff-{uuid.uuid4()}"


def _error(response: Any, status: int, source: str) -> str:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"message", "source"} and body["source"] == source, body
    assert isinstance(body["message"], str) and body["message"].strip(), body
    return str(body["message"])


def _reset_count(client: ApiClient) -> int:
    return int(client.get("/healthz").json()["reset_count"])


def _breach(client: ApiClient) -> str:
    """One breach through the project API; returns the flagged event id."""
    simulated = client.request(
        "POST",
        "/v1/test/clock/advance",
        headers={"Authorization": f"Bearer {config.reviewer_key()}"},
        json={"seconds": 0},
    ).json()["simulated_time"]
    sample = {
        "system_id": "sys-42",
        "metric_name": "accuracy",
        "metric_type": "performance",
        "value": 0.61,
        "timestamp": str(simulated)[:19] + "Z",
    }
    response = client.request(
        "POST",
        "/v1/metrics",
        headers={"Authorization": f"Bearer {config.ingest_key_sys_42()}"},
        json=sample,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["flagged_event_id"])


def _event_status(client: ApiClient, event_id: str) -> int:
    response = client.get(
        f"/v1/events/{event_id}", headers={"Authorization": f"Bearer {config.viewer_key()}"}
    )
    return int(response.status_code)


def test_the_idle_window_is_configurable_with_a_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SESSION_IDLE_SECONDS", raising=False)
    assert config.session_idle_seconds() == DEFAULT_IDLE_SECONDS
    monkeypatch.setenv("SESSION_IDLE_SECONDS", "45")
    assert config.session_idle_seconds() == 45


def test_a_second_setup_while_a_session_is_active_is_409_and_does_not_reset(
    harness_client: ApiClient, wall: WallClock
) -> None:
    assert _system_setup(harness_client, _new_id()).status_code == 201
    resets = _reset_count(harness_client)
    other = _new_id()
    message = _error(_system_setup(harness_client, other), 409, "system")
    assert "active" in message
    assert _reset_count(harness_client) == resets
    assert other not in harness._system_sessions


def test_a_rejected_setup_leaves_the_active_session_data_intact(
    harness_client: ApiClient, wall: WallClock
) -> None:
    assert _system_setup(harness_client, _new_id()).status_code == 201
    event_id = _breach(harness_client)
    assert _event_status(harness_client, event_id) == 200
    _error(_system_setup(harness_client, _new_id()), 409, "system")
    assert _event_status(harness_client, event_id) == 200


def test_the_tester_setup_forwards_the_409_and_creates_no_session(
    harness_client: ApiClient, wall: WallClock
) -> None:
    assert _system_setup(harness_client, _new_id()).status_code == 201
    before = len(harness._tester_sessions)
    response = harness_client.request(
        "POST", "/tester/test/setup", json={"target_url": f"{SELF_BASE}/system", **target_keys()}
    )
    _error(response, 409, "system")
    assert len(harness._tester_sessions) == before


def test_setup_after_the_self_run_finishes_is_201(
    harness_client: ApiClient, wall: WallClock
) -> None:
    setup = harness_client.request(
        "POST", "/tester/test/setup", json={"target_url": f"{SELF_BASE}/system", **target_keys()}
    )
    assert setup.status_code == 201, setup.text
    session_id = setup.json()["session_id"]
    _error(_system_setup(harness_client, _new_id()), 409, "system")
    run = harness_client.request("POST", "/tester/test/run", json={"session_id": session_id})
    assert run.status_code == 200, run.text
    assert _system_setup(harness_client, _new_id()).status_code == 201


def test_setup_after_an_aborted_self_run_is_201(
    harness_client: ApiClient, wall: WallClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    setup = harness_client.request(
        "POST", "/tester/test/setup", json={"target_url": f"{SELF_BASE}/system", **target_keys()}
    )
    session_id = setup.json()["session_id"]

    def crash(*args: Any) -> dict[str, Any]:
        raise RuntimeError("the tester crashed")

    monkeypatch.setattr(harness, "_probe_events_list", crash)
    run = harness_client.request("POST", "/tester/test/run", json={"session_id": session_id})
    _error(run, 500, "tester")
    assert _system_setup(harness_client, _new_id()).status_code == 201


def test_setup_after_the_idle_window_expires_is_201(
    harness_client: ApiClient, wall: WallClock
) -> None:
    window = config.session_idle_seconds()
    assert _system_setup(harness_client, _new_id()).status_code == 201
    wall.advance(window - 1)
    _error(_system_setup(harness_client, _new_id()), 409, "system")
    wall.advance(2)
    assert _system_setup(harness_client, _new_id()).status_code == 201


def test_a_report_read_refreshes_the_window(harness_client: ApiClient, wall: WallClock) -> None:
    window = config.session_idle_seconds()
    first = _new_id()
    assert _system_setup(harness_client, first).status_code == 201
    wall.advance(window - 1)
    assert harness_client.get(f"/system/test/report?session_id={first}").status_code == 200
    wall.advance(window - 1)
    _error(_system_setup(harness_client, _new_id()), 409, "system")
    wall.advance(2)
    assert _system_setup(harness_client, _new_id()).status_code == 201


def test_the_same_id_is_still_409_after_its_session_ended(
    harness_client: ApiClient, wall: WallClock
) -> None:
    first = _new_id()
    assert _system_setup(harness_client, first).status_code == 201
    message = _error(_system_setup(harness_client, first), 409, "system")
    assert message == "session_id already exists"
    wall.advance(config.session_idle_seconds() + 1)
    message = _error(_system_setup(harness_client, first), 409, "system")
    assert message == "session_id already exists"


def test_the_window_follows_session_idle_seconds(
    harness_client: ApiClient, wall: WallClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SESSION_IDLE_SECONDS", "30")
    assert _system_setup(harness_client, _new_id()).status_code == 201
    wall.advance(29)
    _error(_system_setup(harness_client, _new_id()), 409, "system")
    wall.advance(2)
    assert _system_setup(harness_client, _new_id()).status_code == 201
