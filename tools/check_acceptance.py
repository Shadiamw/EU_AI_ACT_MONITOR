"""Fail unless the pytest JUnit file shows every Phase 3 acceptance criterion passing.

AC-12 is reported and not required: reminders are not in Phase 3 (decision 89).
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


def _node(classname: str, name: str) -> str:
    return f"{classname}::{name}"


def _passed(case: ET.Element) -> bool:
    return not any(child.tag in ("failure", "error", "skipped") for child in case)


def _matches(node: str, required: str) -> bool:
    if required.endswith("::"):
        return node.startswith(required)
    return node == required or node.startswith(required + "[")


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_acceptance.py RESULTS.xml", file=sys.stderr)
        return 2
    root = ET.parse(argv[1]).getroot()
    cases = [
        (_node(case.get("classname") or "", case.get("name") or ""), _passed(case))
        for case in root.iter("testcase")
    ]
    missing: list[str] = []
    for criterion, names in REQUIRED.items():
        for required in names:
            hits = [ok for node, ok in cases if _matches(node, required)]
            if hits and all(hits):
                print(f"{criterion} passed ({len(hits)}): {required}")
            else:
                missing.append(f"{criterion} {required}")
                print(f"{criterion} MISSING OR FAILED: {required}", file=sys.stderr)
    print(
        "AC-12 not in Phase 3: reminders need the deadlines of REQ-10. "
        "reminder stays a valid webhook type."
    )
    if missing:
        print(f"{len(missing)} acceptance criterion check(s) failed", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
