"""Test-only routes (plan G2; docs/decisions.md 34). Never mounted by app/main.py.

- GET /v1/events/{event_id}: what the spec says Part 1's event-detail endpoint does for the
  ledger: auth, event lookup, then audit.record_view in the same transaction, then commit.
- GET /_test/typed: a typed query parameter, to show a validation failure is 400, not 422.
- GET /_test/session-info: the role and isolation level of a request's session.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import audit, events
from app.auth import Principal, require
from app.db import get_session
from app.errors import not_found

router = APIRouter()

Reader = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]
DbSession = Annotated[Session, Depends(get_session)]


@router.get("/v1/events/{event_id}")
def get_event(event_id: str, principal: Reader, session: DbSession) -> dict[str, str]:
    if not events.exists(session, event_id):
        raise not_found()
    audit.record_view(session, principal, event_id)
    session.commit()
    return {"event_id": event_id}


@router.get("/_test/typed")
def typed(count: int) -> dict[str, int]:
    return {"count": count}


@router.get("/_test/session-info")
def session_info(session: DbSession) -> dict[str, str]:
    row = session.execute(
        text("SELECT current_user AS role, current_setting('transaction_isolation') AS iso")
    ).one()
    session.commit()
    return {"role": row.role, "isolation": row.iso}
