"""The standing tester and system interface (Cross-team Testing Interface v2: "Required calls" and
"Execution rules"), checked against app/harness.py and app/api/harness_routes.py through the
ASGI app (fix round item 1(e)).

Error bodies are {message, source}; source is tester, system, communication or unknown.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Any

import pytest

from app import harness, platform
from tests.api.conftest import ApiClient
from tests.platform5.harness_asgi import SELF_BASE, harness_client, target_keys

__all__ = ["harness_client"]

SOURCES = {"tester", "system", "communication", "unknown"}
OUTCOMES = {"pass", "fail", "error", "skip"}
OTHER = "http://other-team.pod-h.test/system"


def _error(response: Any, status: int, source: str) -> str:
    assert response.status_code == status, response.text
    body = response.json()
    assert set(body) == {"message", "source"}, body
    assert isinstance(body["message"], str) and body["message"].strip(), body
    assert body["source"] == source, body
    return str(body["message"])


def _setup(client: ApiClient, target: str = OTHER) -> str:
    response = client.request(
        "POST", "/tester/test/setup", json={"target_url": target, **target_keys()}
    )
    assert response.status_code == 201, response.text
    assert set(response.json()) == {"session_id"}
    return str(response.json()["session_id"])


def _run(client: ApiClient, session_id: str) -> Any:
    return client.request("POST", "/tester/test/run", json={"session_id": session_id})


def _report(client: ApiClient, session_id: str) -> Any:
    return client.get(f"/tester/test/report?session_id={session_id}")


def _assert_well_formed(report: dict[str, Any], planned: list[str]) -> None:
    """Every planned test exactly once, unique nonempty ids, nonempty messages, known outcomes,
    a source on every error; code, component and system always present and well formed."""
    ids = [row["test_id"] for row in report["results"]]
    assert ids == planned and len(set(ids)) == len(ids)
    for row in report["results"]:
        assert isinstance(row["test_id"], str) and row["test_id"]
        assert isinstance(row["message"], str) and row["message"].strip(), row
        assert row["outcome"] in OUTCOMES, row
        if row["outcome"] == "error":
            assert row.get("source") in SOURCES, row
    assert {"code", "component", "system"} <= set(report["coverage"])
    for entry in report["coverage"].values():
        assert set(entry) == {"covered", "total", "basis"} and entry["basis"].strip()
        if entry["covered"] is None or entry["total"] is None:
            assert entry["covered"] is None and entry["total"] is None
        else:
            assert 0 <= entry["covered"] <= entry["total"]


# --- Tester POST /test/setup ----------------------------------------------------------------


def test_tester_setup_rejects_an_invalid_request_and_creates_no_session(
    harness_client: ApiClient,
) -> None:
    before = len(harness._tester_sessions)
    _error(harness_client.request("POST", "/tester/test/setup", json={}), 400, "tester")
    bad = {"target_url": "ftp://other-team.pod-h.test/system"}
    _error(harness_client.request("POST", "/tester/test/setup", json=bad), 400, "tester")
    malformed = harness_client.request("POST", "/tester/test/setup", content=b"{not json")
    assert malformed.status_code == 400 and malformed.json()["source"] in SOURCES
    assert len(harness._tester_sessions) == before


@pytest.mark.parametrize(
    ("status", "body"),
    [
        (500, '{"message":"Could not initialize the session database.","source":"system"}'),
        (409, '{"message":"session_id already exists","source":"system"}'),
    ],
)
def test_a_system_setup_error_is_returned_unchanged_and_no_session_is_made(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch, status: int, body: str
) -> None:
    real = harness._http

    def target(method: str, url: str, *args: Any, **kwargs: Any) -> tuple[int, str]:
        if url.endswith("/system/test/setup"):
            return status, body
        return real(method, url, *args, **kwargs)

    monkeypatch.setattr(harness, "_http", target)
    before = len(harness._tester_sessions)
    response = harness_client.request("POST", "/tester/test/setup", json={"target_url": OTHER})
    message = _error(response, status, "system")
    assert message == (
        "Could not initialize the session database."
        if status == 500
        else "session_id already exists"
    )
    assert len(harness._tester_sessions) == before


def test_no_response_from_system_setup_is_a_communication_error(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(harness, "_http", lambda *args, **kwargs: (0, "TimeoutError: timed out"))
    before = len(harness._tester_sessions)
    response = harness_client.request("POST", "/tester/test/setup", json={"target_url": OTHER})
    _error(response, 500, "communication")
    assert len(harness._tester_sessions) == before


# --- System POST /test/setup and GET /test/report ------------------------------------------


def test_system_setup_and_report_follow_the_interface(harness_client: ApiClient) -> None:
    session_id = f"staff-{uuid.uuid4()}"
    setup = harness_client.request(
        "POST", "/system/test/setup", json={"session_id": session_id, "setup": {}}
    )
    assert (setup.status_code, setup.json()) == (201, {"session_id": session_id})
    again = harness_client.request("POST", "/system/test/setup", json={"session_id": session_id})
    _error(again, 409, "system")
    _error(harness_client.request("POST", "/system/test/setup", json={}), 400, "system")

    report = harness_client.get(f"/system/test/report?session_id={session_id}")
    assert report.status_code == 200
    assert report.json() == {
        "session_id": session_id,
        "coverage": {
            "code": {
                "covered": None,
                "total": None,
                "basis": "Source lines; target instrumentation unavailable",
            }
        },
    }
    _error(harness_client.get("/system/test/report?session_id=nope"), 404, "system")
    _error(harness_client.get("/system/test/report"), 400, "system")

    harness._system_sessions[session_id].status = "preparing"
    try:
        _error(harness_client.get(f"/system/test/report?session_id={session_id}"), 409, "system")
    finally:
        harness._system_sessions[session_id].status = "ready"


def test_a_failed_system_preparation_is_500_source_system_and_leaves_no_session(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    session_id = f"staff-{uuid.uuid4()}"

    def down() -> None:
        raise RuntimeError("database unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(platform, "reset", down)
        response = harness_client.request(
            "POST", "/system/test/setup", json={"session_id": session_id}
        )
        _error(response, 500, "system")
        assert session_id not in harness._system_sessions
    # The endpoint stays reachable while the resource is down, and works once it is back.
    retry = harness_client.request("POST", "/system/test/setup", json={"session_id": session_id})
    assert retry.status_code == 201


# --- Tester POST /test/run and GET /test/report ---------------------------------------------


def test_one_run_per_session_and_the_stored_report_is_the_run_report(
    harness_client: ApiClient,
) -> None:
    _error(harness_client.request("POST", "/tester/test/run", json={}), 400, "tester")
    _error(_run(harness_client, "never-issued"), 404, "tester")
    _error(_report(harness_client, "never-issued"), 404, "tester")
    session_id = _setup(harness_client)
    _error(_report(harness_client, session_id), 409, "tester")
    run = _run(harness_client, session_id)
    assert run.status_code == 200, run.text
    _assert_well_formed(run.json(), [f"T-0{n}" for n in range(1, 9)])
    _error(_run(harness_client, session_id), 409, "tester")
    stored = _report(harness_client, session_id)
    assert (stored.status_code, stored.json()) == (200, run.json())


def test_failed_tests_and_system_errors_are_in_a_complete_200_report(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = harness._http

    def target(method: str, url: str, *args: Any, **kwargs: Any) -> tuple[int, str]:
        if "/v1/events" in url:
            return 418, ""
        if "/test/report" in url:
            return 500, '{"message":"boom","source":"system"}'
        return real(method, url, *args, **kwargs)

    session_id = _setup(harness_client)
    monkeypatch.setattr(harness, "_http", target)
    run = _run(harness_client, session_id)
    assert run.status_code == 200, run.text
    outcomes = {row["test_id"]: row for row in run.json()["results"]}
    assert outcomes["T-04"]["outcome"] == outcomes["T-05"]["outcome"] == "fail"
    assert (outcomes["T-08"]["outcome"], outcomes["T-08"]["source"]) == ("error", "system")
    _assert_well_formed(run.json(), [f"T-0{n}" for n in range(1, 9)])


def test_an_aborted_run_keeps_the_results_it_has(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Interface: on abort, 500 with source tester; the stored report keeps the recorded
    outcomes, marks the test the abort prevented as error (source tester) and the unexecuted
    tests as skip; the session cannot be run again."""
    session_id = _setup(harness_client, f"{SELF_BASE}/system")

    def crash(*args: Any) -> dict[str, Any]:
        raise RuntimeError("the tester crashed")

    monkeypatch.setattr(harness, "_probe_events_list", crash)
    run = _run(harness_client, session_id)
    _error(run, 500, "tester")
    stored = _report(harness_client, session_id)
    assert stored.status_code == 200
    planned = [t["test_id"] for t in harness._planned_tests(True)]
    _assert_well_formed(stored.json(), planned)
    rows = {row["test_id"]: row for row in stored.json()["results"]}
    assert [rows[t]["outcome"] for t in ("T-01", "T-02", "T-03")] == ["pass"] * 3
    assert (rows["T-04"]["outcome"], rows["T-04"]["source"]) == ("error", "tester")
    assert {rows[t]["outcome"] for t in planned[4:]} == {"skip"}
    _error(_run(harness_client, session_id), 409, "tester")


class _SlowReadySession(harness.TesterSession):
    """Reading a "ready" status takes a moment, so two callers that both check before either
    marks the session running are both let through unless the check and the mark are atomic."""

    def __getattribute__(self, name: str) -> Any:
        value = object.__getattribute__(self, name)
        if name == "status" and value == "ready":
            time.sleep(0.3)
        return value


def test_two_concurrent_runs_of_one_session_get_one_409(monkeypatch: pytest.MonkeyPatch) -> None:
    session_id = f"race-{uuid.uuid4()}"
    session = _SlowReadySession(session_id=session_id, target_url=OTHER)
    monkeypatch.setitem(harness._tester_sessions, session_id, session)
    executed: list[str] = []
    monkeypatch.setattr(harness, "_execute_tester", lambda s: executed.append(s.session_id))
    statuses: list[int] = []

    def run() -> None:
        try:
            harness.tester_run(session_id)
            statuses.append(200)
        except harness.HarnessError as exc:
            statuses.append(exc.status)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(statuses) == [200, 409]
    assert executed == [session_id]
