"""Phase 5 Part 3, M3: the projection check (spec 6.3, D13; decisions 304 and 305).

The data is built through the real app (real keys, the fake clock, PostgreSQL 16), never by
inserting rows, so the ledger entries are the ones the server writes. Edits are made afterwards
as the owner role, which is the attack the check exists for: the chain covers ledger rows, not
the event, disposition or harm_field rows. Expected mismatches are reasoned out from spec 6.3 in
comments; they are not copied from output.
"""

from datetime import timedelta
from typing import Any

import httpx
import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import gaps
from app.formats import api_time
from app.ledger.verify import verify_ledger
from tests.api.conftest import ApiClient
from tests.db_helpers import HARM_FIELDS, bearer
from tests.part1.conftest import get, post, rows
from tests.worker.conftest import FakeClock

HEALTH, INFRASTRUCTURE, RIGHTS, PROPERTY = HARM_FIELDS

# Four complete decisions with two different statuses, so a harm_field edit would show.
DECIDED = {
    HEALTH: {"status": "NOT_APPLICABLE", "evidence": None},
    INFRASTRUCTURE: {"status": "NOT_APPLICABLE", "evidence": None},
    RIGHTS: {"status": "CONFIRMED", "evidence": None},
    PROPERTY: {"status": "NOT_APPLICABLE", "evidence": None},
}

PART1_MISSING = "report assembly and submission (Phase 5 Part 1) are not on this branch"


def projection(engine: Engine) -> list[dict[str, Any]]:
    """ledger_ops.projection_check in its own session, as the app role (the worker's role).
    Imported here, so each test fails by name until M3's code commit adds the module."""
    from app import ledger_ops

    with Session(engine) as session:
        return ledger_ops.projection_check(session)


def sample(client: ApiClient, fake_clock: FakeClock, **changes: Any) -> httpx.Response:
    """A sample one minute after the previous step, stamped with the fake clock's time."""
    fake_clock.advance(60)
    stamp = fake_clock.now().isoformat().replace("+00:00", "Z")
    response = post(client, timestamp=stamp, **changes)
    assert response.status_code == 201, response.text
    return response


def breach(client: ApiClient, fake_clock: FakeClock, **changes: Any) -> str:
    event_id: str | None = sample(client, fake_clock, **changes).json()["flagged_event_id"]
    assert event_id is not None
    return event_id


def view(client: ApiClient, event_id: str, actor: str) -> dict[str, Any]:
    response = get(client, f"/v1/events/{event_id}", actor=actor)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def dispose(
    client: ApiClient, event_id: str, tier: str, rationale: str | None = None
) -> httpx.Response:
    return client.request(
        "POST",
        f"/v1/events/{event_id}/disposition",
        headers=bearer("rev-1"),
        json={"tier": tier, "rationale": rationale, "harm_fields": DECIDED},
    )


def state(engine: Engine, event_id: str) -> str:
    [row] = rows(engine, "SELECT state FROM event WHERE event_id = :e", e=event_id)
    value: str = row["state"]
    return value


def build_history(client: ApiClient, fake_clock: FakeClock, app_engine: Engine) -> dict[str, Any]:
    """Every kind of entry the projection replays, through the real app:

    - gap G1 opened by the sweeper and closed by the next sample (GAP_OPENED, GAP_CLOSED);
    - breach A, viewed by the reviewer (REVIEW_STARTED), dispositioned OTHER_SERIOUS and revised
      to DEATH a day later (DISPOSITION_RECORDED, DISPOSITION_REVISED: a revision chain);
    - breach B on the other metric, closed NOT_SERIOUS with a rationale (deadline null);
    - gap G2 opened and left open (FLAGGED).
    """
    fake_clock.advance(900)
    [gap_closed] = gaps.sweep(app_engine)

    event_a = breach(client, fake_clock)  # accuracy_drift 0.34 > 0.30; closes G1
    view(client, event_a, "viewer-1")
    assert view(client, event_a, "rev-1")["state"] == "UNDER_REVIEW"
    fake_clock.advance(4 * 3600)
    first = dispose(client, event_a, "OTHER_SERIOUS")
    assert first.status_code == 200, first.text
    fake_clock.advance(24 * 3600)
    revised = dispose(client, event_a, "DEATH")
    assert revised.status_code == 200, revised.text
    assert revised.json()["state"] == "DISPOSITIONED"

    event_b = breach(
        client, fake_clock, metric_name="accuracy", metric_type="performance", value=0.61
    )
    view(client, event_b, "rev-1")
    closed = dispose(client, event_b, "NOT_SERIOUS", "within tolerance after manual check")
    assert closed.status_code == 200, closed.text
    assert closed.json()["state"] == "CLOSED"
    assert closed.json()["deadline_utc"] is None

    fake_clock.advance(900)
    [gap_open] = gaps.sweep(app_engine)

    assert state(app_engine, gap_closed) == "CLOSED"
    assert state(app_engine, gap_open) == "FLAGGED"
    assert rows(
        app_engine, "SELECT count(*) AS n FROM disposition WHERE event_id = :e", e=event_a
    ) == [{"n": 2}]
    return {
        "a": event_a,
        "b": event_b,
        "gap_closed": gap_closed,
        "gap_open": gap_open,
        "deadline_a": revised.json()["deadline_utc"],
    }


