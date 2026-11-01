"""The acceptance gate's mechanics (Phase 4 spec 4.10; Part 4 decision 151).

The gate is pure: it reads a JUnit file and the two mappings. These tests write small JUnit
files and patch the mappings, so they need no database and no other part's tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tools import check_acceptance as gate

PHASE3_AC15 = "tests.api.test_audit::"
PHASE4_AC15 = "tests.part1.test_review_audit::test_review_open_is_audited"


def _junit(tmp_path: Path, cases: list[tuple[str, str, str]]) -> str:
    """cases: (classname, name, outcome) with outcome 'passed', 'failed' or 'skipped'."""
    body = []
    for classname, name, outcome in cases:
        child = {
            "passed": "",
            "failed": '<failure message="boom"/>',
            "skipped": '<skipped message="skip"/>',
        }[outcome]
        body.append(f'<testcase classname="{classname}" name="{name}">{child}</testcase>')
    path = tmp_path / "results.xml"
    path.write_text(
        '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite name="pytest">'
        + "".join(body)
        + "</testsuite></testsuites>",
        encoding="utf-8",
    )
    return str(path)


def _phase3_passing() -> list[tuple[str, str, str]]:
    """One passing case for every Phase 3 entry in REQUIRED."""
    cases = []
    for names in gate.REQUIRED.values():
        for required in names:
            classname, _, name = required.partition("::")
            cases.append((classname, name or "test_anything", "passed"))
    return cases


def _required_passing() -> list[tuple[str, str, str]]:
    """One passing case for every entry of the merged mapping the gate checks (REQUIRED plus
    PHASE4_REQUIRED, per criterion)."""
    cases = []
    for names in gate.required_criteria().values():
        for required in names:
            classname, _, name = required.partition("::")
            cases.append((classname, name or "test_anything", "passed"))
    return cases


@pytest.fixture
def phase4_ac15(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "PHASE4_REQUIRED", {"AC-15": (PHASE4_AC15,)})


@pytest.mark.usefixtures("phase4_ac15")
def test_a_phase4_entry_for_an_existing_criterion_keeps_the_phase3_entries(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The Phase 4 ids are added to a criterion, never substituted for the Phase 3 ones."""
    only_phase4 = [c for c in _phase3_passing() if not c[0].startswith("tests.api.test_audit")]
    only_phase4.append(("tests.part1.test_review_audit", "test_review_open_is_audited", "passed"))
    assert gate.main([_junit(tmp_path, only_phase4)]) == 1
    assert f"AC-15 MISSING OR FAILED: {PHASE3_AC15}" in capsys.readouterr().err


@pytest.mark.usefixtures("phase4_ac15")
def test_the_union_passes_when_both_parts_of_a_criterion_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = [*_phase3_passing()]
    cases.append(("tests.part1.test_review_audit", "test_review_open_is_audited", "passed"))
    assert gate.main([_junit(tmp_path, cases)]) == 0
    out = capsys.readouterr().out
    assert f"AC-15 passed (1): {PHASE3_AC15}" in out
    assert f"AC-15 passed (1): {PHASE4_AC15}" in out


@pytest.mark.usefixtures("phase4_ac15")
def test_a_phase4_entry_that_did_not_run_fails_the_gate(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main([_junit(tmp_path, _phase3_passing())]) == 1
    assert f"AC-15 MISSING OR FAILED: {PHASE4_AC15}" in capsys.readouterr().err


def test_the_merged_mapping_keeps_order_and_drops_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        gate,
        "PHASE4_REQUIRED",
        {
            "AC-15": (PHASE4_AC15, PHASE3_AC15),
            "AC-07": ("tests.part1.test_x::test_y",),
            "AC-08": (),
        },
    )
    merged = gate.required_criteria()
    assert merged["AC-15"] == (PHASE3_AC15, PHASE4_AC15)
    assert merged["AC-07"] == ("tests.part1.test_x::test_y",)
    assert "AC-08" not in merged
    assert merged["AC-02"] == gate.REQUIRED["AC-02"]


def test_a_skipped_required_test_counts_as_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = _phase3_passing()
    first = gate.REQUIRED["AC-04"][0]
    classname, _, name = first.partition("::")
    cases = [c for c in cases if (c[0], c[1]) != (classname, name)]
    cases.append((classname, name, "skipped"))
    assert gate.main([_junit(tmp_path, cases)]) == 1
    assert f"AC-04 MISSING OR FAILED: {first}" in capsys.readouterr().err


def test_a_failed_required_test_counts_as_missing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cases = _phase3_passing()
    first = gate.REQUIRED["AC-06"][0]
    classname, _, name = first.partition("::")
    cases = [c for c in cases if (c[0], c[1]) != (classname, name)]
    cases.append((classname, name, "failed"))
    assert gate.main([_junit(tmp_path, cases)]) == 1
    assert f"AC-06 MISSING OR FAILED: {first}" in capsys.readouterr().err


def test_one_argument_is_the_results_file(tmp_path: Path) -> None:
    assert gate.main([_junit(tmp_path, _required_passing())]) == 0


@pytest.mark.parametrize("argv", [[], ["a.xml", "b.xml"]])
def test_any_other_argument_count_is_a_usage_error(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main(argv) == 2
    assert "usage: check_acceptance.py RESULTS.xml" in capsys.readouterr().err


def test_there_is_no_ac12_note(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Spec 4.10 drops the Phase 3 AC-12 note: AC-12 is a Phase 4 criterion like the others."""
    gate.main([_junit(tmp_path, _required_passing())])
    captured = capsys.readouterr()
    output = captured.out + captured.err
    assert "AC-12 not in Phase 3" not in output
    assert "reminders need the deadlines of REQ-10" not in output
