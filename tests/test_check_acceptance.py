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
    monkeypatch.setattr(gate, "PHASE5_REQUIRED", {})


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


def test_phase5_required_is_merged() -> None:
    """Phase 5 spec 10.4: required_criteria() includes every PHASE5_REQUIRED id, after the
    Phase 3 and 4 ids of the same criterion, and the eight Phase 5 criteria are filled."""
    merged = gate.required_criteria()
    assert set(gate.PHASE5_REQUIRED) == {
        "AC-01",
        "AC-03",
        "AC-05",
        "AC-06",
        "AC-13",
        "AC-14",
        "AC-16",
        "AC-18",
    }
    for criterion, names in gate.PHASE5_REQUIRED.items():
        assert names, criterion
        earlier = (*gate.REQUIRED.get(criterion, ()), *gate.PHASE4_REQUIRED.get(criterion, ()))
        assert merged[criterion][: len(earlier)] == tuple(dict.fromkeys(earlier)), criterion
        assert set(names) <= set(merged[criterion]), criterion
    assert merged["AC-16"] == (
        "tests.platform5.test_ac16_in_process::test_ac16_in_process_trace_both_paths",
    )


def test_phase5_required_is_pinned_exactly() -> None:
    """Audit F-08: every Phase 5 gate id is pinned, so dropping or renaming any one fails here
    (spec 10.4; decision 321)."""
    assert gate.PHASE5_REQUIRED == {
        "AC-01": ("tests.part1.test_ingestion::test_ac01_cadence_twelve_samples_no_gap",),
        "AC-03": (
            "tests.part1.test_ingestion::test_drift_boundary",
            "tests.part1.test_ingestion::test_performance_boundary",
            "tests.part1.test_ingestion::test_contract_example_1_flags_and_detail_shows_the_breach",
        ),
        "AC-05": (
            "tests.records.test_retention::test_ac05_retrievable_at_5mo29d",
            "tests.records.test_retention::test_ac05_at_6mo1d_presence_or_absence_is_accepted",
            "tests.records.test_erasure::test_nothing_is_erased_before_retain_until",
        ),
        "AC-06": (
            "tests.ledger_ops.test_nightly::test_nightly_verify_records_a_valid_run",
            "tests.ledger_ops.test_nightly::test_model5_ex5_nightly_verify_finds_a_payload_edit",
            "tests.ledger_ops.test_nightly::test_nightly_verify_runs_once_per_utc_day",
            "tests.ledger_ops.test_nightly::test_head_regression_is_detected",
            "tests.ledger_ops.test_nightly::test_tampered_report_bytes_are_found_by_the_nightly_check",
            "tests.reports.test_report_storage::test_report_tables_are_insert_only",
        ),
        "AC-13": (
            "tests.reports.test_report_schema::test_ac13_report_matches_the_schema",
            "tests.reports.test_report_schema::test_ac13_three_timestamps_are_non_null_and_distinct",
            "tests.reports.test_report_schema::test_closure_report_for_not_serious_has_no_deadline",
            "tests.reports.test_report_schema::test_closure_report_is_marked_not_for_submission",
            "tests.reports.test_report_storage::test_report_bytes_are_served_exactly_as_stored",
            "tests.reports.test_report_storage::test_report_hashes_are_in_the_ledger",
            "tests.reports.test_report_storage::test_pdf_contains_every_required_field",
        ),
        "AC-14": (
            "tests.reports.test_report_api::test_ac14_the_reviewer_gets_the_report_and_others_are_denied",
            "tests.reports.test_submission::test_submission_roles",
        ),
        "AC-16": ("tests.platform5.test_ac16_in_process::test_ac16_in_process_trace_both_paths",),
        "AC-18": (
            "tests.records.test_erasure::test_ac18_personal_data_is_unreadable_after_erasure",
            "tests.records.test_erasure::test_ac18_chain_positions_and_hashes_are_unchanged_and_verify",
            "tests.records.test_erasure::test_ac18_standalone_verifier_agrees_after_erasure",
            "tests.records.test_erasure::test_erasure_before_retain_until_is_409_and_changes_nothing",
            "tests.records.test_erasure::test_erasure_twice_is_409",
        ),
    }


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
