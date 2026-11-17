"""Phase 5 Part 2 HTTP: POST /v1/events/{event_id}/erasure (spec 4.4). REVIEWER only.

Order of checks: 401 and 403 (the `require` dependency), 400 MALFORMED_BODY (the body must be
absent, empty or `{}`; read only after authentication), then inside the transaction 404
NOT_FOUND, 409 RETENTION_NOT_EXPIRED, 409 ALREADY_ERASED, and 200. The first failing check
decides the response and nothing is written before it. The log line names the event and the
actor, never a field value.
"""

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import dispositions, observability, retention
from app.auth import Principal, require
from app.db import get_session
from app.jsonout import ExactJSONResponse

router = APIRouter()
DbSession = Annotated[Session, Depends(get_session)]
Reviewer = Annotated[Principal, Depends(require("REVIEWER"))]

logger = logging.getLogger("api.erasure")


async def empty_body(request: Request, principal: Reviewer) -> None:
    """An empty body or `{}`; anything else is 400 MALFORMED_BODY (spec 4.4 step 3)."""
    raw = await request.body()
    if not raw.strip():
        return
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError, RecursionError):
        raise dispositions.malformed() from None
    if value != {}:
        raise dispositions.malformed()


@router.post("/v1/events/{event_id}/erasure")
def post_erasure(
    request: Request,
    event_id: str,
    principal: Reviewer,
    _body: Annotated[None, Depends(empty_body)],
    session: DbSession,
) -> ExactJSONResponse:
    observability.note(request, event_id=event_id)
    erased = retention.erase(session, principal, event_id)
    session.commit()
    logger.info("record erased: event_id=%s actor=%s", event_id, principal.actor_id)
    return ExactJSONResponse(erased)
