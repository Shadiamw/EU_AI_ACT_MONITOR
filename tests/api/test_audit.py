"""M4: GET /v1/events/{event_id}/audit (AC-15 view, audit of audit, unknown event).

Expected values are reasoned out in comments next to each assertion.
"""

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text
from starlette.types import Message, Receive, Scope, Send

from app import clock
from tests.api.conftest import (
    NOT_FOUND,
    ApiClient,
    append_committed,
    append_lock_is_free,
    as_actor,
    count_entries,
    deliver_notification,
    flag_event,
    owner_rows,
    seed,
)

AUDIT_KEYS = {"actor_id", "action", "timestamp"}


def audit(client: ApiClient, event_id: str, actor: str = "viewer-1") -> list[dict[str, Any]]:
    response = client.get(f"/v1/events/{event_id}/audit", headers=as_actor(actor))
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def view(client: ApiClient, event_id: str, actor: str) -> None:
    response = client.get(f"/v1/events/{event_id}", headers=as_actor(actor))
    assert response.status_code == 200, response.text
    assert response.json()["event_id"] == event_id


def stored_timestamp(owner_engine: Engine, seq: int) -> str:
    with owner_engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT recorded_at FROM ledger_entry WHERE seq = :seq"), {"seq": seq}
            ).scalar_one()
        )


# A. AC-15 (view)


