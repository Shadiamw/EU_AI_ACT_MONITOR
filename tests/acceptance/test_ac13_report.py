"""AC-13 (REQ-12) over HTTP: the assembled report is schema-valid, carries three distinct
timestamps, and its PDF download matches its hash (Phase 5 spec 3.3, 3.6, 10.1).

The schema is the contract-delta copy, docs/contract/phase5/report.schema.json (Δ1, spec 9.4).
"""

from __future__ import annotations

import hashlib
import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from jsonschema import Draft202012Validator

from tests.acceptance.api import TIER_DAYS, Api, api_time, parse_time

pytestmark = pytest.mark.integration

SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[2] / "docs" / "contract" / "phase5" / "report.schema.json"
    ).read_text(encoding="utf-8")
)
SERIOUS = ("DEATH", "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "OTHER_SERIOUS")


def _report(api: Api, event_id: str, fmt: str | None = None) -> httpx.Response:
    query = "" if fmt is None else f"?format={fmt}"
    response = api.http.get(f"/v1/events/{event_id}/report{query}", headers=api.headers["reviewer"])
    assert response.status_code == 200, response.text
    return response


def _assembled(api: Api, tier: str) -> tuple[str, Any, dict[str, Any]]:
    """Flag at T, advance 4 h, dispose, advance 1 h, report (spec 10.1, AC-13)."""
    event_id, detected = api.flag()
    api.event(event_id, "reviewer")
    api.advance(4 * 3600)
    disposed = api.disposed(event_id, tier)
    api.advance(3600)
    return event_id, detected, {"disposed": disposed, "report": _report(api, event_id).json()}


@pytest.mark.parametrize("tier", SERIOUS)
def test_ac13_report_over_http_matches_the_schema(api: Api, tier: str) -> None:
    event_id, detected, got = _assembled(api, tier)
    report = got["report"]
    errors = sorted(Draft202012Validator(SCHEMA).iter_errors(report), key=str)
    assert not errors, [error.message for error in errors]
    assert report["report_kind"] == "SERIOUS_INCIDENT"
    assert report["event"]["event_id"] == event_id
    assert report["determination"]["tier"] == tier
    assert report["regulation"]["reporting_window_days"] == TIER_DAYS[tier]

    stamps = report["timestamps"]
    assert stamps["detected_at"] == api_time(detected)
    assert stamps["determined_at"] == got["disposed"]["disposition"]["assigned_at"]
    assert stamps["last_revised_at"] is None
    determined = parse_time(stamps["determined_at"])
    assembled = parse_time(stamps["assembled_at"])
    assert detected < determined < assembled
    # Real seconds pass between calls, so each gap is the advance plus at most a minute.
    assert timedelta(hours=4) <= determined - detected < timedelta(hours=4, minutes=1)
    assert timedelta(hours=1) <= assembled - determined < timedelta(hours=1, minutes=1)
    for key in ("health_safety_harm", "fundamental_rights_infringement"):
        assert report["harm_fields"][key]["status"] in ("CONFIRMED", "NOT_APPLICABLE")


def test_ac13_pdf_download_matches_its_hash(api: Api) -> None:
    event_id, _, got = _assembled(api, "DEATH")
    as_json = _report(api, event_id)
    as_pdf = _report(api, event_id, "pdf")
    assert as_pdf.headers["content-type"].startswith("application/pdf")
    assert as_pdf.content.startswith(b"%PDF")
    for response in (as_json, as_pdf):
        digest = hashlib.sha256(response.content).hexdigest()
        assert response.headers["X-Report-Sha256"] == digest
        assert response.headers["X-Report-Id"] == got["report"]["report_id"]
        assert response.headers["X-Report-Version"] == "1"
    # Served exactly as stored (INV-14): a second download is byte-identical.
    assert _report(api, event_id, "pdf").content == as_pdf.content
    assert as_json.json() == got["report"]
