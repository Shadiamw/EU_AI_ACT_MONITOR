"""Part 1: GET /v1/events, GET /v1/events/{event_id} and the two 501 stubs (spec 1.6, spec 1.9
Detail and list and Stubs; decisions 117, 118, 124, 125).
"""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import Engine

from app import gaps
from tests.api.conftest import EVENT_KEYS, FORBIDDEN, MALFORMED, NOT_FOUND, UNAUTHORIZED, ApiClient
from tests.db_helpers import HARM_FIELDS, bearer, insert_event
from tests.part1.conftest import T0, entry_types, get, post, post_raw, rows
from tests.worker.conftest import FakeClock

SUMMARY_KEYS = {"event_id", "system_id", "type", "metric_name", "detected_at", "state"}


def ids(response: object) -> list[str]:
    return [item["event_id"] for item in response.json()]  # type: ignore[attr-defined]


def test_unknown_event_is_404_and_writes_nothing(client: ApiClient, owner_engine: Engine) -> None:
    for actor in ("rev-1", "viewer-1"):
        response = get(client, "/v1/events/evt-does-not-exist", actor)
        assert response.status_code == 404
        assert response.json() == NOT_FOUND
    assert entry_types(owner_engine) == ["SEED"]


def test_detail_is_the_contract_event_shape_and_records_a_view(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = post(client).json()["flagged_event_id"]
    fake_clock.advance(30)
    response = get(client, f"/v1/events/{event_id}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    body = response.json()
    assert set(body) == EVENT_KEYS
    assert set(body["harm_fields"]) == set(HARM_FIELDS)
    # REQ-14: EVENT_VIEWED with the viewer's actor id and an empty payload (spec 2.4).
    [viewed] = rows(
        owner_engine,
        "SELECT actor_id, subject_id, payload FROM ledger_entry WHERE entry_type = 'EVENT_VIEWED'",
    )
    assert viewed == {"actor_id": "viewer-1", "subject_id": event_id, "payload": {}}
    trail = get(client, f"/v1/events/{event_id}/audit", "rev-1").json()
    assert trail[-1]["action"] == "EVENT_VIEWED"
    assert trail[-1]["actor_id"] == "viewer-1"


@pytest.mark.parametrize(
    ("value", "metric_name", "metric_type", "limit"),
    [
        ("0.340000000000000000000000000001", "accuracy_drift", "drift", "0.30"),
        ("0.1234567890123456789", "accuracy", "performance", "0.70"),
    ],
)
def test_detail_decimals_are_exact_json_numbers(
    client: ApiClient, value: str, metric_name: str, metric_type: str, limit: str
) -> None:
    content = json.dumps(
        {
            "system_id": "sys-42",
            "metric_name": metric_name,
            "metric_type": metric_type,
            "value": "VALUE",
            "timestamp": "2026-09-27T10:05:00Z",
        }
    ).replace('"VALUE"', value)
    event_id = post_raw(client, content).json()["flagged_event_id"]
    text = get(client, f"/v1/events/{event_id}").text
    # Numbers, not strings, with every digit (spec 1.6; decision 124).
    assert f'"observed_value":{value},' in text.replace(" ", "")
    assert f'"threshold_value":{limit},' in text.replace(" ", "")
    parsed = json.loads(text, parse_float=Decimal)
    assert parsed["observed_value"] == Decimal(value)
    assert parsed["threshold_value"] == Decimal(limit)


def test_list_returns_summaries_newest_first(client: ApiClient) -> None:
    a = post(client, timestamp="2026-09-27T10:05:00Z").json()["flagged_event_id"]
    post(client, value=0.10, timestamp="2026-09-27T10:06:00Z")
    b = post(client, timestamp="2026-09-27T10:07:00Z").json()["flagged_event_id"]
    post(client, value=0.10, timestamp="2026-09-27T10:08:00Z")
    c = post(client, timestamp="2026-09-27T09:00:00Z").json()["flagged_event_id"]
    # An accuracy event with b's detected_at: ties are broken by event_id (ascending,
    # decision 117).
    d = post(
        client,
        metric_name="accuracy",
        metric_type="performance",
        value=0.5,
        timestamp="2026-09-27T10:07:00Z",
    ).json()["flagged_event_id"]
    tied = sorted([b, d])

    response = get(client, "/v1/events")
    assert response.status_code == 200
    assert ids(response) == [*tied, a, c]
    by_id = {item["event_id"]: item for item in response.json()}
    assert by_id[a] == {
        "event_id": a,
        "system_id": "sys-42",
        "type": "threshold_breach",
        "metric_name": "accuracy_drift",
        "detected_at": "2026-09-27T10:05:00Z",
        "state": "FLAGGED",
    }
    assert all(set(item) == SUMMARY_KEYS for item in response.json())


def make_gap_then_breach(
    client: ApiClient, app_engine: Engine, fake_clock: FakeClock
) -> tuple[str, str]:
    """A CLOSED gap event (detected at T0 + 900 s) and a FLAGGED breach at 10:25:00Z."""
    post(client, value=0.10)
    fake_clock.advance(900)
    [gap] = gaps.sweep(app_engine)
    breach = post(client, timestamp="2026-09-27T10:25:00Z").json()["flagged_event_id"]
    return gap, breach


def test_list_filters(client: ApiClient, app_engine: Engine, fake_clock: FakeClock) -> None:
    gap, breach = make_gap_then_breach(client, app_engine, fake_clock)
    # The gap was detected at 10:20:00Z (T0 + 900 s) and closed by the breach's sample.
    cases = {
        "": [breach, gap],
        "?system_id=sys-42": [breach, gap],
        "?system_id=sys-99": [],
        "?state=FLAGGED": [breach],
        "?state=CLOSED": [gap],
        "?state=UNDER_REVIEW": [],
        "?state=DISPOSITIONED": [],
        "?type=threshold_breach": [breach],
        "?type=monitoring_gap": [gap],
        "?since=2026-09-27T10:20:00Z": [breach, gap],  # detected_at >= since
        "?since=2026-09-27T10:20:00%2B00:00": [breach, gap],
        "?since=2026-09-27T10:20:00.001Z": [breach],
        "?since=2026-09-27T10:25:00Z": [breach],
        "?since=2026-09-27T10:25:01Z": [],
        "?type=monitoring_gap&state=CLOSED&system_id=sys-42": [gap],
        "?type=monitoring_gap&state=FLAGGED": [],
    }
    for query, expected in cases.items():
        response = get(client, "/v1/events" + query)
        assert response.status_code == 200, query
        assert ids(response) == expected, query


BAD_QUERIES = [
    "state=OPEN",
    "state=flagged",
    "type=breach",
    "since=yesterday",
    "since=2026-09-27T10:05:00",
    "since=2026-09-27T12:05:00%2B02:00",
    "limit=abc",
    "limit=-1",
    "limit=1.5",
    "limit=",
    "offset=-1",
    "offset=x",
]


@pytest.mark.parametrize("query", BAD_QUERIES)
def test_list_bad_filters_are_400(client: ApiClient, owner_engine: Engine, query: str) -> None:
    response = get(client, "/v1/events?" + query)
    assert response.status_code == 400
    assert response.json() == MALFORMED
    assert entry_types(owner_engine) == ["SEED"]


def test_list_limit_and_offset(client: ApiClient, owner_engine: Engine) -> None:
    """limit defaults to 50 and is capped at 200; offset defaults to 0 (spec 1.6; decision
    118). 205 events, one second apart, newest first."""
    with owner_engine.begin() as conn:
        for i in range(205):
            insert_event(
                conn,
                f"evt-{i:03d}",
                event_type="threshold_breach",
                detected_at=T0 + timedelta(seconds=i),
            )
    newest_first = [f"evt-{i:03d}" for i in reversed(range(205))]
    cases = {
        "": newest_first[:50],
        "?limit=2": newest_first[:2],
        "?limit=2&offset=2": newest_first[2:4],
        "?offset=200": newest_first[200:205],
        "?offset=205": [],
        "?limit=0": [],
        "?limit=200": newest_first[:200],
        "?limit=201": newest_first[:200],
        "?limit=100000&offset=1": newest_first[1:201],
    }
    for query, expected in cases.items():
        assert ids(get(client, "/v1/events" + query)) == expected, query


@pytest.mark.parametrize("actor", ["sys-42", "rev-1", "viewer-1"])
def test_list_accepts_every_role_and_writes_nothing(
    client: ApiClient, owner_engine: Engine, actor: str
) -> None:
    post(client)
    before = entry_types(owner_engine)
    response = get(client, "/v1/events", actor)
    assert response.status_code == 200
    assert len(response.json()) == 1
    assert entry_types(owner_engine) == before
    assert "x-total-count" not in response.headers


def test_list_without_a_key_is_401(client: ApiClient) -> None:
    response = client.get("/v1/events")
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)


def test_gap_events_in_list_and_detail(
    client: ApiClient, app_engine: Engine, fake_clock: FakeClock
) -> None:
    """A gap appears with no ingest call (spec 3 table); its detail is the Phase 3 stub shape
    for a gap (spec 4): no metric or values, four NOT_APPLICABLE harm fields."""
    fake_clock.advance(900)  # measured from the seed time
    [gap] = gaps.sweep(app_engine)
    assert get(client, "/v1/events?type=monitoring_gap").json() == [
        {
            "event_id": gap,
            "system_id": "sys-42",
            "type": "monitoring_gap",
            "metric_name": None,
            "detected_at": "2026-09-27T10:20:00Z",
            "state": "FLAGGED",
        }
    ]
    detail = get(client, f"/v1/events/{gap}").json()
    assert detail == {
        "event_id": gap,
        "system_id": "sys-42",
        "type": "monitoring_gap",
        "metric_name": None,
        "observed_value": None,
        "threshold_value": None,
        "detected_at": "2026-09-27T10:20:00Z",
        "state": "FLAGGED",
        "disposition": None,
        "deadline_utc": None,
        "revisions": [],
        "harm_fields": {
            key: {
                "status": "NOT_APPLICABLE",
                "evidence": None,
                "confirmed_by": None,
                "confirmed_at": None,
            }
            for key in HARM_FIELDS
        },
    }


NO_DISPOSITION = {
    "error_code": "NO_DISPOSITION",
    "message": "no disposition has been recorded for this event",
}


@pytest.mark.parametrize("exists", [True, False], ids=["existing-event", "unknown-event"])
def test_disposition_and_report_check_the_key_and_role_first(
    client: ApiClient, owner_engine: Engine, exists: bool
) -> None:
    """Phase 4 Part 2 (Addendum 9.10; was the 501 stub test of spec 1.6). Both routes accept
    REVIEWER only, and check the key and the role before anything else (Phase 4 spec 2.4 and
    2.8). After the role check the disposition reads its body, so a broken one is 400 whether or
    not the event exists (the pinned order puts 400 before 404); the report looks the event up,
    so it is 409 NO_DISPOSITION for an event without a disposition and 404 for an unknown one.
    Nothing is appended."""
    event_id = post(client).json()["flagged_event_id"] if exists else "evt-nope"
    before = entry_types(owner_engine)
    disposition = f"/v1/events/{event_id}/disposition"
    report = f"/v1/events/{event_id}/report"
    cases = [
        ("POST", disposition, "rev-1", 400, MALFORMED),
        ("POST", disposition, "viewer-1", 403, FORBIDDEN),
        ("POST", disposition, "sys-42", 403, FORBIDDEN),
        ("POST", disposition, None, 401, UNAUTHORIZED),
        ("GET", report, "rev-1", *((409, NO_DISPOSITION) if exists else (404, NOT_FOUND))),
        ("GET", report, "viewer-1", 403, FORBIDDEN),
        ("GET", report, "sys-42", 403, FORBIDDEN),
        ("GET", report, None, 401, UNAUTHORIZED),
    ]
    for method, url, actor, status, error in cases:
        headers = {} if actor is None else bearer(actor)
        response = client.request(method, url, headers=headers, content="{not json")
        assert (response.status_code, response.json()) == (status, error), (method, url, actor)
    assert entry_types(owner_engine) == before
