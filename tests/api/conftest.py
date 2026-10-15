"""Fixtures for the M4 endpoint tests.

The app under test is app.main.create_app() plus the test-only routes. The principal resolver
is overridden so each request picks its actor with an X-Test-Actor header (no header means no
principal, so 401). Tests that need the stub's default resolver build their own app.
"""

from collections.abc import Callable, Iterator
from typing import Annotated, Any

import anyio.from_thread
import httpx
import pytest
from fastapi import FastAPI, Header
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session
from starlette.types import ASGIApp

from app import ledger
from app.auth import Principal, resolve_principal
from app.db import get_engine
from app.ledger.canonical import Payload
from app.main import create_app
from tests.api import g2_routes
from tests.db_helpers import TRIGGER_NAME, trigger_disabled

PRINCIPALS = {
    "viewer-1": Principal(role="VIEWER", actor_id="viewer-1", system_id=None),
    "rev-1": Principal(role="REVIEWER", actor_id="rev-1", system_id=None),
    "sys-42": Principal(role="SYSTEM", actor_id="sys-42", system_id="sys-42"),
}

NOT_FOUND = {"error_code": "NOT_FOUND", "message": "resource not found"}
FORBIDDEN = {"error_code": "FORBIDDEN_ROLE", "message": "this role may not call this endpoint"}
UNAUTHORIZED = {"error_code": "MISSING_OR_INVALID_KEY", "message": "missing or invalid API key"}
MALFORMED = {"error_code": "MALFORMED_BODY", "message": "the request is malformed"}
INTERNAL = {"error_code": "INTERNAL_ERROR", "message": "an internal error occurred"}

SEED_PAYLOAD: Payload = {"systems": ["sys-42"], "threshold_count": "2"}


def header_principal(
    x_test_actor: Annotated[str | None, Header()] = None,
) -> Principal | None:
    return None if x_test_actor is None else PRINCIPALS[x_test_actor]


def as_actor(actor: str) -> dict[str, str]:
    return {"X-Test-Actor": actor}


def build_app(*, override_principal: bool = True) -> FastAPI:
    app = create_app()
    app.include_router(g2_routes.router)
    if override_principal:
        app.dependency_overrides[resolve_principal] = header_principal
    return app


@pytest.fixture(scope="session", autouse=True)
def dispose_app_engine() -> Iterator[None]:
    yield
    get_engine().dispose()


@pytest.fixture
def api_app() -> FastAPI:
    return build_app()


class ApiClient:
    """A synchronous client that runs the ASGI app in a background event loop.

    Requests go through httpx's ASGI transport; sync handlers run in the loop's thread pool,
    as under a real server.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._portal_cm = anyio.from_thread.start_blocking_portal()
        self._portal = self._portal_cm.__enter__()
        self._client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://testserver"
        )

    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            return await self._client.request(method, url, **kwargs)

        return self._portal.call(send)

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def close(self) -> None:
        self._portal.call(self._client.aclose)
        self._portal_cm.__exit__(None, None, None)

    def __enter__(self) -> "ApiClient":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@pytest.fixture
def client(api_app: FastAPI) -> Iterator[ApiClient]:
    with ApiClient(api_app) as api_client:
        yield api_client


def append_committed(
    app_engine: Engine, entry_type: str, actor_id: str, subject_id: str | None, payload: Any
) -> ledger.LedgerEntry:
    with Session(app_engine) as session:
        entry = ledger.append(session, entry_type, actor_id, subject_id, payload)
        session.commit()
    return entry


def seed(app_engine: Engine) -> None:
    with Session(app_engine) as session:
        ledger.ensure_seed(session, SEED_PAYLOAD)
        session.commit()


def flag_event(app_engine: Engine, event_id: str) -> ledger.LedgerEntry:
    """EVENT_FLAGGED and NOTIFICATION_REQUESTED in one transaction, as Part 1's ingest does."""
    with Session(app_engine) as session:
        flagged = ledger.append(
            session,
            "EVENT_FLAGGED",
            "sys-42",
            event_id,
            {
                "event_id": event_id,
                "system_id": "sys-42",
                "metric_name": "accuracy_drift",
                "observed_value": "0.34",
                "threshold_value": "0.30",
                "threshold_version_id": "thr-1",
            },
        )
        ledger.append(
            session,
            "NOTIFICATION_REQUESTED",
            "sys-42",
            event_id,
            {"notification_id": f"ntf-{event_id}", "event_id": event_id},
        )
        session.commit()
    return flagged


def deliver_notification(app_engine: Engine, event_id: str) -> ledger.LedgerEntry:
    """NOTIFICATION_DELIVERED, written by the background worker (actor system)."""
    return append_committed(
        app_engine,
        "NOTIFICATION_DELIVERED",
        "system",
        event_id,
        {"notification_id": f"ntf-{event_id}", "webhook_id": "wh-1", "latency_seconds": 2},
    )


def owner_rows(owner_engine: Engine) -> list[dict[str, Any]]:
    with owner_engine.connect() as conn:
        return [
            dict(row._mapping)
            for row in conn.execute(text("SELECT * FROM ledger_entry ORDER BY seq"))
        ]


def count_entries(owner_engine: Engine, entry_type: str) -> int:
    with owner_engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM ledger_entry WHERE entry_type = :t"), {"t": entry_type}
            ).scalar_one()
        )


def append_lock_is_free(owner_engine: Engine) -> bool:
    """True if nobody holds the append lock: a try-lock in a throwaway transaction succeeds."""
    with owner_engine.begin() as conn:
        return bool(
            conn.execute(
                text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": ledger.ADVISORY_LOCK_KEY}
            ).scalar_one()
        )


def trigger_enabled(owner_engine: Engine) -> bool:
    with owner_engine.connect() as conn:
        state = conn.execute(
            text("SELECT tgenabled FROM pg_trigger WHERE tgname = :name"), {"name": TRIGGER_NAME}
        ).scalar_one()
    return bool(state == "O")


@pytest.fixture
def owner_tamper(owner_engine: Engine) -> Iterator[Callable[..., None]]:
    """Run SQL as the owner with the trigger disabled (addendum rule 1).

    The disable, the edit and the enable share one transaction; the finalizer re-enables the
    trigger anyway and checks it is enabled, even if the test failed.
    """

    def tamper(*statements: str) -> None:
        with trigger_disabled(owner_engine) as conn:
            for statement in statements:
                conn.execute(text(statement))

    yield tamper
    with owner_engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE ledger_entry ENABLE TRIGGER {TRIGGER_NAME}"))
    assert trigger_enabled(owner_engine)
