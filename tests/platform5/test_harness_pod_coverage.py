"""coverage.system and coverage.component in the tester's suite report (Cross-team Testing
Interface, "Coverage fields"; follow-up round 2, SS's decision; audit F-07).

system: the acceptance criteria of the shared pod specification, AC-01 to AC-18 of the pod's
REQ/AC document, all in scope and all in the total. A criterion counts once, and only when a probe
performs that criterion's own Setup, Action and Expected through the target's public interface and
ends pass or fail.

component: the components of the system architecture (README 17.2). A component counts once, and
only when a probe sends a request that passes through it and ends pass or fail on that
component's own observable behaviour.

Error and skip never count, and 0 is a valid count.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from app import harness, harness_model

README = Path(__file__).resolve().parents[2] / "README.md"
BASE_IDS = [f"T-0{n}" for n in range(1, 9)]
MODEL_IDS = [test_id for test_id, _ in harness_model.MODEL_TESTS]

CRITERIA = tuple(f"AC-{n:02d}" for n in range(1, 19))
COMPONENTS = (
    "Ingestion API",
    "Rules engine",
    "Event service",
    "Gap sweeper",
    "Access control",
    "Config service",
    "Workflow engine",
    "Notification outbox",
    "Reporting service",
    "Audit ledger",
)

# The components each probe exercises (reasons in README 17.2). No probe performs the Setup,
# Action and Expected of any AC-01 to AC-18, so no probe lists a criterion.
EXPECTED_COMPONENTS: dict[str, tuple[str, ...]] = {
    "T-01": (),
    "T-02": ("Access control",),
    "T-03": (),
    "T-04": (),
    "T-05": (),
    "T-06": (),
    "T-07": (),
    "T-08": (),
    "M-01": ("Access control",),
    "M-02": ("Ingestion API", "Rules engine", "Event service", "Reporting service"),
    "M-03": ("Workflow engine", "Reporting service"),
    "M-04": ("Reporting service",),
    "M-05": ("Access control",),
    "M-06": ("Reporting service",),
    "M-07": (
        "Ingestion API",
        "Rules engine",
        "Event service",
        "Workflow engine",
        "Reporting service",
    ),
    "M-08": (),
    "M-09": ("Event service", "Reporting service", "Audit ledger"),
    "M-10": ("Audit ledger",),
    "M-11": (),
    "M-12": (),
}

SYSTEM_BASIS_START = "Acceptance criteria listed in the pod specification"
COMPONENT_BASIS = "Components of the system architecture (README 17.2)"


def _rows(outcome: str, ids: list[str]) -> list[dict[str, Any]]:
    return [{"test_id": test_id, "outcome": outcome, "message": "m"} for test_id in ids]


def _counts(entry: dict[str, Any]) -> tuple[Any, Any]:
    return entry["covered"], entry["total"]


def test_the_criteria_are_ac01_to_ac18_of_the_pod_document() -> None:
    assert harness._CRITERIA == CRITERIA
    assert len(harness._CRITERIA) == len(set(harness._CRITERIA)) == 18


def test_the_components_are_those_of_the_system_architecture() -> None:
    assert harness._COMPONENTS == COMPONENTS
    assert len(set(harness._COMPONENTS)) == 10


def test_every_planned_probe_has_one_coverage_row_and_only_known_names() -> None:
    planned = [t["test_id"] for t in harness._planned_tests(True)]
    assert sorted(harness._PROBE_COVERAGE) == sorted(planned)
    for test_id, (criteria, components) in harness._PROBE_COVERAGE.items():
        assert set(criteria) <= set(harness._CRITERIA), test_id
        assert set(components) <= set(harness._COMPONENTS), test_id
    for entry in harness._planned_tests(True):
        criteria, components = harness._PROBE_COVERAGE[entry["test_id"]]
        assert (entry["criteria"], entry["components"]) == (criteria, components)


def test_no_probe_performs_an_acceptance_criterion() -> None:
    """Each AC's Setup, Action and Expected, as written in the REQ/AC document, is more than any
    probe does (the 18-row table in the follow-up round 2 report gives the reason per AC)."""
    assert {test_id: criteria for test_id, (criteria, _) in harness._PROBE_COVERAGE.items()} == {
        test_id: () for test_id in EXPECTED_COMPONENTS
    }


def test_each_probe_lists_only_the_components_it_passes_through() -> None:
    assert {
        test_id: components for test_id, (_, components) in harness._PROBE_COVERAGE.items()
    } == EXPECTED_COMPONENTS


def test_a_cross_team_run_that_passes_everything() -> None:
    coverage = harness._coverage_from_results(_rows("pass", BASE_IDS), None, False)
    assert _counts(coverage["system"]) == (0, 18)
    assert _counts(coverage["component"]) == (1, 10)


def test_a_same_team_run_that_passes_everything() -> None:
    results = _rows("pass", [*BASE_IDS[:-1], *MODEL_IDS, "T-08"])
    coverage = harness._coverage_from_results(results, None, True)
    assert _counts(coverage["system"]) == (0, 18)
    assert _counts(coverage["component"]) == (7, 10)


def test_a_fail_counts_and_an_error_or_a_skip_never_does() -> None:
    ids = [*BASE_IDS, *MODEL_IDS]
    for outcome, covered in (("pass", 7), ("fail", 7), ("error", 0), ("skip", 0)):
        coverage = harness._coverage_from_results(_rows(outcome, ids), None, True)
        assert _counts(coverage["component"]) == (covered, 10), outcome
        assert _counts(coverage["system"]) == (0, 18), outcome
    empty = harness._coverage_from_results([], None)
    assert (_counts(empty["system"]), _counts(empty["component"])) == ((0, 18), (0, 10))


def test_the_rule_counts_each_criterion_and_component_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The counting rule itself, with a mapping in which probes do evaluate criteria."""
    monkeypatch.setattr(
        harness,
        "_PROBE_COVERAGE",
        {
            "P-1": (("AC-08",), ("Reporting service",)),
            "P-2": (("AC-08", "AC-14"), ("Reporting service", "Access control")),
            "P-3": (("AC-09",), ("Workflow engine",)),
            "P-4": (("AC-11",), ("Workflow engine",)),
        },
    )
    results = [
        *_rows("pass", ["P-1"]),
        *_rows("fail", ["P-2"]),
        *_rows("error", ["P-3"]),
        *_rows("skip", ["P-4"]),
        *_rows("pass", ["unknown"]),
    ]
    coverage = harness._coverage_from_results(results, None)
    assert _counts(coverage["system"]) == (2, 18)
    assert _counts(coverage["component"]) == (2, 10)


def test_the_bases_name_the_criteria_and_the_architecture() -> None:
    coverage = harness._coverage_from_results([], None)
    system = coverage["system"]["basis"]
    assert system.startswith(SYSTEM_BASIS_START)
    assert "AC-01 to AC-18" in system
    assert coverage["component"]["basis"] == COMPONENT_BASIS


def test_readme_lists_the_components_and_their_probes() -> None:
    text = README.read_text(encoding="utf-8")
    section = text.split("### 17.2 ", 1)[1].split("\n### ", 1)[0]
    rows = re.findall(r"^\| ([A-Z][a-z]+(?: [a-z]+| API)*) \| [^|]+ \| ([^|]+) \|$", section, re.M)
    rows = [row for row in rows if row[0] != "Component"]
    assert [name for name, _ in rows] == list(harness._COMPONENTS)
    for name, probes in rows:
        documented = set(re.findall(r"[TM]-\d\d", probes))
        mapped = {t for t, (_, comps) in harness._PROBE_COVERAGE.items() if name in comps}
        assert documented == mapped, name
