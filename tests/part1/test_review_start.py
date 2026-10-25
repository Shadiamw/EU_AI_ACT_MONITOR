"""Phase 4 Part 1: the review start on the event detail (Phase 4 spec 1.4, decision D3; Addendum
rule 4).

A reviewer's first GET /v1/events/{id} of a FLAGGED breach event moves it to UNDER_REVIEW. The
route writes, in order: EVENT_VIEWED (payload {}), then REVIEW_STARTED (the event as subject,
the reviewer as actor) only when the state changed, then commits before it returns. Nothing
else ever changes state: viewers, system keys, gap events, second views and events already past
FLAGGED. Every expected value here is worked out from spec 1.4 and the test's own setup.
"""

import asyncio
import logging
from collections.abc import Iterator
from typing import Any

import httpx
import psycopg
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session
from starlette.types import Message, Receive, Scope, Send

from app import audit, gaps, ledger
from app.ledger.verify import verify_ledger
from app.main import create_app
from tests.api.conftest import FORBIDDEN, INTERNAL, NOT_FOUND, ApiClient, append_lock_is_free
from tests.db_helpers import HARM_FIELDS, bearer
from tests.part1.conftest import get, post, rows
from tests.worker.conftest import FakeClock

STARTED_LINE = "review started: event_id={event_id} actor=rev-1"


def flag(client: ApiClient) -> str:
    """Contract Example 1: a FLAGGED breach event on sys-42."""
    response = post(client)
    assert response.status_code == 201
    event_id: str = response.json()["flagged_event_id"]
    return event_id


def ledger_rows(owner_engine: Engine) -> list[dict[str, Any]]:
    return rows(
        owner_engine,
        "SELECT seq, entry_type, actor_id, subject_id, payload FROM ledger_entry ORDER BY seq",
    )


def event_row(owner_engine: Engine, event_id: str) -> dict[str, Any]:
    [row] = rows(owner_engine, "SELECT * FROM event WHERE event_id = :e", e=event_id)
    return row


def state(owner_engine: Engine, event_id: str) -> str:
    return str(event_row(owner_engine, event_id)["state"])


def set_state(owner_engine: Engine, event_id: str, new_state: str) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text("UPDATE event SET state = :state WHERE event_id = :e"),
            {"state": new_state, "e": event_id},
        )


def viewed(seq: int, actor: str, event_id: str) -> dict[str, Any]:
    return {
        "seq": seq,
        "entry_type": "EVENT_VIEWED",
        "actor_id": actor,
        "subject_id": event_id,
        "payload": {},
    }


def review_started(seq: int, event_id: str) -> dict[str, Any]:
    return {
        "seq": seq,
        "entry_type": "REVIEW_STARTED",
        "actor_id": "rev-1",
        "subject_id": event_id,
        "payload": {"event_id": event_id, "prior_state": "FLAGGED", "new_state": "UNDER_REVIEW"},
    }


def started_lines(caplog: pytest.LogCaptureFixture) -> list[tuple[int, str]]:
    return [
        (record.levelno, record.getMessage())
        for record in caplog.records
        if record.name == "api.review" and record.getMessage().startswith("review started")
    ]


def event_row_lock_is_free(owner_engine: Engine, event_id: str) -> bool:
    with owner_engine.connect() as conn:
        try:
            conn.execute(
                text("SELECT 1 FROM event WHERE event_id = :e FOR UPDATE NOWAIT"), {"e": event_id}
            )
        except OperationalError as error:
            assert isinstance(error.orig, psycopg.errors.LockNotAvailable)
            return False
        finally:
            conn.rollback()
    return True


@pytest.fixture
def logs(caplog: pytest.LogCaptureFixture) -> Iterator[pytest.LogCaptureFixture]:
    caplog.set_level(logging.DEBUG, logger="api")
    yield caplog


# ---------------------------------------------------------------------------------------------
# The reviewer's first view
# ---------------------------------------------------------------------------------------------


