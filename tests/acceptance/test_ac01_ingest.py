"""AC-01 (REQ-01): one hour of samples at the default 5-minute interval is collected with no gap.

Phase 3 spec, shared rules: sys-42 samples every 300 s; a gap opens after 3 silent intervals.
The hour is simulated: one sample at simulated now, then a 300 s clock advance, twelve times.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from tests.acceptance.api import PERF_METRIC, Api

pytestmark = pytest.mark.integration

INTERVAL = 300
SAMPLES = 12


def test_an_hour_at_the_default_interval_is_collected_without_a_gap(api: Api) -> None:
    stamps = []
    for _ in range(SAMPLES):
        at = api.now().replace(microsecond=0)
        response = api.ingest(PERF_METRIC, "0.90", at=at)
        assert response.status_code == 201, response.text
        body = response.json()
        assert set(body) == {"sample_id"}, body  # no breach: no flagged_event_id
        assert body["sample_id"].startswith("smp-")
        stamps.append(at)
        api.advance(INTERVAL)

    gaps = [later - earlier for earlier, later in zip(stamps, stamps[1:], strict=False)]
    assert all(abs(gap - timedelta(seconds=INTERVAL)) <= timedelta(seconds=10) for gap in gaps)
    assert api.events(type="monitoring_gap") == []
    assert api.events(type="threshold_breach") == []


def test_every_sample_is_stored_once_with_a_distinct_id(api: Api) -> None:
    at = api.now().replace(microsecond=0)
    first = api.ingest(PERF_METRIC, "0.90", at=at)
    second = api.ingest(PERF_METRIC, "0.91", at=at + timedelta(seconds=INTERVAL))
    duplicate = api.ingest(PERF_METRIC, "0.90", at=at)
    assert (first.status_code, second.status_code) == (201, 201)
    assert first.json()["sample_id"] != second.json()["sample_id"]
    # Phase 3 spec 1.4 step 5: a duplicate (same system, metric, timestamp) is 200 with the
    # original sample_id, and creates nothing.
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["sample_id"] == first.json()["sample_id"]
