"""AC-03 (REQ-03): a value beyond the threshold flags an event with the exact metric name,
timestamp, value, threshold and system id, for a drift and a performance metric; a value
exactly at the threshold does not flag, one unit beyond does.

Seeded thresholds (Phase 3 spec, shared rules): accuracy_drift (drift) breaches when > 0.30;
accuracy (performance) breaches when < 0.70. Event fields: contract Event object and Phase 3
spec 1.5 (detected_at is the request's timestamp).
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from tests.acceptance.api import (
    DRIFT_LIMIT,
    DRIFT_METRIC,
    PERF_LIMIT,
    PERF_METRIC,
    Api,
    api_time,
    decimal_json,
)

pytestmark = pytest.mark.integration

# (metric, value exactly at the limit, value one unit beyond it, limit)
CASES = [
    pytest.param(DRIFT_METRIC, "0.30", "0.31", DRIFT_LIMIT, id="drift"),
    pytest.param(PERF_METRIC, "0.70", "0.69", PERF_LIMIT, id="performance"),
]


def test_contract_example_1_flags_with_sample_and_event_ids(api: Api) -> None:
    """Contract Example 1, verbatim request; 201 with {sample_id, flagged_event_id}."""
    response = api.http.post(
        "/v1/metrics",
        headers=api.headers["system"],
        json={
            "system_id": "sys-42",
            "metric_name": "accuracy_drift",
            "metric_type": "drift",
            "value": 0.34,
            "timestamp": "2026-09-27T10:05:00Z",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert set(body) == {"sample_id", "flagged_event_id"}
    assert body["sample_id"].startswith("smp-")
    assert body["flagged_event_id"].startswith("evt-")
    event = decimal_json(
        api.http.get(f"/v1/events/{body['flagged_event_id']}", headers=api.headers["viewer"])
    )
    assert event["detected_at"] == "2026-09-27T10:05:00Z"
    assert event["observed_value"] == Decimal("0.34")
    assert event["threshold_value"] == Decimal("0.30")


@pytest.mark.parametrize(("metric", "at_limit", "beyond", "limit"), CASES)
def test_a_value_exactly_at_the_threshold_does_not_flag(
    api: Api, metric: str, at_limit: str, beyond: str, limit: Decimal
) -> None:
    response = api.ingest(metric, at_limit)
    assert response.status_code == 201, response.text
    assert "flagged_event_id" not in response.json()
    assert api.events(type="threshold_breach") == []


@pytest.mark.parametrize(("metric", "at_limit", "beyond", "limit"), CASES)
def test_one_unit_beyond_the_threshold_flags_with_the_exact_fields(
    api: Api, metric: str, at_limit: str, beyond: str, limit: Decimal
) -> None:
    at = api.now().replace(microsecond=0)
    response = api.ingest(metric, beyond, at=at)
    assert response.status_code == 201, response.text
    event_id = response.json()["flagged_event_id"]

    event = decimal_json(api.http.get(f"/v1/events/{event_id}", headers=api.headers["viewer"]))
    assert event["event_id"] == event_id
    assert event["system_id"] == "sys-42"
    assert event["type"] == "threshold_breach"
    assert event["metric_name"] == metric
    assert event["observed_value"] == Decimal(beyond)
    assert event["threshold_value"] == limit
    assert event["detected_at"] == api_time(at)
    assert event["state"] == "FLAGGED"

    [summary] = api.events(type="threshold_breach")
    assert summary == {
        "event_id": event_id,
        "system_id": "sys-42",
        "type": "threshold_breach",
        "metric_name": metric,
        "detected_at": api_time(at),
        "state": "FLAGGED",
    }
