"""POST /v1/test/reset and POST /v1/test/clock/advance (spec 4.6, decision 133).

Both exist only when ENVIRONMENT=test. Outside the test environment they return 404, so a
production deploy does not expose them.
"""

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, platform
from app.errors import ApiError, not_found

router = APIRouter()


def _require_test() -> None:
    if not config.is_test_environment():
        raise not_found()


@router.post("/v1/test/reset")
def test_reset() -> JSONResponse:
    _require_test()
    platform.reset()
    return JSONResponse({"ok": True})


@router.post("/v1/test/clock/advance")
async def test_clock_advance(request: Request) -> JSONResponse:
    _require_test()
    try:
        body: Any = await request.json()
    except Exception:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed") from None
    if not isinstance(body, dict) or set(body) != {"seconds"}:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed")
    seconds = body["seconds"]
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 1:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed")
    try:
        result = platform.advance_clock(seconds)
    except ValueError:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed") from None
    return JSONResponse(
        {
            "ok": True,
            "seconds_advanced": result["seconds_advanced"],
            "offset_seconds": result["offset_seconds"],
            "gaps_opened": result["gaps_opened"],
            "deliveries": result["deliveries"],
        }
    )
