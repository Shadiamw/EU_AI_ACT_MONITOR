"""POST /v1/test/reset, clock advance, and suite runner (Phase 4 spec 4.5, Addendum rule 7).

Outside ENVIRONMENT=test these return the standard 404 before any key check. Inside test they
require a key: REVIEWER or VIEWER for reset and clock advance; REVIEWER only for the suite.
"""

from __future__ import annotations

import hmac
import logging
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse, Response

from app import auth, clock, config, platform, suite_runner
from app.auth import Principal, bearer_key, key_digest
from app.errors import ApiError, not_found
from app.formats import api_time

router = APIRouter()
logger = logging.getLogger("api")


def _ensure_test_environment() -> None:
    """404 before any key check when ENVIRONMENT is not test (Addendum rule 7)."""
    if not config.is_test_environment():
        raise not_found()


def _principal_from_seed_keys(authorization: str | None) -> Principal | None:
    """Match the configured seed keys when api_key is empty (bootstrap for reset)."""
    key = bearer_key(authorization)
    if key is None:
        return None
    presented = key_digest(key).encode("ascii")
    for raw, role, actor_id in (
        (config.reviewer_key(), "REVIEWER", "rev-1"),
        (config.viewer_key(), "VIEWER", "viewer-1"),
    ):
        if hmac.compare_digest(key_digest(raw).encode("ascii"), presented):
            return Principal(role=role, actor_id=actor_id, system_id=None)
    return None


def _require_test(*roles: str):
    """Environment check first, then the usual 401 / 403 key check."""
    allowed = frozenset(roles)

    def dependency(
        request: Request,
        authorization: Annotated[str | None, Header()] = None,
    ) -> Principal:
        _ensure_test_environment()
        principal = auth.resolve_principal(authorization)
        if principal is None:
            principal = _principal_from_seed_keys(authorization)
        if principal is None:
            raise ApiError(401, "MISSING_OR_INVALID_KEY", "missing or invalid API key")
        request.state.actor_id = principal.actor_id
        if principal.role not in allowed:
            raise ApiError(403, "FORBIDDEN_ROLE", "this role may not call this endpoint")
        return principal

    return dependency


TestReader = Annotated[Principal, Depends(_require_test("REVIEWER", "VIEWER"))]
TestReviewer = Annotated[Principal, Depends(_require_test("REVIEWER"))]


@router.post("/v1/test/reset")
def test_reset(_principal: TestReader) -> Response:
    platform.reset()
    return Response(status_code=204)


@router.post("/v1/test/clock/advance")
async def test_clock_advance(request: Request, _principal: TestReader) -> JSONResponse:
    try:
        body: Any = await request.json()
    except Exception:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed") from None
    if not isinstance(body, dict) or set(body) != {"seconds"}:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed")
    seconds = body["seconds"]
    if not isinstance(seconds, int) or isinstance(seconds, bool) or seconds < 0:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed")
    try:
        result = platform.advance_clock(seconds)
    except ValueError:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed") from None
    # Contract body is only simulated_time; the old fields are logged (spec 4.5).
    gaps_opened = result["gaps_opened"]
    logger.info(
        "clock advance: seconds_advanced=%s offset_seconds=%s gaps_opened=%d "
        "reminders_fired=%s deliveries=%s",
        result["seconds_advanced"],
        result["offset_seconds"],
        len(gaps_opened) if isinstance(gaps_opened, list) else 0,
        result["reminders_fired"],
        result["deliveries"],
    )
    return JSONResponse({"simulated_time": api_time(clock.now())})


@router.get("/v1/test/suite")
def test_suite_status(_principal: TestReviewer) -> JSONResponse:
    """Latest suite run (or null). Poll while status is queued/running."""
    run = suite_runner.current_run()
    return JSONResponse({"run": None if run is None else run.to_dict()})


@router.post("/v1/test/suite")
async def test_suite_start(request: Request, _principal: TestReviewer) -> JSONResponse:
    """Start smoke, acceptance, or full. Body: {"scope": "smoke"|"acceptance"|"full"}."""
    try:
        body: Any = await request.json()
    except Exception:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed") from None
    if not isinstance(body, dict) or set(body) != {"scope"}:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed")
    scope = body["scope"]
    if not isinstance(scope, str):
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed")
    try:
        run = suite_runner.start(scope)
    except ValueError:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed") from None
    except RuntimeError:
        current = suite_runner.current_run()
        return JSONResponse(
            {
                "accepted": False,
                "message": "a suite run is already in progress",
                "run": None if current is None else current.to_dict(),
            },
            status_code=200,
        )
    return JSONResponse({"accepted": True, "run": run.to_dict()}, status_code=202)
