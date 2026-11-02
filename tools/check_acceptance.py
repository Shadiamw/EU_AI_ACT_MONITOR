"""Fail unless the pytest JUnit file shows every required acceptance criterion passing.

Phase 3 ACs stay required on every branch. Phase 4 ACs (AC-07..12, 14, 15, 17) are added at
integration (Phase 4 Addendum 9.5) once Parts 1–3 publish their exact node ids. The two
mappings are merged per criterion as a union (Part 4 decision 151): a Phase 4 entry for AC-15
adds its ids to the Phase 3 AC-15 ids and never replaces them.
"""

import sys
import xml.etree.ElementTree as ET

# classname::test name, without a parameter suffix. A parameterised run matches too.
REQUIRED: dict[str, tuple[str, ...]] = {
    "AC-02": (
        "tests.worker.test_gaps::test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do",
        "tests.worker.test_worker_system::test_gap_to_delivery_to_close_and_reverify",
    ),
    "AC-04": (
        "tests.worker.test_notifications::test_delivery_appends_delivered_with_integer_latency_and_boolean_bound",
        "tests.worker.test_notifications::test_within_bound_compares_latency_with_the_configured_bound",
        "tests.worker.test_notifications::test_ac04_the_bound_is_configurable_to_five_minutes",
        "tests.worker.test_notifications::test_ac04_a_slow_receiver_with_a_low_bound_is_not_within_bound",
    ),
    "AC-06": (
        "tests.test_verify::test_ac06_payload_tamper_is_detected",
        "tests.api.test_export_verify::test_ac06_payload_tamper_through_the_api",
        "tests.test_schema_permissions::test_app_role_has_insert_and_select_only",
        "tests.test_schema_permissions::test_trigger_definition_blocks_update_and_delete_but_not_truncate",
    ),
    "AC-15": ("tests.api.test_audit::",),
}

# Filled at integration from the part reports (Addendum 9.5; integration decision 218). Merged
# per criterion with REQUIRED, so AC-15 keeps the Phase 3 tests.api.test_audit:: entry.
PHASE4_REQUIRED: dict[str, tuple[str, ...]] = {
    "AC-07": (
        "tests.part1.test_review_prefill::test_ac07_prefill_fills_the_rights_field_and_leaves_the_others_blank",
        "tests.part1.test_review_prefill::test_no_context_row_gives_four_blank_fields",
        "tests.part1.test_review_decisions::test_ac07_confirmation_record",
        "tests.part1.test_review_integration::test_ac07_confirmation_record_through_the_api",
    ),
    "AC-08": (
        "tests.part2.test_disposition_api::test_ac08_a_report_without_a_disposition_is_409_and_changes_nothing",
    ),
    "AC-09": (
        "tests.part2.test_disposition_api::test_ac09_not_serious_without_a_rationale_is_contract_example_3",
        "tests.part2.test_disposition_api::test_ac09_not_serious_with_a_rationale_closes_with_no_deadline",
        "tests.part2.test_disposition_api::test_ac09_each_of_the_four_tiers_is_accepted",
        "tests.part2.test_disposition_api::test_ac09_any_other_tier_is_malformed",
        "tests.part2.test_disposition_integration::test_ac09_a_not_serious_first_disposition_has_no_escalation_rows",
    ),
    "AC-10": (
        "tests.part2.test_disposition_api::test_ac10_a_revision_appends_a_row_and_keeps_the_original",
        "tests.part2.test_disposition_schema::test_the_table_is_append_only",
    ),
    "AC-11": (
        "tests.part2.test_disposition_api::test_ac11_the_deadline_runs_from_the_assignment_not_the_detection",
        "tests.part2.test_disposition_api::test_ac11_revisions",
        "tests.part2.test_deadlines::test_calendar_days_worked_examples",
        "tests.part2.test_deadlines::test_working_days_from_a_sunday",
        "tests.part2.test_deadlines::test_working_days_with_holidays",
    ),
    "AC-12": (
        "tests.part3.test_escalations::test_ac12_fires_at_50_80_100_and_not_at_49_79_99",
        "tests.part3.test_escalations::test_one_second_early_fires_nothing_and_the_instant_fires",
        "tests.part3.test_escalations::test_a_big_jump_fires_every_percentage_once_in_order",
        "tests.part3.test_escalations::test_no_duplicates_after_further_passes",
        "tests.part3.test_escalations::test_two_callers_across_the_50_instant_fire_one_reminder_each_time",
        "tests.part3.test_escalations_integration::test_no_duplicates_through_the_clock_advance_endpoint",
        "tests.part3.test_escalations_integration::test_a_worker_pass_and_a_clock_advance_across_the_50_instant_50_times",
    ),
    "AC-14": (
        "tests.part2.test_disposition_api::test_ac14_non_reviewers_are_denied_on_both_actions",
        "tests.part2.test_disposition_api::test_ac14_the_reviewer_succeeds_on_both_actions",
    ),
    "AC-15": (
        "tests.part2.test_disposition_api::test_ac15_a_view_by_the_viewer_and_an_edit_by_the_reviewer_are_both_audited",
        "tests.part1.test_review_start::test_a_reviewer_view_moves_flagged_to_under_review",
        "tests.part1.test_review_start::test_the_audit_trail_shows_the_review_start",
        "tests.part1.test_review_start::test_a_viewer_view_changes_nothing",
        "tests.part1.test_review_start::test_a_second_reviewer_view_appends_event_viewed_only",
    ),
    "AC-17": (
        "tests.part1.test_review_integration::test_ac17_detection_time_and_assignment_anchored_deadline",
        "tests.part1.test_review_detail::test_detected_at_survives_the_review_start_and_the_harm_decisions",
    ),
}


def _node(classname: str, name: str) -> str:
    return f"{classname}::{name}"


def _passed(case: ET.Element) -> bool:
    return not any(child.tag in ("failure", "error", "skipped") for child in case)


def _matches(node: str, required: str) -> bool:
    if required.endswith("::"):
        return node.startswith(required)
    return node == required or node.startswith(required + "[")


def required_criteria() -> dict[str, tuple[str, ...]]:
    """REQUIRED plus PHASE4_REQUIRED, per criterion, in order, without duplicates.

    A criterion whose Phase 4 tuple is still empty adds nothing (decision 151)."""
    merged = {criterion: tuple(names) for criterion, names in REQUIRED.items()}
    for criterion, names in PHASE4_REQUIRED.items():
        if names:
            merged[criterion] = tuple(dict.fromkeys((*merged.get(criterion, ()), *names)))
    return merged


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1:
        print("usage: check_acceptance.py RESULTS.xml", file=sys.stderr)
        return 2
    root = ET.parse(args[0]).getroot()
    cases = [
        (_node(case.get("classname") or "", case.get("name") or ""), _passed(case))
        for case in root.iter("testcase")
    ]
    required = required_criteria()
    missing: list[str] = []
    for criterion, names in required.items():
        for name in names:
            hits = [ok for node, ok in cases if _matches(node, name)]
            if hits and all(hits):
                print(f"{criterion} passed ({len(hits)}): {name}")
            else:
                missing.append(f"{criterion} {name}")
                print(f"{criterion} MISSING OR FAILED: {name}", file=sys.stderr)
    if not PHASE4_REQUIRED or not any(PHASE4_REQUIRED.values()):
        print(
            "Phase 4 ACs not yet wired into REQUIRED "
            "(fill PHASE4_REQUIRED at integration; decision 151)."
        )
    if missing:
        print(f"{len(missing)} acceptance criterion check(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
