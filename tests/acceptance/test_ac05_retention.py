"""AC-05 (REQ-05) over HTTP, Phase 5 spec 5.3: a flagged event is retrievable unchanged at
five months and 29 days; at six months and one day it is either still there unchanged or gone
(404), and anything else, a 500 in particular, fails. Erasure before retain_until is refused
(INV-10, spec 5.3 step 4).

The six-month advance runs the sweep and reminder passes, so monitoring-gap events appear for
sys-42 during the jump; the test reads only its own event id (spec 5.3). The evidence bundle
and the five metric_sample rows are not exposed over HTTP; the in-process
tests/records/test_retention.py checks them.
"""

from __future__ import annotations

import calendar
from datetime import datetime, timedelta

import pytest

from tests.acceptance.api import DRIFT_METRIC, Api

pytestmark = pytest.mark.integration

RETENTION_NOT_EXPIRED = {
    "error_code": "RETENTION_NOT_EXPIRED",
    "message": "the retention period of this record has not ended",
}


def add_calendar_months(moment: datetime, months: int) -> datetime:
    """Spec 5.3: the same day and time `months` later, clamped to the month's last day."""
    index = moment.month - 1 + months
    year, month = moment.year + index // 12, index % 12 + 1
    return moment.replace(
        year=year, month=month, day=min(moment.day, calendar.monthrange(year, month)[1])
    )


def test_ac05_over_http_retrievable_then_accepted(api: Api) -> None:
    start = api.now().replace(microsecond=0)
    for minutes in (4, 3, 2, 1):
        healthy = api.ingest(DRIFT_METRIC, "0.10", at=start - timedelta(minutes=minutes))
        assert healthy.status_code == 201, healthy.text
    event_id, detected = api.flag("0.34", DRIFT_METRIC)
    at_flag = api.event(event_id)
    assert at_flag["state"] == "FLAGGED"

    api.advance_to(add_calendar_months(detected, 5) + timedelta(days=29))
    assert api.event(event_id) == at_flag
    erasure = api.http.post(f"/v1/events/{event_id}/erasure", headers=api.headers["reviewer"])
    assert erasure.status_code == 409, erasure.text
    assert erasure.json() == RETENTION_NOT_EXPIRED

    api.advance_to(add_calendar_months(detected, 6) + timedelta(days=1))
    later = api.http.get(f"/v1/events/{event_id}", headers=api.headers["viewer"])
    assert later.status_code in (200, 404), later.text
    if later.status_code == 200:
        assert later.json() == at_flag
