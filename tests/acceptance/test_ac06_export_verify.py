"""AC-06 (REQ-06), export form: the exported chain re-verifies offline, and an edit to an
exported record is detected at that record.

Over HTTP the runner cannot modify storage directly; the contract has no tamper endpoint. This
is the other team's demonstration (Phase 3 spec 2.8): export, run tools/verify_ledger.py, edit
one payload in the file, run it again. The storage-layer write permissions and the direct
modification are covered in-process (tests/test_schema_permissions.py, tests/test_verify*.py).
Entry format and checks: Phase 3 spec 2.3 and 2.7.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from tests.acceptance.api import Api, parse_ledger_time

pytestmark = pytest.mark.integration

VERIFIER = Path(__file__).resolve().parents[2] / "tools" / "verify_ledger.py"
FIELDS = {
    "seq",
    "entry_type",
    "actor_id",
    "subject_id",
    "payload",
    "recorded_at",
    "prev_hash",
    "hash",
}


def _verify_file(path: Path) -> tuple[int, dict[str, object]]:
    run = subprocess.run(
        [sys.executable, str(VERIFIER), str(path)],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    result: dict[str, object] = json.loads(run.stdout)
    return run.returncode, result


def test_the_export_has_the_pinned_entry_format(api: Api) -> None:
    api.flag()
    entries = api.export()
    assert [entry["seq"] for entry in entries] == list(range(1, len(entries) + 1))
    assert entries[0]["entry_type"] == "SEED"
    assert entries[0]["prev_hash"] == "0" * 64
    for earlier, later in zip(entries, entries[1:], strict=False):
        assert later["prev_hash"] == earlier["hash"]
    for entry in entries:
        assert set(entry) == FIELDS
        assert len(entry["hash"]) == 64 and entry["hash"] == entry["hash"].lower()
        parse_ledger_time(entry["recorded_at"])
    assert [entry["entry_type"] for entry in entries[1:]] == [
        "EVENT_FLAGGED",
        "NOTIFICATION_REQUESTED",
    ]


def test_the_export_verifies_offline_and_matches_the_server(api: Api, tmp_path: Path) -> None:
    api.flag()
    entries = api.export()
    path = tmp_path / "export.json"
    path.write_text(json.dumps(entries), encoding="utf-8")

    code, result = _verify_file(path)
    assert code == 0, result
    assert result == {"valid": True, "entries_checked": len(entries), "first_mismatch": None}

    server = api.verify()
    assert {key: server[key] for key in ("valid", "entries_checked", "first_mismatch")} == result


def test_an_edited_record_is_named_by_the_offline_verifier(api: Api, tmp_path: Path) -> None:
    event_id, _ = api.flag()
    entries = api.export()
    flagged = next(entry for entry in entries if entry["entry_type"] == "EVENT_FLAGGED")
    assert flagged["subject_id"] == event_id
    flagged["payload"]["observed_value"] = "0.99"
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(entries), encoding="utf-8")

    code, result = _verify_file(path)
    assert code == 1, result
    assert result["valid"] is False
    assert result["first_mismatch"] == str(flagged["seq"])
    assert result["entries_checked"] == flagged["seq"]

    # The live chain is untouched.
    assert api.verify()["valid"] is True
