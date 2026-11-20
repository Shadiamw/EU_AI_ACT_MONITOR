"""coverage.model against the real app (Cross-team Testing Interface v2, "Coverage fields"; Part 4
decisions 325 to 329; audit F-06, fix round item 1(b)).

Every design-requirement probe M-01 to M-12 (docs/models/phase5_design_requirements.md) runs
against the ASGI app and the real database through tests/platform5/harness_asgi.py, and must
pass; a probe whose expectation were wrong would end "fail" and these tests would fail. The rest
checks the interface's rules for the model entry: present only for the own system (R1), total =
the listed requirements and covered = the requirements a probe evaluated (R2), well-formed
counts and null rather than zero (R3), pass and fail evaluate while error and skip do not (R6),
and the stored report equals the run's report (R5).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app import harness, harness_model
from tests.api.conftest import ApiClient
from tests.platform5.harness_asgi import SELF_BASE, harness_client, target_keys

__all__ = ["harness_client"]

MAPPING = Path(__file__).resolve().parents[2] / "docs" / "models" / "phase5_design_requirements.md"
BASE_IDS = [f"T-0{n}" for n in range(1, 9)]
MODEL_IDS = [f"M-{n:02d}" for n in range(1, 13)]
PROJECT = "http://127.0.0.1:8000"

# What each probe returns when its expectation holds (app/harness_model.py, Probes.m01 to m12).
PASS_MESSAGES = {
    "M-01": "All 8 contract-delta routes answer 401 without a key.",
    "M-02": "Report before a disposition is 409 NO_DISPOSITION.",
    "M-03": "Report 200 after a disposition, with every required key and ordered timestamps.",
    "M-04": "JSON and PDF match X-Report-Sha256 and are served identically twice.",
    "M-05": "Viewer and system keys get 403 on the report.",
    "M-06": "Submission 201 INITIAL; the report is listed as SUBMITTED.",
    "M-07": "Not-serious closure report 200; its submission is 409 NOT_SUBMITTABLE.",
    "M-08": "Erasure before retain_until is 409 RETENTION_NOT_EXPIRED.",
    "M-09": "After retain_until: erasure 200, report 410, evidence null, ledger still valid.",
    "M-10": "Manual verification run 201: chain valid, no event-table projection mismatch.",
    "M-11": "Ops dashboard 200 with version, worker and ledger sections.",
    "M-12": "/healthz reports version ",
}


def _probe_keys() -> dict[str, str]:
    keys = target_keys()
    return {
        "ingest": keys["ingest_key"],
        "reviewer": keys["reviewer_key"],
        "viewer": keys["viewer_key"],
    }


def _listed_requirements() -> list[str]:
    """The DR ids of the mapping document's table, counted from the document itself."""
    text = MAPPING.read_text(encoding="utf-8")
    return re.findall(r"^\| (DR-\d\d) \|", text, re.MULTILINE)


def _rows(outcome: str, ids: list[str]) -> list[dict[str, Any]]:
    return [{"test_id": test_id, "outcome": outcome, "message": "m"} for test_id in ids]


def _assert_entry(entry: dict[str, Any]) -> None:
    """Interface: covered, total and a nonempty basis; nonnegative integers with covered <=
    total, or null for both."""
    assert set(entry) == {"covered", "total", "basis"}, entry
    assert isinstance(entry["basis"], str) and entry["basis"].strip(), entry
    covered, total = entry["covered"], entry["total"]
    if covered is None or total is None:
        assert covered is None and total is None, entry
        return
    assert type(covered) is int and type(total) is int, entry
    assert 0 <= covered <= total, entry


def test_every_model_probe_passes_against_the_app(harness_client: ApiClient) -> None:
    results = harness_model.run_probes(PROJECT, _probe_keys())
    assert [row["test_id"] for row in results] == MODEL_IDS
    outcomes = {row["test_id"]: (row["outcome"], row["message"]) for row in results}
    assert {k: v[0] for k, v in outcomes.items()} == dict.fromkeys(MODEL_IDS, "pass"), outcomes
    for test_id, (_, message) in outcomes.items():
        assert message.startswith(PASS_MESSAGES[test_id]), (test_id, message)
    model = harness_model.model_coverage(results)
    assert (model["covered"], model["total"]) == (13, len(_listed_requirements()))


