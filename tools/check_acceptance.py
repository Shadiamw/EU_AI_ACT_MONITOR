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

# Filled at integration from the part reports (Addendum 9.5). Kept here so the merge of Part 4
# only edits this mapping.
PHASE4_REQUIRED: dict[str, tuple[str, ...]] = {
    # "AC-07": (),
    # "AC-08": (),
    # "AC-09": (),
    # "AC-10": (),
    # "AC-11": (),
    # "AC-12": (),
    # "AC-14": (),
    # "AC-17": (),
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
