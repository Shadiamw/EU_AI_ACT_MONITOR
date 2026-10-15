"""Part 2 endpoints: the audit trail, the export and server-side verify.

All three accept REVIEWER or VIEWER only and run as the application role in one transaction
per request (docs/decisions.md 25 to 33). Responses are built as plain JSON, never through a
response model, so first_mismatch stays a string.
"""

from typing import Annotated

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy.orm import Session

from app import events, ledger
from app.auth import Principal, require
from app.db import get_session
from app.errors import not_found
from app.ledger import read

router = APIRouter()

Reader = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]
DbSession = Annotated[Session, Depends(get_session)]


@router.get("/v1/events/{event_id}/audit")
def get_audit(event_id: str, principal: Reader, session: DbSession) -> JSONResponse:
    if not events.exists(session, event_id):
        raise not_found()
    # Build the response first, so it never contains its own view (decision 31).
    trail = read.audit_trail(session, event_id)
    ledger.append(session, "AUDIT_VIEWED", principal.actor_id, event_id, {})
    session.commit()
    return JSONResponse(trail)


@router.get("/v1/ledger/export")
def get_export(principal: Reader, session: DbSession) -> JSONResponse:
    entries = read.all_entries(session)
    session.commit()
    return JSONResponse(entries)


@router.get("/v1/ledger/verify")
def get_verify(principal: Reader, session: DbSession) -> JSONResponse:
    result = ledger.verify_ledger(session)
    session.commit()
    return JSONResponse(
        {
            "valid": result["valid"],
            "entries_checked": result["entries_checked"],
            "first_mismatch": result["first_mismatch"],
        }
    )
