"""AC-18 (REQ-16) over HTTP, Phase 5 spec 4.5: after retain_until, erasure makes the personal
data unreadable while the chain keeps every position and hash, and both verifiers agree.

Over HTTP the steps that read SQL (the sealed form, record_key, `open`) are left to the
in-process tests/records/test_erasure.py; everything the API shows is checked here.
"""

from __future__ import annotations

import calendar
import json
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import pytest

from tests.acceptance.api import HARM_KEYS, Api, Sink, decisions

pytestmark = pytest.mark.integration

VERIFIER = Path(__file__).resolve().parents[2] / "tools" / "verify_ledger.py"
EVIDENCE = "Applicant Jane Example was refused a loan"
RATIONALE = "Raised by Jane Example's complaint"
RECORD_ERASED = {
    "error_code": "RECORD_ERASED",
    "message": "the personal data of this record has been erased",
}
RETENTION_NOT_EXPIRED = {
    "error_code": "RETENTION_NOT_EXPIRED",
    "message": "the retention period of this record has not ended",
}
ALREADY_ERASED = {"error_code": "ALREADY_ERASED", "message": "this record has already been erased"}


def add_calendar_months(moment: datetime, months: int) -> datetime:
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    return moment.replace(
        year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1])
    )


def _erase(api: Api, event_id: str) -> httpx.Response:
    return api.http.post(f"/v1/events/{event_id}/erasure", headers=api.headers["reviewer"])


def _offline_verify(export: list[dict[str, object]]) -> dict[str, object]:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "export.json"
        path.write_text(json.dumps(export), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, str(VERIFIER), str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    assert run.returncode == 0, run.stdout + run.stderr
    result: dict[str, object] = json.loads(run.stdout)
    return result


def test_ac18_over_http_erasure_keeps_the_chain(api: Api, sink: Sink) -> None:
    # 1. Setup.
    api.register_webhook(sink.url, ["flag_notification", "reminder"])
    event_id, detected = api.flag()
    api.event(event_id, "reviewer")
    fields = decisions(
        fundamental_rights_infringement={"status": "CONFIRMED", "evidence": EVIDENCE}
    )
    api.disposed(event_id, "OTHER_SERIOUS", rationale=RATIONALE, harm_fields=fields)
    api.advance(3600)
    api.disposed(event_id, "DEATH", rationale=RATIONALE, harm_fields=fields)
    report = api.http.get(f"/v1/events/{event_id}/report", headers=api.headers["reviewer"])
    assert report.status_code == 200, report.text
    report_id = report.json()["report_id"]
    submitted = api.http.post(
        f"/v1/events/{event_id}/report/submission",
        headers=api.headers["reviewer"],
        json={
            "report_id": report_id,
            "json_sha256": report.headers["X-Report-Sha256"],
            "report_type": "Initial",
        },
    )
    assert submitted.status_code == 201, submitted.text

    # 2. Readable. (Each event read is logged, so E0 is taken after the reads.)
    event = api.event(event_id)
    assert event["harm_fields"]["fundamental_rights_infringement"]["evidence"] == EVIDENCE
    assert event["disposition"]["rationale"] == RATIONALE
    assert "Jane Example" in report.text
    e0 = api.export()
    assert api.verify() == {"valid": True, "entries_checked": len(e0), "first_mismatch": None}

    # 3. Too early: refused, nothing written.
    early = _erase(api, event_id)
    assert (early.status_code, early.json()) == (409, RETENTION_NOT_EXPIRED)
    assert api.export() == e0

    # 4. Past retain_until = max(6 calendar months, 183 days) from detected_at, plus a day.
    retain = max(add_calendar_months(detected, 6), detected + timedelta(days=183))
    api.advance_to(retain + timedelta(days=1))
    erased = _erase(api, event_id)
    assert erased.status_code == 200, erased.text
    e1 = api.export()
    body = erased.json()
    assert body["event_id"] == event_id
    assert set(body) == {"event_id", "erased_at", "erased_fields"}
    assert all("Jane" not in name for name in body["erased_fields"])

    # 5. Unreadable through the API.
    event = api.event(event_id)
    assert all(event["harm_fields"][key]["evidence"] is None for key in HARM_KEYS)
    assert event["disposition"]["rationale"] is None
    for path in (f"/v1/events/{event_id}/report", f"/v1/reports/{report_id}"):
        gone = api.http.get(path, headers=api.headers["reviewer"])
        assert (gone.status_code, gone.json()) == (410, RECORD_ERASED), path
    listed = api.http.get(f"/v1/events/{event_id}/reports", headers=api.headers["viewer"])
    assert listed.status_code == 200
    assert [row["report_id"] for row in listed.json()] == [report_id]
    assert "RECORD_ERASED" in [row["action"] for row in api.audit(event_id)]

    # 6. The chain at the erasure (E1, exported right after it): E0 is an unchanged prefix, one
    # RECORD_ERASED, last; both verifiers valid. The six-month advance's sweep and reminder
    # passes append before the erasure; the reads of step 5 append after E1.
    assert e1[: len(e0)] == e0
    erasures = [entry for entry in e1[len(e0) :] if entry["entry_type"] == "RECORD_ERASED"]
    assert [entry["subject_id"] for entry in erasures] == [event_id]
    assert e1[-1]["entry_type"] == "RECORD_ERASED"
    expected = {"valid": True, "entries_checked": len(e1), "first_mismatch": None}
    assert _offline_verify(e1) == expected
    now = api.verify()
    assert now["valid"] is True and now["first_mismatch"] is None, now

    # 7. Idempotence.
    again = _erase(api, event_id)
    assert (again.status_code, again.json()) == (409, ALREADY_ERASED)
