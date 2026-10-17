"""Part 1 HTTP contract: ingestion and event reads."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import audit, events
from app.auth import Principal, require
from app.db import get_session
from app.errors import ApiError, not_found
from app.formats import api_time
from app.ingestion import MetricIn, ingest

router = APIRouter()
DbSession = Annotated[Session, Depends(get_session)]
Sender = Annotated[Principal, Depends(require("SYSTEM"))]
Reader = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]
Lister = Annotated[Principal, Depends(require("SYSTEM", "REVIEWER", "VIEWER"))]


def malformed() -> ApiError:
    return ApiError(400, "MALFORMED_BODY", "the request is malformed")


async def parse_metric(request: Request) -> MetricIn:
    # Decode numeric tokens directly to Decimal: FastAPI's ordinary JSON parser uses floats.
    try:
        raw = json.loads(
            await request.body(), parse_float=Decimal, parse_constant=lambda value: None
        )
        return MetricIn.model_validate(raw)
    except (ValueError, ValidationError, UnicodeError, RecursionError):
        raise malformed() from None


@router.post("/v1/metrics")
def post_metric(
    principal: Sender,
    sample: Annotated[MetricIn, Depends(parse_metric)],
    session: DbSession,
) -> JSONResponse:
    body = ingest(session, principal, sample)
    session.commit()
    return JSONResponse(body, status_code=201)


def event_body(row: Any) -> dict[str, Any]:
    # Earlier worker rows contain null harm entries; normalize them at the API boundary.
    harm = {
        name: row.harm_fields.get(name)
        or {
            "status": "PREFILLED_PENDING_CONFIRMATION",
            "evidence": None,
            "confirmed_by": None,
            "confirmed_at": None,
        }
        for name in events.HARM_FIELDS
    }
    return {
        "event_id": row.event_id,
        "system_id": row.system_id,
        "type": row.type,
        "metric_name": row.metric_name,
        "observed_value": None if row.observed_value is None else float(row.observed_value),
        "threshold_value": None if row.threshold_value is None else float(row.threshold_value),
        "detected_at": api_time(row.detected_at),
        "state": row.state,
        "disposition": row.disposition,
        "deadline_utc": None if row.deadline_utc is None else api_time(row.deadline_utc),
        "revisions": row.revisions,
        "harm_fields": harm,
    }


@router.get("/v1/events")
def list_events(
    principal: Lister,
    session: DbSession,
    response: Response,
    system_id: str | None = None,
    state: Literal["FLAGGED", "UNDER_REVIEW", "DISPOSITIONED", "CLOSED"] | None = None,
    type: Literal["threshold_breach", "monitoring_gap"] | None = None,
    since: datetime | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    if since is not None and (since.tzinfo is None or since.utcoffset() != timedelta(0)):
        raise malformed()
    if principal.role == "SYSTEM":
        if system_id is not None and system_id != principal.system_id:
            raise ApiError(403, "SYSTEM_MISMATCH", "the key does not match system_id")
        system_id = principal.system_id
    filters = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    for column, value in (("system_id", system_id), ("state", state), ("type", type)):
        if value is not None:
            filters.append(f"{column} = :{column}")
            params[column] = value
    if since is not None:
        filters.append("detected_at >= :since")
        params["since"] = since
    where = " WHERE " + " AND ".join(filters) if filters else ""
    # One query gives a consistent page and count under READ COMMITTED.
    rows = session.execute(
        text(
            "SELECT *, count(*) OVER () AS total FROM event"
            + where
            + " ORDER BY detected_at, event_id LIMIT :limit OFFSET :offset"
        ),
        params,
    ).all()
    total = (
        rows[0].total
        if rows
        else session.execute(
            text("SELECT count(*) FROM event" + where),
            params,
        ).scalar_one()
    )
    body = [event_body(row) for row in rows]
    response.headers.update(
        {"X-Total-Count": str(total), "X-Limit": str(limit), "X-Offset": str(offset)}
    )
    session.commit()
    return body


@router.get("/v1/events/{event_id}")
def get_event(event_id: str, principal: Reader, session: DbSession) -> dict[str, Any]:
    row = session.execute(
        text("SELECT * FROM event WHERE event_id = :id"), {"id": event_id}
    ).one_or_none()
    if row is None:
        raise not_found()
    body = event_body(row)
    audit.record_view(session, principal, event_id)
    session.commit()
    return body
