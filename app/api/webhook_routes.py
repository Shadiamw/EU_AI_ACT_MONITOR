"""POST /v1/webhooks (API contract): register a receiver for flag_notification and reminder.

REVIEWER or VIEWER only (401, then 403, then the body: spec 4). 201 `{webhook_id}` after a 2xx
verification ping; 422 WEBHOOK_UNVERIFIED when the ping fails; 400 MALFORMED_BODY for a body
that does not fit or a URL the system will not call (spec 3.6; app/webhooks.py), with no ping.
The handler commits before it returns (decision 25).
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.orm import Session

from app import webhooks
from app.auth import Principal, require
from app.db import get_session
from app.errors import ApiError

router = APIRouter()

Registrant = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]
DbSession = Annotated[Session, Depends(get_session)]


class WebhookIn(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    url: str
    event_types: Annotated[list[Literal["flag_notification", "reminder"]], Field(min_length=1)]


def malformed() -> ApiError:
    return ApiError(400, "MALFORMED_BODY", "the request is malformed")


async def parse_webhook(request: Request, principal: Registrant) -> WebhookIn:
    """The body, read only after the key and the role are checked (spec 4, order of checks),
    so an unparseable body cannot answer 400 before 401 or 403."""
    try:
        return WebhookIn.model_validate_json(await request.body())
    except (ValidationError, UnicodeError):
        raise malformed() from None


@router.post("/v1/webhooks")
def register_webhook(
    principal: Registrant,
    body: Annotated[WebhookIn, Depends(parse_webhook)],
    session: DbSession,
) -> JSONResponse:
    try:
        webhook_id = webhooks.register(
            session, principal.actor_id, body.url, list(body.event_types)
        )
    except webhooks.WebhookUrlError:
        raise malformed() from None
    except webhooks.VerificationFailed:
        raise ApiError(
            422, "WEBHOOK_UNVERIFIED", "the webhook did not answer the verification ping with 2xx"
        ) from None
    session.commit()
    return JSONResponse({"webhook_id": webhook_id}, status_code=201)
