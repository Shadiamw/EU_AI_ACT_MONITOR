"""Phase 3 stubs: POST /v1/events/{event_id}/disposition and GET /v1/events/{event_id}/report.

The contract defines both in full (disposition in Example 3); Phase 4 and 5 build them. In
Phase 3 each authenticates and checks the role first (disposition: REVIEWER only; report:
REVIEWER or VIEWER), then returns 501 NOT_IMPLEMENTED whether or not the event exists. The body
is never read (spec 1.6 Stubs; decision 125).
"""

from typing import Annotated

from fastapi import APIRouter, Depends

from app.auth import Principal, require
from app.errors import ApiError

router = APIRouter()

Reviewer = Annotated[Principal, Depends(require("REVIEWER"))]
Reader = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]


def not_implemented() -> ApiError:
    return ApiError(501, "NOT_IMPLEMENTED", "not implemented in this phase")


@router.post("/v1/events/{event_id}/disposition")
def post_disposition(event_id: str, principal: Reviewer) -> None:
    raise not_implemented()


@router.get("/v1/events/{event_id}/report")
def get_report(event_id: str, principal: Reader) -> None:
    raise not_implemented()