def test_a_reviewer_view_moves_flagged_to_under_review(
    client: ApiClient, owner_engine: Engine, logs: pytest.LogCaptureFixture
) -> None:
    event_id = flag(client)
    before = event_row(owner_engine, event_id)
    # SEED is seq 1, EVENT_FLAGGED and NOTIFICATION_REQUESTED are seqs 2 and 3.
    assert len(ledger_rows(owner_engine)) == 3

    response = get(client, f"/v1/events/{event_id}", actor="rev-1")

    assert response.status_code == 200
    assert response.json()["state"] == "UNDER_REVIEW"
    after = event_row(owner_engine, event_id)
    assert after == {**before, "state": "UNDER_REVIEW"}
    assert after["detected_at"] == before["detected_at"]
    # Spec 1.4 steps 3 and 4: the view first, then the review start.
    assert ledger_rows(owner_engine)[3:] == [
        viewed(4, "rev-1", event_id),
        review_started(5, event_id),
    ]
    assert started_lines(logs) == [(logging.INFO, STARTED_LINE.format(event_id=event_id))]
    # The body is the detail as it now stands: a viewer sees the same event.
    assert get(client, f"/v1/events/{event_id}").json() == response.json()


def test_the_audit_trail_shows_the_review_start(client: ApiClient) -> None:
    event_id = flag(client)
    get(client, f"/v1/events/{event_id}", actor="rev-1")
    trail = get(client, f"/v1/events/{event_id}/audit").json()
    assert [(item["actor_id"], item["action"]) for item in trail] == [
        ("sys-42", "EVENT_FLAGGED"),
        # The worker's actor appends the notification request (app/notifications.py).
        ("system", "NOTIFICATION_REQUESTED"),
        ("rev-1", "EVENT_VIEWED"),
        ("rev-1", "REVIEW_STARTED"),
    ]


# ---------------------------------------------------------------------------------------------
# Views that never change state
# ---------------------------------------------------------------------------------------------


def test_a_viewer_view_changes_nothing(
    client: ApiClient, owner_engine: Engine, logs: pytest.LogCaptureFixture
) -> None:
    event_id = flag(client)
    before = event_row(owner_engine, event_id)
    for _ in range(3):
        response = get(client, f"/v1/events/{event_id}", actor="viewer-1")
        assert response.status_code == 200
        assert response.json()["state"] == "FLAGGED"
    assert event_row(owner_engine, event_id) == before
    assert ledger_rows(owner_engine)[3:] == [viewed(seq, "viewer-1", event_id) for seq in (4, 5, 6)]
    assert started_lines(logs) == []


def test_a_system_key_gets_403_and_writes_nothing(client: ApiClient, owner_engine: Engine) -> None:
    event_id = flag(client)
    before = (event_row(owner_engine, event_id), ledger_rows(owner_engine))
    response = get(client, f"/v1/events/{event_id}", actor="sys-42")
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert (event_row(owner_engine, event_id), ledger_rows(owner_engine)) == before


def test_a_second_reviewer_view_appends_event_viewed_only(
    client: ApiClient, owner_engine: Engine, logs: pytest.LogCaptureFixture
) -> None:
    event_id = flag(client)
    get(client, f"/v1/events/{event_id}", actor="rev-1")
    started = event_row(owner_engine, event_id)
    response = get(client, f"/v1/events/{event_id}", actor="rev-1")
    assert response.status_code == 200
    assert response.json()["state"] == "UNDER_REVIEW"
    assert event_row(owner_engine, event_id) == started
    assert ledger_rows(owner_engine)[3:] == [
        viewed(4, "rev-1", event_id),
        review_started(5, event_id),
        viewed(6, "rev-1", event_id),
    ]
    assert len(started_lines(logs)) == 1


