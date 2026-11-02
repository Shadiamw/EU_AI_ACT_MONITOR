"""The four Part 2 coverage gaps found by the Parts 1-2 verification (integration decision 221).

Written by the integration, in a file of its own, so that no Part 2 test file changes. The gaps
are the NOT TESTED clauses of the independent Part 2 audit (parts12-verification.md section 5):

- N5: a blank rationale (absent, empty or whitespace only) never extends a deadline, also on a
  reopen (Addendum rule 1; spec 2.3 "blank").
- M14: the harm fields, the disposition row and the event update are written before
  escalations.plan runs (the pinned order of Guide 8 and spec 2.4; now a real Part 3 seam).
- N1, revision case: a revision with unconfirmed harm fields is 409 and writes nothing.
- N2 and N3: a same-tier revision and a looser revision with a rationale stay DISPOSITIONED.

Clock: FakeClock throughout (Addendum rule 10); the HTTP test endpoints are never used.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import escalations
from tests.api.conftest import ApiClient
from tests.db_helpers import HARM_FIELDS, bearer
from tests.part1.test_review_decisions import INCOMPLETE_INPUTS
from tests.part2.conftest import (
    ANCHOR,
    EXTENSION,
    UNCONFIRMED,
    decided,
    dispose,
    event_row,
    flag,
    parse,
    snapshot,
)
from tests.worker.conftest import FakeClock

DAY_SECONDS = 86400
CONFIRMED_RIGHTS = {
    **decided(),
    "fundamental_rights_infringement": {"status": "CONFIRMED", "evidence": None},
}


def first(client: ApiClient, fake_clock: FakeClock, tier: str, **kwargs: Any) -> str:
    """Flag contract Example 1 and record the first disposition at ANCHOR."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    response = dispose(client, event_id, tier, **kwargs)
    assert response.status_code == 200, response.text
    return event_id


def raw_dispose(client: ApiClient, event_id: str, request: dict[str, Any]) -> Any:
    return client.request(
        "POST", f"/v1/events/{event_id}/disposition", headers=bearer("rev-1"), json=request
    )


def with_xmin(owner_engine: Engine, event_id: str) -> dict[str, Any]:
    """snapshot() plus the event row's xmin, so even a rewrite to the same values shows."""
    with owner_engine.connect() as conn:
        xmin = conn.execute(
            text("SELECT xmin::text FROM event WHERE event_id = :e"), {"e": event_id}
        ).scalar_one()
    return {**snapshot(owner_engine, event_id), "xmin": xmin}


# N5: a blank rationale never extends a deadline.

BLANK = {"absent key": None, "empty": "", "spaces": "   ", "tab and newline": "\t\n"}


@pytest.mark.parametrize("blank", BLANK.keys())
@pytest.mark.parametrize("path", ["DEATH then OTHER_SERIOUS", "reopen to OTHER_SERIOUS"])
def test_a_blank_rationale_never_extends_a_deadline(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, path: str, blank: str
) -> None:
    """Addendum rule 1 cases 1 and 5: a looser serious tier than the most recent serious one
    (DEATH) with a blank rationale is 409 DEADLINE_EXTENSION_REQUIRES_RATIONALE, exact body,
    and nothing is written: the deadline stays ANCHOR + 10 days, or null and CLOSED on the
    reopen path."""
    event_id = first(client, fake_clock, "DEATH")
    if path == "reopen to OTHER_SERIOUS":
        fake_clock.advance(DAY_SECONDS)
        closed = dispose(client, event_id, "NOT_SERIOUS", "assessed, not serious")
        assert closed.status_code == 200, closed.text
    fake_clock.advance(DAY_SECONDS)
    before = with_xmin(owner_engine, event_id)
    request: dict[str, Any] = {"tier": "OTHER_SERIOUS", "harm_fields": decided()}
    if blank != "absent key":
        request["rationale"] = BLANK[blank]
    response = raw_dispose(client, event_id, request)
    assert (response.status_code, response.json()) == (409, EXTENSION)
    assert with_xmin(owner_engine, event_id) == before
    event = event_row(owner_engine, event_id)
    if path == "reopen to OTHER_SERIOUS":
        assert (event["state"], event["deadline_utc"]) == ("CLOSED", None)
    else:
        assert (event["state"], event["deadline_utc"]) == ("DISPOSITIONED", parse_day(10))


def parse_day(days: int) -> datetime:
    """ANCHOR plus whole calendar days (the default calendar)."""
    return ANCHOR + timedelta(days=days)


# M14: the harm fields are written before the escalation plan.


