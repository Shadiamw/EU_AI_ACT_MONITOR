"""Phase 4 Part 2 HTTP contract: POST /v1/events/{event_id}/disposition and the report guard on
GET /v1/events/{event_id}/report (Phase 4 spec 2.4 and 2.8). Both accept REVIEWER only (REQ-13,
AC-14; Addendum rule 6).

Order of checks on the disposition (spec 7, Pinned): 401 and 403 (the `require` dependency),
400 MALFORMED_BODY and 400 RATIONALE_REQUIRED (the body, read only after authentication), then
inside the transaction 404, 409 EVENT_NOT_REVIEWABLE, 409 HARM_FIELDS_UNCONFIRMED and 409
DEADLINE_EXTENSION_REQUIRES_RATIONALE. The first failing check decides the response, and
nothing is written or appended before it. A success commits once and returns the full Event;
the response is not a view and appends no EVENT_VIEWED.

The report is a guard until Phase 5 (D1): 404, 409 NO_DISPOSITION, otherwise 501. It reads only.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from app import dispositions, events, observability
from app.auth import Principal, require
from app.db import get_session
from app.dispositions import DispositionRequest
from app.errors import ApiError, not_found
from app.jsonout import ExactJSONResponse

router = APIRouter()
DbSession = Annotated[Session, Depends(get_session)]
Reviewer = Annotated[Principal, Depends(require("REVIEWER"))]


async def parse_disposition(request: Request, principal: Reviewer) -> DispositionRequest:
    """The body, read only after authentication (steps 2 and 3). Raw JSON, so an absent
    harm_fields reaches the 409 check instead of becoming a 400."""
    try:
        raw = json.loads(await request.body())
    except (ValueError, UnicodeError, RecursionError):
        raise dispositions.malformed() from None
    return dispositions.parse_request(raw)


@router.post("/v1/events/{event_id}/disposition")
def post_disposition(
    request: Request,
    event_id: str,
    principal: Reviewer,
    body: Annotated[DispositionRequest, Depends(parse_disposition)],
    session: DbSession,
) -> ExactJSONResponse:
    observability.note(request, event_id=event_id)
    recorded = dispositions.record(session, principal, event_id, body)
    detail = events.detail(session, event_id)
    if detail is None:  # the row is locked by this transaction, so it cannot have gone
        raise not_found()
    session.commit()
    dispositions.disposition_logger.info(
        "disposition %s: event_id=%s disposition_id=%s seq_in_event=%d tier=%s actor=%s",
        "revised" if recorded.revision else "recorded",
        event_id,
        recorded.disposition_id,
        recorded.seq_in_event,
        recorded.tier,
        principal.actor_id,
    )
    return ExactJSONResponse(detail)


@router.get("/v1/events/{event_id}/report")
def get_report(
    request: Request, event_id: str, principal: Reviewer, session: DbSession
) -> ExactJSONResponse:
    observability.note(request, event_id=event_id)
    dispositions.report_guard(session, event_id)
    # A disposition exists (any tier): the report itself is Phase 5 (D1).
    raise ApiError(501, "NOT_IMPLEMENTED", "not implemented in this phase")