def test_a_wrong_answer_is_a_fail_and_a_prevented_probe_an_error(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A target that answers 500 to everything: each probe that can evaluate a response fails;
    a probe that needs an earlier probe's event could not be evaluated (error, source tester);
    none passes, and covered counts only the failed probes' requirements."""

    def broken(
        method: str, url: str, payload: Any = None, key: str = "", timeout: float = 20.0
    ) -> harness_model.Response:
        return harness_model.Response(500, {}, b"")

    monkeypatch.setattr(harness_model, "fetch", broken)
    results = harness_model.run_probes(PROJECT, _probe_keys())
    outcomes = {row["test_id"]: row["outcome"] for row in results}
    assert outcomes == {
        "M-01": "fail",
        "M-02": "fail",
        "M-03": "error",
        "M-04": "error",
        "M-05": "error",
        "M-06": "error",
        "M-07": "fail",
        "M-08": "error",
        "M-09": "error",
        "M-10": "fail",
        "M-11": "fail",
        "M-12": "fail",
    }
    assert {row.get("source") for row in results if row["outcome"] == "error"} == {"tester"}
    failed = {
        dr
        for test_id, drs in harness_model.MODEL_TESTS
        if outcomes[test_id] == "fail"
        for dr in drs
    }
    assert harness_model.model_coverage(results)["covered"] == len(failed) == 6


def test_no_response_is_a_communication_error_and_evaluates_nothing(
    harness_client: ApiClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def silent(
        method: str, url: str, payload: Any = None, key: str = "", timeout: float = 20.0
    ) -> harness_model.Response:
        return harness_model.Response(0, {}, b"TimeoutError: timed out")

    monkeypatch.setattr(harness_model, "fetch", silent)
    results = harness_model.run_probes(PROJECT, _probe_keys())
    assert all(row["outcome"] == "error" for row in results), results
    assert results[0]["source"] == "communication" and results[1]["source"] == "communication"
    model = harness_model.model_coverage(results)
    assert (model["covered"], model["total"]) == (0, 26)
    _assert_entry(model)


def test_a_self_run_reports_model_coverage_and_stores_the_same_report(
    harness_client: ApiClient,
) -> None:
    setup = harness_client.request(
        "POST", "/tester/test/setup", json={"target_url": f"{SELF_BASE}/system", **target_keys()}
    )
    assert setup.status_code == 201, setup.text
    session_id = setup.json()["session_id"]
    run = harness_client.request("POST", "/tester/test/run", json={"session_id": session_id})
    assert run.status_code == 200, run.text
    report = run.json()
    assert report["session_id"] == session_id
    ids = [row["test_id"] for row in report["results"]]
    assert ids == [*BASE_IDS[:-1], *MODEL_IDS, "T-08"]
    outcomes = {row["test_id"]: (row["outcome"], row["message"]) for row in report["results"]}
    assert {k: v[0] for k, v in outcomes.items()} == dict.fromkeys(ids, "pass"), outcomes
    coverage = report["coverage"]
    assert set(coverage) == {"code", "component", "system", "model"}
    for entry in coverage.values():
        _assert_entry(entry)
    assert (coverage["model"]["covered"], coverage["model"]["total"]) == (13, 26)
    assert coverage["code"] == {
        "covered": None,
        "total": None,
        "basis": "Source lines; target instrumentation unavailable",
    }
    stored = harness_client.get(f"/tester/test/report?session_id={session_id}")
    assert stored.status_code == 200
    assert stored.json() == report


def test_a_cross_team_run_has_no_probes_and_no_model_coverage(
    harness_client: ApiClient,
) -> None:
    """Another team's system (any URL that is neither the self target nor listed in
    SAME_TEAM_SYSTEM_URLS): the plan is T-01 to T-08 and coverage has no model entry, in the
    run's report and in the stored one."""
    setup = harness_client.request(
        "POST",
        "/tester/test/setup",
        json={"target_url": "http://other-team.pod-h.test/system", **target_keys()},
    )
    assert setup.status_code == 201, setup.text
    session_id = setup.json()["session_id"]
    run = harness_client.request("POST", "/tester/test/run", json={"session_id": session_id})
    assert run.status_code == 200, run.text
    report = run.json()
    assert [row["test_id"] for row in report["results"]] == BASE_IDS
    assert set(report["coverage"]) == {"code", "component", "system"}
    stored = harness_client.get(f"/tester/test/report?session_id={session_id}").json()
    assert "model" not in stored["coverage"] and stored == report


def test_total_is_the_number_of_listed_requirements_and_covered_never_exceeds_it() -> None:
    listed = _listed_requirements()
    assert len(listed) == len(set(listed)) == 26
    assert harness_model.model_coverage([])["total"] == len(listed)
    mapped = {dr for _, drs in harness_model.MODEL_TESTS for dr in drs}
    assert mapped <= set(listed)
    everything = _rows("pass", [*BASE_IDS, *MODEL_IDS, "M-99", "X"])
    assert harness_model.model_coverage(everything)["covered"] == len(mapped) == 13
    for outcome in ("pass", "fail", "error", "skip"):
        _assert_entry(harness_model.model_coverage(_rows(outcome, MODEL_IDS)))


def test_only_pass_and_fail_evaluate_a_requirement() -> None:
    for outcome, covered in (("pass", 13), ("fail", 13), ("error", 0), ("skip", 0)):
        assert harness_model.model_coverage(_rows(outcome, MODEL_IDS))["covered"] == covered


def test_basis_names_the_design_version_and_the_mapping_without_credentials() -> None:
    basis = harness_model.model_coverage([])["basis"]
    assert basis.strip()
    assert harness_model.DESIGN_VERSION in basis
    assert "docs/models/phase5_design_requirements.md" in basis
    for url in re.findall(r"https?://\S+", basis):
        assert "@" not in url.split("/")[2], url
    lowered = basis.lower()
    assert not any(word in lowered for word in ("bearer", "token", "password", "key="))


def test_an_unavailable_measure_is_null_with_a_reason_never_zero() -> None:
    coverage = harness._coverage_from_results([], None)
    assert coverage["code"]["covered"] is None and coverage["code"]["total"] is None
    assert coverage["code"]["basis"].strip()
    copied = harness._coverage_from_results(
        [], {"covered": None, "total": None, "basis": "Branches; not measured"}
    )
    assert copied["code"] == {"covered": None, "total": None, "basis": "Branches; not measured"}


def test_same_team_is_explicit_and_omits_the_model_when_in_doubt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("SUITE_BASE_URL", raising=False)
    monkeypatch.delenv("SAME_TEAM_SYSTEM_URLS", raising=False)
    fly = "https://eu-ai-act-monitor-api.fly.dev"
    assert harness._is_same_team(f"{fly}/system")
    for other in (
        "https://other-team.example/system",
        f"{fly}.other-team.example/system",
        f"{fly}/system/extra",
        "http://127.0.0.1:8000/system",
        "",
    ):
        assert not harness._is_same_team(other), other
    monkeypatch.setenv("SAME_TEAM_SYSTEM_URLS", " , ")
    assert not harness._is_same_team("https://other-team.example/system")
