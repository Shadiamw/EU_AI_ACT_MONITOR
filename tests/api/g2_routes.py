"""Test-only typed-query and session-info routes. Never mounted by app/main.py."""

from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.db import get_session

router = APIRouter()

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