@pytest.mark.integration
@pytest.mark.parametrize("revision", [False, True], ids=["first", "revision"])
def test_the_harm_fields_are_written_before_the_escalation_plan(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    revision: bool,
) -> None:
    """Spec 2.4 and Guide 8, the pinned order: lock the event row, then harm fields, the
    disposition row, the event update, escalations.plan, and the ledger append last. Inside
    plan, in the same transaction, the four fields already carry the request's statuses with
    the reviewer and the call's time (a revision keeps the earlier time on the fields it does not
    change), the new disposition row exists, the event row is updated,
    and no ledger entry for this call has been appended yet. Needs Part 3's real plan."""
    event_id = flag(client)
    fake_clock.set(ANCHOR)
    if revision:
        assert dispose(client, event_id, "OTHER_SERIOUS").status_code == 200
        fake_clock.advance(3 * DAY_SECONDS)
    called_at = fake_clock.now()
    rows_before = 1 if revision else 0
    with owner_engine.connect() as conn:
        ledger_before = conn.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one()
    seen: list[dict[str, Any]] = []
    real_plan = escalations.plan

    def recording_plan(session: Session, event_id: str, anchor: Any, deadline: Any) -> None:
        seen.append(
            {
                "fields": [
                    tuple(row)
                    for row in session.execute(
                        text(
                            "SELECT field_key, status, confirmed_by, confirmed_at FROM harm_field"
                            " WHERE event_id = :e ORDER BY field_key"
                        ),
                        {"e": event_id},
                    )
                ],
                "dispositions": session.execute(
                    text("SELECT count(*) FROM disposition WHERE event_id = :e"), {"e": event_id}
                ).scalar_one(),
                "event": tuple(
                    session.execute(
                        text("SELECT state, deadline_utc FROM event WHERE event_id = :e"),
                        {"e": event_id},
                    ).one()
                ),
                "ledger": session.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one(),
            }
        )
        real_plan(session, event_id, anchor, deadline)

    monkeypatch.setattr(escalations, "plan", recording_plan)
    response = dispose(client, event_id, "DEATH", harm_fields=CONFIRMED_RIGHTS)
    assert response.status_code == 200, response.text
    statuses = {
        key: "CONFIRMED" if key == "fundamental_rights_infringement" else "NOT_APPLICABLE"
        for key in HARM_FIELDS
    }

    # A revision touches only the fields whose decision changed (spec 1.5, Addendum rule 2): the
    # three NOT_APPLICABLE fields keep the first call's confirmed_at.
    def confirmed_at(key: str) -> Any:
        unchanged = revision and key != "fundamental_rights_infringement"
        return ANCHOR if unchanged else called_at

    assert seen == [
        {
            "fields": [
                (key, statuses[key], "rev-1", confirmed_at(key)) for key in sorted(HARM_FIELDS)
            ],
            "dispositions": rows_before + 1,
            "event": ("DISPOSITIONED", parse_day(10)),
            "ledger": ledger_before,
        }
    ]


# N1, revision case: a revision with unconfirmed harm fields writes nothing.

UNCONFIRMED_REQUESTS: dict[str, dict[str, Any]] = {
    "harm_fields absent": {"tier": "DEATH", "rationale": None},
    "harm_fields null": {"tier": "DEATH", "rationale": None, "harm_fields": None},
    **{
        name: {"tier": "DEATH", "rationale": None, "harm_fields": raw}
        for name, (raw, _) in INCOMPLETE_INPUTS.items()
        if raw is not None
    },
}


@pytest.mark.parametrize("request_body", UNCONFIRMED_REQUESTS.values(), ids=UNCONFIRMED_REQUESTS)
def test_a_revision_with_unconfirmed_harm_fields_writes_nothing(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    request_body: dict[str, Any],
) -> None:
    """Spec 1.5 second row, on an event that already has a disposition: 409
    HARM_FIELDS_UNCONFIRMED, exact body, and the event row (with its xmin), the four harm fields,
    the disposition rows and the whole ledger are unchanged (spec 7, "nothing is written")."""
    event_id = first(client, fake_clock, "OTHER_SERIOUS")
    fake_clock.advance(DAY_SECONDS)
    before = with_xmin(owner_engine, event_id)
    assert len(before["dispositions"]) == 1
    response = raw_dispose(client, event_id, request_body)
    assert (response.status_code, response.json()) == (409, UNCONFIRMED)
    assert with_xmin(owner_engine, event_id) == before


# N2 and N3: same-tier and looser revisions stay DISPOSITIONED.


@pytest.mark.parametrize(
    ("tiers", "rationale", "days"),
    [
        (("OTHER_SERIOUS", "OTHER_SERIOUS"), None, 15),
        (("DEATH", "OTHER_SERIOUS"), "the harm was not a death", 15),
    ],
    ids=["same tier", "looser with a rationale"],
)
def test_same_tier_and_looser_revisions_stay_dispositioned(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    tiers: tuple[str, str],
    rationale: str | None,
    days: int,
) -> None:
    """Spec 2.5 revision table: serious to the same tier, and serious to a looser tier with a
    rationale, leave the event DISPOSITIONED with deadline_utc = ANCHOR + the new tier's days."""
    event_id = first(client, fake_clock, tiers[0])
    fake_clock.advance(DAY_SECONDS)
    response = dispose(client, event_id, tiers[1], rationale)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["state"] == "DISPOSITIONED"
    assert parse(body["deadline_utc"]) == parse_day(days)
    detail = client.get(f"/v1/events/{event_id}", headers=bearer("viewer-1")).json()
    assert detail["state"] == "DISPOSITIONED"
    event = event_row(owner_engine, event_id)
    assert (event["state"], event["deadline_utc"]) == ("DISPOSITIONED", parse_day(days))
