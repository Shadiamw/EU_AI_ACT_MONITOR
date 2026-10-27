"""Staff project-tester harness mounted under /system and /tester.

Staff registry bases should be:
  https://<host>/system
  https://<host>/tester
The autograder appends /test, /test/setup, /test/run, or /test/report.
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, harness
from app.errors import ApiError, not_found


def _require_test() -> None:
    if not config.is_test_environment():
        raise not_found()


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception:
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed") from None
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ApiError(400, "MALFORMED_BODY", "the request is malformed")
    return body


def build_router(role: str) -> APIRouter:
    """role is 'system' or 'tester'. Mounted at /{role} so paths become /{role}/test/..."""
    router = APIRouter()

    @router.api_route("/test", methods=["GET", "POST"])
    async def test_root() -> JSONResponse:
        _require_test()
        return JSONResponse(harness.service_info(role))

    @router.post("/test/setup")
    async def test_setup(request: Request) -> JSONResponse:
        _require_test()
        body = await _json_body(request)
        try:
            session = harness.setup(role, body)
        except Exception as exc:  # noqa: BLE001
            raise ApiError(500, "INTERNAL_ERROR", f"setup failed: {type(exc).__name__}") from exc
        return JSONResponse(
            {
                "session_id": session.session_id,
                "status": session.status,
                "role": role,
                "message": session.message,
            }
        )

    @router.post("/test/run")
    async def test_run(request: Request) -> JSONResponse:
        _require_test()
        body = await _json_body(request)
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            # Some graders call /test/run immediately after setup without a body;
            # create a fresh session when missing.
            session = harness.setup(role, body)
            session_id = session.session_id
        try:
            session = harness.run(session_id)
        except KeyError:
            raise ApiError(404, "NOT_FOUND", "unknown session_id") from None
        return JSONResponse(
            {
                "session_id": session.session_id,
                "status": session.status,
                "role": role,
                "message": session.message,
            }
        )

    @router.get("/test/report")
    async def test_report(session_id: str | None = None) -> JSONResponse:
        _require_test()
        if not session_id:
            raise ApiError(400, "MALFORMED_BODY", "session_id is required")
        session = harness.get(session_id)
        if session is None:
            raise ApiError(404, "NOT_FOUND", "unknown session_id")
        return JSONResponse(session.report())

    @router.post("/test/report")
    async def test_report_post(request: Request) -> JSONResponse:
        """Accept session_id in the body for graders that POST the report poll."""
        _require_test()
        body = await _json_body(request)
        session_id = body.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise ApiError(400, "MALFORMED_BODY", "session_id is required")
        session = harness.get(session_id)
        if session is None:
            raise ApiError(404, "NOT_FOUND", "unknown session_id")
        return JSONResponse(session.report())

    return router
