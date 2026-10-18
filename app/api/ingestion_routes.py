"""Part 1 HTTP contract: POST /v1/metrics, GET /v1/events and GET /v1/events/{event_id}.

Check order (spec 4): 401, then 403 (the `require` dependency), then the body or query (400
MALFORMED_BODY), then SYSTEM_MISMATCH. Each handler commits before it returns (decision 25).
"""

import json
import re
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError
from sqlalchemy.orm import Session

from app import audit, events, observability
from app.auth import Principal, require
from app.db import get_session
from app.errors import ApiError, not_found
from app.ingestion import MetricIn, ingest
from app.jsonout import ExactJSONResponse

router = APIRouter()
DbSession = Annotated[Session, Depends(get_session)]
Sender = Annotated[Principal, Depends(require("SYSTEM"))]
Reader = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]
Lister = Annotated[Principal, Depends(require("SYSTEM", "REVIEWER", "VIEWER"))]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
_INTEGER = re.compile(r"-?[0-9]+")


def malformed() -> ApiError:
    return ApiError(400, "MALFORMED_BODY", "the request is malformed")


async def parse_metric(request: Request, principal: Sender) -> MetricIn:
    """The body, read only after authentication. JSON numbers are parsed as Decimal (spec 4,
    Numbers); NaN and Infinity become null, which fails validation."""
    try:
        raw = json.loads(await request.body(), parse_float=Decimal, parse_constant=lambda _: None)
        return MetricIn.model_validate(raw)
    except (ValueError, ValidationError, UnicodeError, RecursionError):
        raise malformed() from None


@router.post("/v1/metrics")
def post_metric(
    request: Request,
    principal: Sender,
    sample: Annotated[MetricIn, Depends(parse_metric)],
    session: DbSession,
) -> ExactJSONResponse:
    result = ingest(session, principal, sample)
    session.commit()
    observability.note(request, outcome=result.outcome)
    if result.flagged_event_id is not None:
        observability.note(request, event_id=result.flagged_event_id)
    return ExactJSONResponse(result.body(), status_code=result.status)


def _count(name: str, raw: str | None, default: int) -> int:
    """limit and offset: a non-negative integer, or 400 (decision 118)."""
    if raw is None:
        return default
    if not _INTEGER.fullmatch(raw):
        raise malformed()
    value = int(raw)
    if value < 0:
        raise malformed()
    return value


@router.get("/v1/events")
def list_events(
    principal: Lister,
    session: DbSession,
    system_id: str | None = None,
    state: Literal["FLAGGED", "UNDER_REVIEW", "DISPOSITIONED", "CLOSED"] | None = None,
    type: Literal["threshold_breach", "monitoring_gap"] | None = None,
    since: datetime | None = None,
    limit: str | None = None,
    offset: str | None = None,
) -> ExactJSONResponse:
    if since is not None and (since.tzinfo is None or since.utcoffset() != timedelta(0)):
        raise malformed()
    body = events.summaries(
        session,
        system_id=system_id,
        state=state,
        event_type=type,
        since=since,
        # Above 200 is capped at 200 (decision 118).
        limit=min(_count("limit", limit, DEFAULT_LIMIT), MAX_LIMIT),
        offset=_count("offset", offset, 0),
    )
    session.commit()
    return ExactJSONResponse(body)


@router.get("/v1/events/{event_id}")
def get_event(
    request: Request, event_id: str, principal: Reader, session: DbSession
) -> ExactJSONResponse:
    observability.note(request, event_id=event_id)
    body = events.detail(session, event_id)
    if body is None:
        raise not_found()
    # REQ-14: the view is logged in the same transaction (spec 1.6).
    audit.record_view(session, principal, event_id)
    session.commit()
    return ExactJSONResponse(body)
