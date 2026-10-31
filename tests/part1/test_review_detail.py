"""Phase 4 Part 1: the Event detail (Phase 4 spec 1.6).

events.detail keeps the contract's shape and field order, takes disposition, revisions and
deadline_utc from dispositions.summary (Part 2; the base commit's stub until then) and the harm
fields from their rows, and never exposes prefill_evidence.

Contract Example 2 is typed out below exactly as the contract prints it. Raw response text is
read with object_pairs_hook and parse_float=str, so key order and the digits of every number
(0.30, not 0.3) are compared, not just dict equality.
"""

import json
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import dispositions, gaps
from app.auth import Principal
from app.review import apply_harm_fields, parse_harm_fields, require_complete
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer, insert_event
from tests.part1.conftest import T0, get, metric, post
from tests.part1.test_review_prefill import AC07_EVIDENCE, RIGHTS, post_ac07
from tests.worker.conftest import FakeClock

# Contract Example 2 (GET /v1/events/evt-7f3a), as printed.
EXAMPLE_2 = """
{
  "event_id": "evt-7f3a",
  "system_id": "sys-42", "type": "threshold_breach",
  "metric_name": "accuracy_drift", "observed_value": 0.34,
  "threshold_value": 0.30,
  "detected_at": "2026-09-27T10:05:00Z", "state": "DISPOSITIONED",
  "disposition": {
    "tier": "OTHER_SERIOUS",
    "rationale": null, "reviewer_id": "rev-1",
    "assigned_at": "2026-09-27T10:12:00Z"
  },
  "deadline_utc": "2026-10-12T10:12:00Z", "revisions": [],
  "harm_fields": { "health_safety_harm": {
      "status": "NOT_APPLICABLE", "evidence": null,
      "confirmed_by": "rev-1", "confirmed_at": "2026-09-27T10:12:00Z"
    },
    "critical_infrastructure_disruption": {
      "status": "CONFIRMED", "evidence": "drift affected routing outputs", "confirmed_by": "rev-1", "confirmed_at": "2026-09-27T10:12:00Z"
    },
    "fundamental_rights_infringement": {
      "status": "NOT_APPLICABLE", "evidence": null,
      "confirmed_by": "rev-1", "confirmed_at": "2026-09-27T10:12:00Z"
    },
    "property_environmental_damage": {
      "status": "NOT_APPLICABLE", "evidence": null,
      "confirmed_by": "rev-1", "confirmed_at": "2026-09-27T10:12:00Z"
    }
  }
}
"""  # noqa: E501
ASSIGNED_AT = "2026-09-27T10:12:00Z"


def pairs(raw: str) -> Any:
    """The JSON text as nested lists of (key, value) pairs in text order, numbers as written."""
    return json.loads(raw, object_pairs_hook=list, parse_float=str, parse_int=str)


def example_2_summary(session: Session, event_id: str) -> dict[str, Any]:
    """Part 2's summary for Example 2's event: its disposition, revisions and deadline."""
    assert event_id == "evt-7f3a"
    return {
        "disposition": {
            "tier": "OTHER_SERIOUS",
            "rationale": None,
            "reviewer_id": "rev-1",
            "assigned_at": ASSIGNED_AT,
        },
        "revisions": [],
        "deadline_utc": "2026-10-12T10:12:00Z",
    }


def store_example_2(owner_engine: Engine, deadline: str | None) -> None:
    """Example 2's event row and four harm fields, as the database holds them after rev-1's
    disposition at 10:12 (the ledger is not read by the detail)."""
    with owner_engine.begin() as conn:
        insert_event(conn, "evt-7f3a", event_type="threshold_breach", detected_at=T0)
        conn.execute(
            text(
                "UPDATE event SET state = 'DISPOSITIONED', deadline_utc = :deadline"
                " WHERE event_id = 'evt-7f3a'"
            ),
            {"deadline": deadline},
        )
        conn.execute(
            text(
                "UPDATE harm_field SET status = 'NOT_APPLICABLE', evidence = NULL,"
                " confirmed_by = 'rev-1', confirmed_at = :at WHERE event_id = 'evt-7f3a'"
            ),
            {"at": ASSIGNED_AT},
        )
        conn.execute(
            text(
                "UPDATE harm_field SET status = 'CONFIRMED',"
                " evidence = 'drift affected routing outputs'"
                " WHERE event_id = 'evt-7f3a' AND field_key = 'critical_infrastructure_disruption'"
            )
        )