@pytest.mark.parametrize("gap_state", ["FLAGGED", "CLOSED"])
def test_a_gap_event_never_enters_under_review(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    fake_clock: FakeClock,
    logs: pytest.LogCaptureFixture,
    gap_state: str,
) -> None:
    # Three missed intervals of 300 s open a gap (INV-12: a gap is never reviewed).
    fake_clock.advance(900)
    [gap] = gaps.sweep(app_engine)
    if gap_state == "CLOSED":
        # A sample on time closes the open gap (ingest step 6).
        assert post(client, timestamp="2026-09-27T10:20:00Z").status_code == 201
    assert state(owner_engine, gap) == gap_state
    before = (event_row(owner_engine, gap), ledger_rows(owner_engine))

    response = get(client, f"/v1/events/{gap}", actor="rev-1")

    assert response.status_code == 200
    assert response.json()["state"] == gap_state
    assert response.json()["harm_fields"] == {
        key: {
            "status": "NOT_APPLICABLE",
            "evidence": None,
            "confirmed_by": None,
            "confirmed_at": None,
        }
        for key in HARM_FIELDS
    }
    assert event_row(owner_engine, gap) == before[0]
    seq = len(before[1]) + 1
    assert ledger_rows(owner_engine) == [*before[1], viewed(seq, "rev-1", gap)]
    assert started_lines(logs) == []


@pytest.mark.parametrize("later_state", ["UNDER_REVIEW", "DISPOSITIONED", "CLOSED"])
def test_a_breach_event_past_flagged_is_not_moved(
    client: ApiClient, owner_engine: Engine, logs: pytest.LogCaptureFixture, later_state: str
) -> None:
    event_id = flag(client)
    set_state(owner_engine, event_id, later_state)
    before = event_row(owner_engine, event_id)
    response = get(client, f"/v1/events/{event_id}", actor="rev-1")
    assert response.status_code == 200
    assert response.json()["state"] == later_state
    assert event_row(owner_engine, event_id) == before
    assert ledger_rows(owner_engine)[3:] == [viewed(4, "rev-1", event_id)]
    assert started_lines(logs) == []


def test_an_unknown_event_is_404_and_writes_nothing(
    client: ApiClient, owner_engine: Engine, logs: pytest.LogCaptureFixture
) -> None:
    event_id = flag(client)
    before = (event_row(owner_engine, event_id), ledger_rows(owner_engine))
    for actor in ("rev-1", "viewer-1"):
        response = get(client, "/v1/events/evt-missing", actor=actor)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert (event_row(owner_engine, event_id), ledger_rows(owner_engine)) == before
    assert append_lock_is_free(owner_engine)
    assert started_lines(logs) == []


# ---------------------------------------------------------------------------------------------
# Failures roll the whole view back
# ---------------------------------------------------------------------------------------------


