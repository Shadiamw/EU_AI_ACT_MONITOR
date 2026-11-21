"""Fix round 2, D-3: the tester validates a target's coverage.code (Cross-team Testing
Interface, "Coverage fields": covered, total and a nonempty basis; nonnegative integer counts with
covered no greater than total; null for both counts when a measure is unavailable, never zero).

A valid entry is copied unchanged. Anything else becomes covered null, total null and a basis that
says the target's coverage.code was invalid and why, without the target's payload.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from app import harness
from tests.api.conftest import ApiClient
from tests.platform5.harness_asgi import harness_client, target_keys

__all__ = ["harness_client"]

OTHER = "http://other-team.pod-h.test/system"
SECRET = "do-not-copy-9f3e"

INVALID: list[tuple[str, Any]] = [
    ("covered-exceeds-total", {"covered": 11, "total": 10, "basis": f"Lines {SECRET}"}),
    ("negative-covered", {"covered": -1, "total": 10, "basis": f"Lines {SECRET}"}),
    ("negative-total", {"covered": 0, "total": -5, "basis": f"Lines {SECRET}"}),
    ("string-count", {"covered": "7", "total": 10, "basis": f"Lines {SECRET}"}),
    ("float-count", {"covered": 7.0, "total": 10, "basis": f"Lines {SECRET}"}),
    ("boolean-count", {"covered": True, "total": 10, "basis": f"Lines {SECRET}"}),
    ("one-count-null", {"covered": None, "total": 10, "basis": f"Lines {SECRET}"}),
    ("missing-count", {"total": 10, "basis": f"Lines {SECRET}"}),
    ("missing-basis", {"covered": 3, "total": 10}),
    ("empty-basis", {"covered": 3, "total": 10, "basis": "   "}),
    ("basis-not-a-string", {"covered": 3, "total": 10, "basis": [SECRET]}),
    ("wrong-type-list", [3, 10, SECRET]),
    ("wrong-type-string", f"3/10 {SECRET}"),
]

VALID: list[tuple[str, dict[str, Any]]] = [
    ("measured", {"covered": 812, "total": 1203, "basis": "Lines of app/, coverage.py"}),
    ("full", {"covered": 10, "total": 10, "basis": "Branches of app/"}),
    ("none-covered", {"covered": 0, "total": 10, "basis": "Lines of app/"}),
    ("unavailable", {"covered": None, "total": None, "basis": "Lines; not instrumented"}),
    ("zero-of-zero", {"covered": 0, "total": 0, "basis": f"Not measured {SECRET}"}),
]


def _assert_replaced(entry: dict[str, Any]) -> None:
    assert set(entry) == {"covered", "total", "basis"}, entry
    assert entry["covered"] is None and entry["total"] is None, entry
    basis = entry["basis"]
    assert isinstance(basis, str) and "coverage.code was invalid" in basis, basis
    assert basis.split("coverage.code was invalid", 1)[1].strip(" :;.()"), basis
    assert SECRET not in basis, basis


@pytest.mark.parametrize(("name", "code"), INVALID, ids=[name for name, _ in INVALID])
def test_an_invalid_coverage_code_becomes_null_with_a_reason(name: str, code: Any) -> None:
    _assert_replaced(harness._checked_code(code))


@pytest.mark.parametrize(("name", "code"), VALID, ids=[name for name, _ in VALID])
def test_a_valid_coverage_code_is_copied_unchanged(name: str, code: dict[str, Any]) -> None:
    assert harness._checked_code(dict(code)) == code


def _run_against(client: ApiClient, monkeypatch: pytest.MonkeyPatch, code: Any) -> dict[str, Any]:
    """A cross-team run whose target answers /test/report with this coverage.code."""
    real = harness._http

    def target(method: str, url: str, *args: Any, **kwargs: Any) -> tuple[int, str]:
        if "/test/report" in url:
            return 200, json.dumps({"session_id": "s", "coverage": {"code": code}})
        return real(method, url, *args, **kwargs)

    setup = client.request(
        "POST", "/tester/test/setup", json={"target_url": OTHER, **target_keys()}
    )
    assert setup.status_code == 201, setup.text
    session_id = setup.json()["session_id"]
    monkeypatch.setattr(harness, "_http", target)
    run = client.request("POST", "/tester/test/run", json={"session_id": session_id})
    assert run.status_code == 200, run.text
    stored = client.get(f"/tester/test/report?session_id={session_id}")
    assert stored.status_code == 200 and stored.json() == run.json()
    report: dict[str, Any] = run.json()
    return report


def test_an_invalid_target_entry_is_null_with_a_reason_in_the_report(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_against(harness_client, monkeypatch, dict(INVALID[0][1]))
    _assert_replaced(report["coverage"]["code"])


def test_a_valid_target_entry_is_copied_into_the_report_unchanged(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    report = _run_against(harness_client, monkeypatch, dict(VALID[0][1]))
    assert report["coverage"]["code"] == VALID[0][1]