def test_views_appear_in_the_audit_in_seq_order_after_the_earlier_entries(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    seed(app_engine)  # seq 1: SEED, no subject
    flag_event(app_engine, "evt-a")  # seq 2 EVENT_FLAGGED, seq 3 NOTIFICATION_REQUESTED
    flag_event(app_engine, "evt-b")  # seq 4, 5: another event
    deliver_notification(app_engine, "evt-a")  # seq 6
    view(client, "evt-a", "viewer-1")  # seq 7 EVENT_VIEWED viewer-1
    view(client, "evt-b", "rev-1")  # seq 8: a view of the other event
    view(client, "evt-a", "rev-1")  # seq 9 EVENT_VIEWED rev-1

    trail = audit(client, "evt-a")
    # evt-a's entries are seqs 2, 3, 6, 7 and 9, in that order. SEED (no subject) and evt-b's
    # seqs 4, 5 and 8 are not about evt-a. EVENT_FLAGGED and NOTIFICATION_REQUESTED are
    # written by sys-42, delivery by the worker (system), and the two views by their viewers.
    # Each timestamp is the stored recorded_at of that seq, unchanged.
    expected = [
        ("sys-42", "EVENT_FLAGGED", 2),
        ("sys-42", "NOTIFICATION_REQUESTED", 3),
        ("system", "NOTIFICATION_DELIVERED", 6),
        ("viewer-1", "EVENT_VIEWED", 7),
        ("rev-1", "EVENT_VIEWED", 9),
    ]
    assert trail == [
        {"actor_id": actor, "action": action, "timestamp": stored_timestamp(owner_engine, seq)}
        for actor, action, seq in expected
    ]
    assert all(set(item) == AUDIT_KEYS for item in trail)


def test_view_timestamp_is_the_overridden_clock(
    client: ApiClient, app_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    flag_event(app_engine, "evt-a")
    simulated = datetime(2026, 9, 27, 10, 6, 0, 123456, tzinfo=UTC)
    monkeypatch.setattr(clock, "now", lambda: simulated)
    view(client, "evt-a", "viewer-1")

    trail = audit(client, "evt-a")
    viewed = [item for item in trail if item["action"] == "EVENT_VIEWED"]
    # One view, by viewer-1, stamped with the simulated time truncated to milliseconds.
    assert viewed == [
        {"actor_id": "viewer-1", "action": "EVENT_VIEWED", "timestamp": "2026-09-27T10:06:00.123Z"}
    ]
    parsed = datetime.strptime(viewed[0]["timestamp"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=UTC)
    assert abs((parsed - simulated).total_seconds()) < 1


def test_other_events_entries_never_appear(client: ApiClient, app_engine: Engine) -> None:
    flag_event(app_engine, "evt-a")
    flag_event(app_engine, "evt-b")
    view(client, "evt-b", "viewer-1")
    audit(client, "evt-b", "rev-1")  # AUDIT_VIEWED for evt-b
    trail = audit(client, "evt-a")
    # Only evt-a's two flag entries: none of evt-b's flag, view or audit entries.
    assert [item["action"] for item in trail] == ["EVENT_FLAGGED", "NOTIFICATION_REQUESTED"]
    assert all(set(item) == AUDIT_KEYS for item in trail)


# B. Audit of audit


def test_audit_lists_its_own_view_only_on_later_calls(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    flag_event(app_engine, "evt-a")

    first = audit(client, "evt-a", "viewer-1")
    # The response is built before AUDIT_VIEWED is appended, so it has none; the ledger has one.
    assert [item for item in first if item["action"] == "AUDIT_VIEWED"] == []
    assert count_entries(owner_engine, "AUDIT_VIEWED") == 1

    second = audit(client, "evt-a", "rev-1")
    # Exactly the first call's view, by its caller viewer-1.
    assert [(i["actor_id"], i["action"]) for i in second if i["action"] == "AUDIT_VIEWED"] == [
        ("viewer-1", "AUDIT_VIEWED")
    ]

    third = audit(client, "evt-a", "viewer-1")
    # The first two calls' views, in seq order.
    assert [(i["actor_id"], i["action"]) for i in third if i["action"] == "AUDIT_VIEWED"] == [
        ("viewer-1", "AUDIT_VIEWED"),
        ("rev-1", "AUDIT_VIEWED"),
    ]


class CommitProbe:
    """Wraps the app and, when a response starts, counts committed AUDIT_VIEWED rows.

    The count runs on a separate owner connection, so it sees only committed rows. If the
    handler committed after its response started, the count would still be one short.
    """

    def __init__(self, app: FastAPI, owner_engine: Engine) -> None:
        self.app = app
        self.owner_engine = owner_engine
        self.committed_at_response: list[int] = []

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        async def probing_send(message: Message) -> None:
            if message["type"] == "http.response.start" and scope["path"].endswith("/audit"):
                self.committed_at_response.append(count_entries(self.owner_engine, "AUDIT_VIEWED"))
            await send(message)

        await self.app(scope, receive, probing_send)


# GUARD TEST: do not delete or weaken this test or CommitProbe. It is the only test that
# catches a commit made after the response has started (decision 25), for example a commit
# moved into the session dependency's teardown or into a background task. An in-process client
# returns only after the whole app call has finished, so a late commit still lands before the
# next request and every other test passes. Only this check, which counts committed rows at
# the moment each response starts, sees the late commit (docs/decisions.md 43).
def test_each_audit_view_is_committed_before_the_response(
    api_app: FastAPI, app_engine: Engine, owner_engine: Engine
) -> None:
    flag_event(app_engine, "evt-a")
    probe = CommitProbe(api_app, owner_engine)
    calls = 50
    with ApiClient(probe) as client:
        for n in range(1, calls + 1):
            trail = audit(client, "evt-a", "viewer-1" if n % 2 else "rev-1")
            # Call n sees the n-1 views of the earlier calls, all committed before it read.
            assert sum(item["action"] == "AUDIT_VIEWED" for item in trail) == n - 1
    # When call n's response started, its own view (the n-th) was already committed.
    assert probe.committed_at_response == list(range(1, calls + 1))


# C. Unknown event


def test_unknown_event_is_404_and_writes_nothing(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    seed(app_engine)
    flag_event(app_engine, "evt-a")
    view(client, "evt-a", "viewer-1")
    before = owner_rows(owner_engine)

    for event_id in ("evt-missing", "evt-a-x", ""):
        response = client.get(f"/v1/events/{event_id}/audit", headers=as_actor("viewer-1"))
        # An id that no EVENT_FLAGGED or GAP_OPENED entry names is unknown. The empty id does
        # not match the route at all, which is also a 404 NOT_FOUND.
        assert response.status_code == 404
        assert response.json() == NOT_FOUND

    assert owner_rows(owner_engine) == before
    assert append_lock_is_free(owner_engine)

    # A subject with only a view entry, no EVENT_FLAGGED or GAP_OPENED, is not an event
    # (decision 30), so it is a 404 too and writes nothing.
    append_committed(app_engine, "EVENT_VIEWED", "viewer-1", "evt-viewed-only", {})
    before = owner_rows(owner_engine)
    response = client.get("/v1/events/evt-viewed-only/audit", headers=as_actor("viewer-1"))
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert owner_rows(owner_engine) == before
    assert append_lock_is_free(owner_engine)
