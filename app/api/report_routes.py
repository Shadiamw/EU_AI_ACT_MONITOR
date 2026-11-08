"""Phase 5 Part 1 HTTP: the report, its submission and its listing (spec 3.6, 3.7, 3.9).

- GET /v1/events/{event_id}/report (REVIEWER only): the stored report, JSON or PDF. The first
  request after a disposition assembles it (D3); every later one serves the stored bytes
  byte for byte and writes nothing (D19). Order of checks: 401, 403, 400 MALFORMED_QUERY,
  404, 410, 409 NO_DISPOSITION, then 200.
- POST /v1/events/{event_id}/report/submission (REVIEWER only): records a manual submission.
- GET /v1/events/{event_id}/reports (REVIEWER or VIEWER): the report rows, without content.
- GET /v1/reports/{report_id} (REVIEWER only): the stored bytes of one version.

The first failing check decides the response, and nothing is written before it. A response
that carries report bytes adds ETag and X-Report-Id, X-Report-Version and X-Report-Sha256
(spec 3.4 item 4); the body is the stored bytes as they are, never a new rendering.
"""

import json
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy.orm import Session

from app import dispositions, observability, reports
from app.auth import Principal, require
from app.db import get_session
from app.errors import ApiError, not_found
from app.jsonout import ExactJSONResponse

router = APIRouter()
DbSession = Annotated[Session, Depends(get_session)]
Reviewer = Annotated[Principal, Depends(require("REVIEWER"))]
Reader = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]

report_logger = logging.getLogger("api.report")

JSON_TYPE = "application/json"
PDF_TYPE = "application/pdf"


def _format(request: Request) -> str:
    """The `format` query parameter: absent or json for JSON, pdf for the PDF; anything else
    (an empty value, a repeated parameter, another word) is 400 MALFORMED_QUERY."""
    values = request.query_params.getlist("format")
    if not values:
        return "json"
    if len(values) == 1 and values[0] in ("json", "pdf"):
        return values[0]
    raise ApiError(400, "MALFORMED_QUERY", "format must be json or pdf")


def _bytes_response(stored: reports.StoredReport, fmt: str) -> Response:
    body, digest, media = (
        (stored.json_bytes, stored.json_sha256, JSON_TYPE)
        if fmt == "json"
        else (stored.pdf_bytes, stored.pdf_sha256, PDF_TYPE)
    )
    return Response(
        content=body,
        media_type=media,
        headers={
            "ETag": f'"{digest}"',
            "X-Report-Id": stored.report_id,
            "X-Report-Version": str(stored.report_version),
            "X-Report-Sha256": digest,
        },
    )


@router.get("/v1/events/{event_id}/report")
def get_report(
    request: Request, event_id: str, principal: Reviewer, session: DbSession
) -> Response:
    fmt = _format(request)
    observability.note(request, event_id=event_id)
    stored = reports.assemble_or_get(session, principal, event_id)
    if stored.assembled:
        session.commit()
        report_logger.info(
            "report assembled: event_id=%s report_id=%s report_version=%d report_kind=%s actor=%s",
            event_id,
            stored.report_id,
            stored.report_version,
            stored.report_kind,
            principal.actor_id,
        )
    return _bytes_response(stored, fmt)


async def parse_submission(request: Request, principal: Reviewer) -> reports.SubmissionRequest:
    """The body, read only after authentication (steps 1 to 3 of spec 3.7)."""
    try:
        raw = json.loads(await request.body())
    except (ValueError, UnicodeError, RecursionError):
        raise dispositions.malformed() from None
    return reports.parse_submission(raw)


@router.post("/v1/events/{event_id}/report/submission", status_code=201)
def post_submission(
    request: Request,
    event_id: str,
    principal: Reviewer,
    body: Annotated[reports.SubmissionRequest, Depends(parse_submission)],
    session: DbSession,
) -> ExactJSONResponse:
    observability.note(request, event_id=event_id)
    recorded = reports.submit(session, principal, event_id, body)
    session.commit()
    report_logger.info(
        "report submitted: event_id=%s report_id=%s submission_id=%s submission_kind=%s actor=%s",
        event_id,
        recorded["report_id"],
        recorded["submission_id"],
        recorded["submission_kind"],
        principal.actor_id,
    )
    return ExactJSONResponse(recorded, status_code=201)


@router.get("/v1/events/{event_id}/reports")
def get_reports(
    request: Request, event_id: str, principal: Reader, session: DbSession
) -> ExactJSONResponse:
    observability.note(request, event_id=event_id)
    return ExactJSONResponse(reports.listing(session, event_id))


@router.get("/v1/reports/{report_id}")
def get_report_by_id(
    request: Request, report_id: str, principal: Reviewer, session: DbSession
) -> Response:
    fmt = _format(request)
    stored = reports.get_by_id(session, report_id)
    if stored is None:
        raise not_found()
    observability.note(request, event_id=stored.event_id)
    return _bytes_response(stored, fmt)