def test_projection_check_finds_an_edited_event_state(
    client: ApiClient, fake_clock: FakeClock, app_engine: Engine, owner_engine: Engine
) -> None:
    history = build_history(client, fake_clock, app_engine)
    # Unedited, every row is what the ledger says it should be.
    assert projection(app_engine) == []

    event_a = history["a"]
    assert state(owner_engine, event_a) == "DISPOSITIONED"
    with owner_engine.begin() as conn:
        conn.execute(text("UPDATE event SET state = 'CLOSED' WHERE event_id = :e"), {"e": event_a})

    # A's latest DISPOSITION_* tier is DEATH, so the ledger says DISPOSITIONED; the row now
    # says CLOSED. Nothing else was touched, so this is the only mismatch.
    assert projection(app_engine) == [
        {
            "event_id": event_a,
            "column": "event.state",
            "expected": "DISPOSITIONED",
            "actual": "CLOSED",
        }
    ]


def test_projection_check_finds_an_edited_deadline(
    client: ApiClient, fake_clock: FakeClock, app_engine: Engine, owner_engine: Engine
) -> None:
    history = build_history(client, fake_clock, app_engine)
    assert projection(app_engine) == []

    event_a, deadline = history["a"], history["deadline_a"]
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE event SET deadline_utc = deadline_utc + interval '1 day'"
                " WHERE event_id = :e"
            ),
            {"e": event_a},
        )
    [row] = rows(owner_engine, "SELECT deadline_utc FROM event WHERE event_id = :e", e=event_a)

    # The latest DISPOSITION_* payload (DISPOSITION_REVISED to DEATH) carries the deadline the
    # API returned; the row is now one day later. The disposition rows were not touched, so
    # only event.deadline_utc differs.
    assert api_time(row["deadline_utc"] - timedelta(days=1)) == deadline
    assert projection(app_engine) == [
        {
            "event_id": event_a,
            "column": "event.deadline_utc",
            "expected": deadline,
            "actual": api_time(row["deadline_utc"]),
        }
    ]


@pytest.mark.integration
def test_projection_check_is_clean_after_the_ac16_trace(
    client: ApiClient, fake_clock: FakeClock, app_engine: Engine
) -> None:
    """Both AC-16 paths (spec 10.3) in process through the ASGI app, then the projection check
    finds nothing. Needs Part 1's report assembly and submission (red until Part 1 merges)."""
    # Path S. S2: breach; S3: no report before a disposition.
    event_e = breach(client, fake_clock)
    response = get(client, f"/v1/events/{event_e}/report", actor="rev-1")
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "NO_DISPOSITION"
    # S4: viewer, then reviewer.
    view(client, event_e, "viewer-1")
    assert view(client, event_e, "rev-1")["state"] == "UNDER_REVIEW"
    # S5 and S6.
    fake_clock.advance(4 * 3600)
    assert dispose(client, event_e, "OTHER_SERIOUS").status_code == 200
    fake_clock.advance(24 * 3600)
    assert dispose(client, event_e, "DEATH").status_code == 200
    # S7: the report.
    fake_clock.advance(3600)
    url = f"/v1/events/{event_e}/report"
    report = get(client, url, actor="rev-1")
    assert report.status_code == 200, (
        f"{PART1_MISSING}: GET {url} gave {report.status_code} {report.text}"
    )
    # S8: submission.
    submission = client.request(
        "POST",
        f"{url}/submission",
        headers=bearer("rev-1"),
        json={
            "report_id": report.headers["X-Report-Id"],
            "json_sha256": report.headers["X-Report-Sha256"],
            "report_type": "Initial",
        },
    )
    assert submission.status_code == 201, (
        f"{PART1_MISSING}: POST {url}/submission gave {submission.status_code} {submission.text}"
    )

    # A healthy sample ends the breach series, so the next breach is a new event (path N).
    assert "flagged_event_id" not in sample(client, fake_clock, value=0.10).json()
    event_f = breach(client, fake_clock)
    assert event_f != event_e
    view(client, event_f, "rev-1")
    refused = dispose(client, event_f, "NOT_SERIOUS")
    assert refused.status_code == 400
    assert refused.json()["error_code"] == "RATIONALE_REQUIRED"
    closed = dispose(client, event_f, "NOT_SERIOUS", "within tolerance after manual check")
    assert closed.status_code == 200
    assert (closed.json()["state"], closed.json()["deadline_utc"]) == ("CLOSED", None)
    url_f = f"/v1/events/{event_f}/report"
    closure = get(client, url_f, actor="rev-1")
    assert closure.status_code == 200, (
        f"{PART1_MISSING}: GET {url_f} gave {closure.status_code} {closure.text}"
    )
    not_submittable = client.request(
        "POST",
        f"{url_f}/submission",
        headers=bearer("rev-1"),
        json={
            "report_id": closure.headers["X-Report-Id"],
            "json_sha256": closure.headers["X-Report-Sha256"],
            "report_type": "Initial",
        },
    )
    assert not_submittable.status_code == 409, not_submittable.text
    assert not_submittable.json()["error_code"] == "NOT_SUBMITTABLE"

    # The rows the trace wrote are exactly what the ledger replays to, and the chain verifies.
    assert projection(app_engine) == []
    with Session(app_engine) as session:
        assert verify_ledger(session)["valid"] is True
