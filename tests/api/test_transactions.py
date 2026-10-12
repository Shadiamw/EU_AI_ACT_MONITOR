"""M4: transactions, concurrency and the application engine.

Expected values are reasoned out in comments next to each assertion.
"""

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import ledger
from app.db import MAX_OVERFLOW, POOL_SIZE, get_engine
from app.ledger import verify_entries
from app.ledger.canonical import LedgerError
from tests.api.conftest import (
    INTERNAL,
    ApiClient,
    append_lock_is_free,
    as_actor,
    count_entries,
    flag_event,
    owner_rows,
    seed,
)

# G1. A failed append inside the audit request


def test_audit_with_a_failing_append_is_500_and_leaves_no_audit_view(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flag_event(app_engine, "evt-a")
    before = owner_rows(owner_engine)

    def failing_append(*args: Any, **kwargs: Any) -> ledger.LedgerEntry:
        raise LedgerError("append failed")

    monkeypatch.setattr(ledger, "append", failing_append)
    response = client.get("/v1/events/evt-a/audit", headers=as_actor("rev-1"))
    assert (response.status_code, response.json()) == (500, INTERNAL)
    assert count_entries(owner_engine, "AUDIT_VIEWED") == 0
    assert owner_rows(owner_engine) == before
    assert append_lock_is_free(owner_engine)


# G2. Concurrent requests


@pytest.mark.parametrize("run", [1, 2, 3])
def test_concurrent_audits_and_views_keep_a_clean_chain(
    api_app: FastAPI, app_engine: Engine, owner_engine: Engine, run: int
) -> None:
    seed(app_engine)  # seq 1
    flag_event(app_engine, "evt-a")  # seq 2, 3
    calls = 20
    # 40 requests at once: within the default thread pool (40) and the engine's pool.
    assert 2 * calls <= POOL_SIZE + MAX_OVERFLOW

    async def fire() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=api_app)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
            requests = [
                client.get("/v1/events/evt-a/audit", headers=as_actor(actor))
                for actor in ["viewer-1", "rev-1"] * (calls // 2)
            ] + [
                client.get("/v1/events/evt-a", headers=as_actor(actor))
                for actor in ["rev-1", "viewer-1"] * (calls // 2)
            ]
            return await asyncio.gather(*requests)

    responses = asyncio.run(fire())
    assert [r.status_code for r in responses] == [200] * (2 * calls)

    # Each request appended exactly one entry: 20 audit views and 20 event views.
    assert count_entries(owner_engine, "AUDIT_VIEWED") == calls
    assert count_entries(owner_engine, "EVENT_VIEWED") == calls
    rows = owner_rows(owner_engine)
    # 3 entries before, then 40 appends with no duplicate or missing seq.
    assert [row["seq"] for row in rows] == list(range(1, 3 + 2 * calls + 1))
    with ApiClient(api_app) as client:
        verified = client.get("/v1/ledger/verify", headers=as_actor("viewer-1")).json()
        exported = client.get("/v1/ledger/export", headers=as_actor("viewer-1")).json()
    expected = {"valid": True, "entries_checked": 3 + 2 * calls, "first_mismatch": None}
    assert verified == expected
    # The exported chain recomputes on its own.
    assert verify_entries(exported) == expected
    # Every audit response was built before its own view, so none lists all 20 views.
    audit_counts = [
        sum(item["action"] == "AUDIT_VIEWED" for item in r.json()) for r in responses[:calls]
    ]
    assert all(0 <= count < calls for count in audit_counts)
    assert append_lock_is_free(owner_engine)


# G3. The application engine


def test_app_engine_is_the_application_role_at_read_committed(client: ApiClient) -> None:
    assert get_engine().url.username == "monitor_app"
    with Session(get_engine()) as session:
        assert session.execute(text("SELECT current_user")).scalar_one() == "monitor_app"
        assert session.execute(text("SHOW transaction_isolation")).scalar_one() == "read committed"
        session.rollback()
    # The same inside a request's session.
    assert client.get("/_test/session-info").json() == {
        "role": "monitor_app",
        "isolation": "read committed",
    }
