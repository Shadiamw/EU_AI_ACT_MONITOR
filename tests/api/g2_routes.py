"""Test-only routes (plan G2; docs/decisions.md 34). Never mounted by app/main.py.

The G2 stand-in for GET /v1/events/{event_id} was removed when Part 1's real event-detail
endpoint landed (app/api/ingestion_routes.py); the tests now call the real one.

- GET /_test/typed: a typed query parameter, to show a validation failure is 400, not 422.
- GET /_test/session-info: the role and isolation level of a request's session.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.auth import Principal, require
from app.db import get_session

router = APIRouter()

Reader = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]
DbSession = Annotated[Session, Depends(get_session)]


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