def test_the_detail_of_example_2_is_example_2_exactly(
    client: ApiClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    store_example_2(owner_engine, "2026-10-12T10:12:00Z")
    monkeypatch.setattr(dispositions, "summary", example_2_summary)
    response = get(client, "/v1/events/evt-7f3a")
    assert response.status_code == 200
    assert pairs(response.text) == pairs(EXAMPLE_2)


def test_disposition_revisions_and_deadline_come_from_the_summary(
    client: ApiClient, owner_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The stored deadline is not read by the detail: the summary's value is shown."""
    store_example_2(owner_engine, None)
    revision = {
        "prior_tier": "OTHER_SERIOUS",
        "new_tier": "DEATH_OR_SERIOUS_HARM",
        "reviewer_id": "rev-1",
        "revised_at": "2026-09-27T11:00:00Z",
    }

    def summary(session: Session, event_id: str) -> dict[str, Any]:
        return {**example_2_summary(session, event_id), "revisions": [revision]}

    monkeypatch.setattr(dispositions, "summary", summary)
    body = get(client, "/v1/events/evt-7f3a").json()
    assert body["disposition"]["tier"] == "OTHER_SERIOUS"
    assert body["deadline_utc"] == "2026-10-12T10:12:00Z"
    assert body["revisions"] == [revision]
    assert list(body["revisions"][0]) == ["prior_tier", "new_tier", "reviewer_id", "revised_at"]


@pytest.mark.parametrize(
    ("stored", "shown"), [("2026-10-12T10:12:00Z", "2026-10-12T10:12:00Z"), (None, None)]
)
def test_the_stub_summary_gives_null_disposition_no_revisions_and_the_stored_deadline(
    client: ApiClient, owner_engine: Engine, stored: str | None, shown: str | None
) -> None:
    store_example_2(owner_engine, stored)
    expected = json.loads(EXAMPLE_2)
    expected.update(disposition=None, deadline_utc=shown, revisions=[])
    response = get(client, "/v1/events/evt-7f3a")
    assert response.json() == expected
    assert [key for key, _ in pairs(response.text)] == [key for key, _ in pairs(EXAMPLE_2)]


def test_a_gap_event_detail_is_unchanged(
    client: ApiClient, app_engine: Engine, fake_clock: FakeClock
) -> None:
    fake_clock.advance(900)
    [gap] = gaps.sweep(app_engine)
    response = get(client, f"/v1/events/{gap}")
    assert response.status_code == 200
    blank = '{"status":"NOT_APPLICABLE","evidence":null,"confirmed_by":null,"confirmed_at":null}'
    assert response.text == (
        f'{{"event_id":"{gap}","system_id":"sys-42","type":"monitoring_gap",'
        '"metric_name":null,"observed_value":null,"threshold_value":null,'
        '"detected_at":"2026-09-27T10:20:00Z","state":"FLAGGED","disposition":null,'
        '"deadline_utc":null,"revisions":[],"harm_fields":{'
        f'"health_safety_harm":{blank},"critical_infrastructure_disruption":{blank},'
        f'"fundamental_rights_infringement":{blank},"property_environmental_damage":{blank}}}}}'
    )


# Every route of the service, each with an allowed key and a minimal request, in an order that
# also works once Part 2's routes are real: the disposition before the report. Kept here rather
# than taken from tests/part1/test_auth.py, which Part 2 edits. The disposition keeps the
# reviewer's own rights text, so it never puts the pre-fill back.
DISPOSITION = {
    "tier": "OTHER_SERIOUS",
    "rationale": None,
    "harm_fields": {
        "health_safety_harm": {"status": "NOT_APPLICABLE", "evidence": None},
        "critical_infrastructure_disruption": {"status": "NOT_APPLICABLE", "evidence": None},
        RIGHTS: {"status": "CONFIRMED", "evidence": "reviewer text"},
        "property_environmental_damage": {"status": "NOT_APPLICABLE", "evidence": None},
    },
}
EVERY_ROUTE: list[tuple[str, str, str | None, Any]] = [
    ("POST", "/v1/metrics", "sys-42", metric(timestamp="2026-10-05T09:00:00Z")),
    ("GET", "/v1/events", "viewer-1", None),
    ("GET", "/v1/events/{id}", "viewer-1", None),
    ("GET", "/v1/events/{id}/audit", "viewer-1", None),
    ("GET", "/v1/ledger/export", "viewer-1", None),
    ("GET", "/v1/ledger/verify", "viewer-1", None),
    ("POST", "/v1/events/{id}/disposition", "rev-1", DISPOSITION),
    ("GET", "/v1/events/{id}/report", "viewer-1", None),
    ("GET", "/healthz", None, None),
]


def every_response(client: ApiClient, event_id: str) -> list[str]:
    """One response from every route in EVERY_ROUTE."""
    texts = []
    for method, path, actor, body in EVERY_ROUTE:
        response = client.request(
            method,
            path.replace("{id}", event_id),
            headers={} if actor is None else bearer(actor),
            json=body,
        )
        texts.append(response.text)
    return texts


def test_prefill_evidence_is_in_no_response_body(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = post_ac07(client, fake_clock)
    for body in every_response(client, event_id):
        assert "prefill_evidence" not in body
    # After a reviewer replaces the pre-fill, the original text is in the database only.
    decisions = require_complete(
        parse_harm_fields(
            {
                "health_safety_harm": {"status": "NOT_APPLICABLE"},
                "critical_infrastructure_disruption": {"status": "NOT_APPLICABLE"},
                RIGHTS: {"status": "CONFIRMED", "evidence": "reviewer text"},
                "property_environmental_damage": {"status": "NOT_APPLICABLE"},
            }
        )
    )
    with Session(app_engine) as session:
        session.execute(text("SELECT 1 FROM event WHERE event_id = :e FOR UPDATE"), {"e": event_id})
        apply_harm_fields(session, event_id, Principal("REVIEWER", "rev-1", None), decisions, T0)
        session.commit()
    texts = every_response(client, event_id)
    assert any("reviewer text" in body for body in texts)
    for body in texts:
        assert "prefill_evidence" not in body
        assert AC07_EVIDENCE not in body
    with owner_engine.connect() as conn:
        assert (
            conn.execute(
                text(
                    "SELECT prefill_evidence FROM harm_field WHERE event_id = :e AND field_key = :k"
                ),
                {"e": event_id, "k": RIGHTS},
            ).scalar_one()
            == AC07_EVIDENCE
        )


def test_detected_at_survives_the_review_start_and_the_harm_decisions(
    client: ApiClient, app_engine: Engine
) -> None:
    event_id = post(client).json()["flagged_event_id"]
    assert get(client, f"/v1/events/{event_id}", actor="rev-1").json()["state"] == "UNDER_REVIEW"
    decisions = require_complete(
        parse_harm_fields(
            {
                key: {"status": "NOT_APPLICABLE"}
                for key in (
                    "health_safety_harm",
                    "critical_infrastructure_disruption",
                    RIGHTS,
                    "property_environmental_damage",
                )
            }
        )
    )
    with Session(app_engine) as session:
        session.execute(text("SELECT 1 FROM event WHERE event_id = :e FOR UPDATE"), {"e": event_id})
        apply_harm_fields(session, event_id, Principal("REVIEWER", "rev-1", None), decisions, T0)
        session.commit()
    assert get(client, f"/v1/events/{event_id}").json()["detected_at"] == "2026-09-27T10:05:00Z"