def fail_record_view(monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("record_view failed")

    monkeypatch.setattr(audit, "record_view", failing)


def fail_review_started(monkeypatch: pytest.MonkeyPatch) -> None:
    """EVENT_VIEWED is appended for real inside the transaction; REVIEW_STARTED then fails."""
    real_append = ledger.append

    def append(session: Session, entry_type: str, *args: Any, **kwargs: Any) -> Any:
        if entry_type == "REVIEW_STARTED":
            raise RuntimeError("REVIEW_STARTED append failed")
        return real_append(session, entry_type, *args, **kwargs)

    monkeypatch.setattr(ledger, "append", append)


def fail_commit(monkeypatch: pytest.MonkeyPatch) -> None:
    """Both appends succeed and the commit itself fails."""

    def commit(self: Session) -> None:
        raise RuntimeError("commit failed")

    monkeypatch.setattr(Session, "commit", commit)


@pytest.mark.parametrize("failure", [fail_record_view, fail_review_started, fail_commit])
def test_a_failed_view_rolls_back_the_review_start(
    client: ApiClient,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    logs: pytest.LogCaptureFixture,
    failure: Any,
) -> None:
    event_id = flag(client)
    before = (event_row(owner_engine, event_id), ledger_rows(owner_engine))
    with monkeypatch.context() as patch:
        failure(patch)
        response = get(client, f"/v1/events/{event_id}", actor="rev-1")

    assert (response.status_code, response.json()) == (500, INTERNAL)
    assert state(owner_engine, event_id) == "FLAGGED"
    assert (event_row(owner_engine, event_id), ledger_rows(owner_engine)) == before
    assert append_lock_is_free(owner_engine)
    assert event_row_lock_is_free(owner_engine, event_id)
    assert started_lines(logs) == []

    # Once the failure is gone, the next reviewer view starts the review as usual.
    assert get(client, f"/v1/events/{event_id}", actor="rev-1").json()["state"] == "UNDER_REVIEW"
    assert ledger_rows(owner_engine)[3:] == [
        viewed(4, "rev-1", event_id),
        review_started(5, event_id),
    ]
    assert started_lines(logs) == [(logging.INFO, STARTED_LINE.format(event_id=event_id))]


# ---------------------------------------------------------------------------------------------
# Concurrency and the commit point
# ---------------------------------------------------------------------------------------------


def test_twenty_parallel_reviewer_views_start_the_review_once(
    client: ApiClient, owner_engine: Engine, logs: pytest.LogCaptureFixture
) -> None:
    event_id = flag(client)
    calls = 20

    async def fire() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as parallel:
            return await asyncio.gather(
                *(
                    parallel.get(f"/v1/events/{event_id}", headers=bearer("rev-1"))
                    for _ in range(calls)
                )
            )

    responses = asyncio.run(fire())

    assert [r.status_code for r in responses] == [200] * calls
    # Every request's UPDATE ran after the winner's or waited for its commit, so every body
    # shows the new state.
    assert [r.json()["state"] for r in responses] == ["UNDER_REVIEW"] * calls
    entries = ledger_rows(owner_engine)
    new = entries[3:]
    assert [e["seq"] for e in entries] == list(range(1, 3 + calls + 1 + 1))
    assert sum(e["entry_type"] == "EVENT_VIEWED" for e in new) == calls
    [started] = [e for e in new if e["entry_type"] == "REVIEW_STARTED"]
    assert started == review_started(started["seq"], event_id)
    # The winner holds the append lock from its EVENT_VIEWED to its commit, so its
    # REVIEW_STARTED comes straight after its own view.
    assert entries[started["seq"] - 2] == viewed(started["seq"] - 1, "rev-1", event_id)
    verified = get(client, "/v1/ledger/verify").json()
    assert verified == {"valid": True, "entries_checked": 3 + calls + 1, "first_mismatch": None}
    with Session(owner_engine) as session:
        assert verify_ledger(session) == verified
    assert state(owner_engine, event_id) == "UNDER_REVIEW"
    assert started_lines(logs) == [(logging.INFO, STARTED_LINE.format(event_id=event_id))]
    assert append_lock_is_free(owner_engine)


class CommitProbe:
    """At the moment the event-detail response starts, records what is already committed: the
    event's state and its EVENT_VIEWED and REVIEW_STARTED counts. A handler that committed only
    after the response started (a background task, the dependency's teardown) shows the old
    values (decision 25)."""

    def __init__(self, app: FastAPI, owner_engine: Engine, event_id: str) -> None:
        self.app = app
        self.owner_engine = owner_engine
        self.event_id = event_id
        self.seen: list[tuple[str, int, int]] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def probing_send(message: Message) -> None:
            if message["type"] == "http.response.start" and scope["path"] == (
                f"/v1/events/{self.event_id}"
            ):
                self.seen.append(self.committed())
            await send(message)

        await self.app(scope, receive, probing_send)

    def committed(self) -> tuple[str, int, int]:
        with self.owner_engine.connect() as conn:
            current = conn.execute(
                text("SELECT state FROM event WHERE event_id = :e"), {"e": self.event_id}
            ).scalar_one()
            counts = conn.execute(
                text(
                    "SELECT count(*) FILTER (WHERE entry_type = 'EVENT_VIEWED'),"
                    " count(*) FILTER (WHERE entry_type = 'REVIEW_STARTED')"
                    " FROM ledger_entry WHERE subject_id = :e"
                ),
                {"e": self.event_id},
            ).one()
        return (str(current), int(counts[0]), int(counts[1]))


def test_the_review_start_is_committed_before_the_response(
    client: ApiClient, owner_engine: Engine
) -> None:
    event_id = flag(client)
    probe = CommitProbe(create_app(), owner_engine, event_id)
    with ApiClient(probe) as probed:
        for _ in range(3):
            response = probed.get(f"/v1/events/{event_id}", headers=bearer("rev-1"))
            assert response.status_code == 200
    # Each response started after its own view, and the first one's review start, committed.
    assert probe.seen == [("UNDER_REVIEW", 1, 1), ("UNDER_REVIEW", 2, 1), ("UNDER_REVIEW", 3, 1)]
