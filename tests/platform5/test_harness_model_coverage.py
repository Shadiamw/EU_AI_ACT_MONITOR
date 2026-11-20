"""coverage.model in the tester's suite report (cross-team testing interface, Phases 5 and 6):
present only for a same-team target, counting the Phase 5 design requirements the suite
evaluated out of all of them, with a basis naming the design version and the mapping."""

import re
from pathlib import Path
from typing import Any

import pytest

from app import harness, harness_model

MAPPING = Path(__file__).resolve().parents[2] / "docs" / "models" / "phase5_design_requirements.md"
BASE_IDS = [f"T-0{n}" for n in range(1, 9)]
MODEL_IDS = [test_id for test_id, _ in harness_model.MODEL_TESTS]


def _results(outcome: str, ids: list[str]) -> list[dict[str, Any]]:
    return [{"test_id": test_id, "outcome": outcome, "message": "m"} for test_id in ids]


def test_a_cross_team_plan_and_report_have_no_model_coverage() -> None:
    assert [t["test_id"] for t in harness._planned_tests(False)] == BASE_IDS
    coverage = harness._coverage_from_results(_results("pass", BASE_IDS), None, False)
    assert set(coverage) == {"code", "component", "system"}
    session = harness.TesterSession(session_id="s", target_url="https://other.example/system")
    assert "model" not in session.report()["coverage"]


def test_a_same_team_plan_runs_the_probes_before_the_coverage_pull() -> None:
    planned = [t["test_id"] for t in harness._planned_tests(True)]
    assert planned == [*BASE_IDS[:-1], *MODEL_IDS, "T-08"]
    assert len(planned) == len(set(planned))


def test_model_coverage_counts_evaluated_requirements_once() -> None:
    results = [*_results("pass", BASE_IDS), *_results("pass", MODEL_IDS)]
    coverage = harness._coverage_from_results(results, None, True)
    model = coverage["model"]
    assert (model["covered"], model["total"]) == (13, 26)
    assert harness_model.DESIGN_VERSION in model["basis"]
    assert harness_model.MAPPING_URL in model["basis"]
    # Components and criteria are unchanged by the probes.
    assert coverage["system"]["covered"] == len(harness._CRITERIA)


def test_errors_and_skips_do_not_count_and_a_fail_does() -> None:
    results = [
        *_results("error", ["M-01"]),
        *_results("skip", ["M-02"]),
        *_results("fail", ["M-06"]),  # DR-03 and DR-20
        *_results("pass", ["M-07"]),  # DR-03 again
    ]
    model = harness_model.model_coverage(results)
    assert (model["covered"], model["total"]) == (2, 26)
    assert harness_model.model_coverage([])["covered"] == 0


def test_same_team_is_the_self_target_or_a_listed_url(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SUITE_BASE_URL", "https://ours.example")
    monkeypatch.delenv("SAME_TEAM_SYSTEM_URLS", raising=False)
    assert harness._is_same_team("https://ours.example/system")
    assert not harness._is_same_team("https://theirs.example/system")
    monkeypatch.setenv("SAME_TEAM_SYSTEM_URLS", "https://staging.ours.example/system/ , x")
    assert harness._is_same_team("https://staging.ours.example/system")
    assert not harness._is_same_team("https://theirs.example/system")


def test_an_aborted_same_team_run_lists_every_planned_test_once() -> None:
    session = harness.TesterSession(session_id="s", target_url="t", same_team=True)
    session.results = _results("pass", ["T-01", "T-02"])
    harness._mark_abort(session, "boom")
    ids = [row["test_id"] for row in session.results]
    assert ids == [t["test_id"] for t in harness._planned_tests(True)]
    assert session.coverage["model"]["covered"] == 0


def test_the_mapping_document_matches_the_code() -> None:
    text = MAPPING.read_text(encoding="utf-8")
    rows = re.findall(r"^\| (DR-\d\d) \| (.+?) \| [^|]+ \| ([^|]+) \|", text, re.MULTILINE)
    assert [dr for dr, _, _ in rows] == [dr for dr, _ in harness_model.DESIGN_REQUIREMENTS]
    for (dr, name, probes), (_, listed) in zip(
        rows, harness_model.DESIGN_REQUIREMENTS, strict=True
    ):
        assert name.replace("`", "") == listed, dr
        documented = set(re.findall(r"M-\d\d", probes))
        mapped = {test_id for test_id, drs in harness_model.MODEL_TESTS if dr in drs}
        assert documented == mapped, dr
