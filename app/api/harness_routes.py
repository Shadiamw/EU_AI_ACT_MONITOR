"""Staff / cross-team harness mounted under /system and /tester.

Registry bases (no /test suffix):
  https://<host>/system
  https://<host>/tester

Interface: student-facing Cross-team Testing Interface v2.
Error bodies for these endpoints use {message, source} (not project error_code).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app import config, harness
from app.errors import not_found


async def _run_sync(fn: Callable[..., Any], *args: Any) -> Any:
    """Run blocking harness work off the event loop (enables loopback HTTP)."""
    return await asyncio.to_thread(fn, *args)


def _require_test() -> None:
    if not config.is_test_environment():
        raise not_found()


async def _json_body(request: Request) -> dict[str, Any]:
    raw = await request.body()
    if not raw:
        return {}
    try:
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise harness.HarnessError(400, "the request is malformed", "unknown") from exc
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise harness.HarnessError(400, "the request is malformed", "unknown")
    return body


def _err(exc: harness.HarnessError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status,
        content={"message": exc.message, "source": exc.source},
    )


def build_router(role: str) -> APIRouter:
    router = APIRouter()

    @router.api_route("/test", methods=["GET", "POST"])
    async def test_root() -> JSONResponse:
        _require_test()
        return JSONResponse(harness.service_info(role))

    if role == "system":
        _mount_system(router)
    else:
        _mount_tester(router)

    return router


def _mount_system(router: APIRouter) -> None:
    @router.post("/test/setup")
    async def system_setup(request: Request) -> JSONResponse:
        _require_test()
        try:
            body = await _json_body(request)
            payload = harness.system_setup(body)
        except harness.HarnessError as exc:
            return _err(exc)
        except Exception as exc:  # noqa: BLE001
            return _err(
                harness.HarnessError(
                    500,
                    f"Could not initialize the session: {type(exc).__name__}",
                    "system",
                )
            )
        return JSONResponse(payload, status_code=201)

    @router.get("/test/report")
    async def system_report(session_id: str | None = None) -> JSONResponse:
        _require_test()
        try:
            return JSONResponse(harness.system_report(session_id or ""))
        except harness.HarnessError as exc:
            return _err(exc)


def _mount_tester(router: APIRouter) -> None:
    @router.post("/test/setup")
    async def tester_setup(request: Request) -> JSONResponse:
        _require_test()
        try:
            body = await _json_body(request)
            # May call remote system setup (or in-process self); keep async only for body read.
            payload = await _run_sync(harness.tester_setup, body)
        except harness.HarnessError as exc:
            return _err(exc)
        except Exception as exc:  # noqa: BLE001
            return _err(
                harness.HarnessError(
                    500, f"tester setup failed: {type(exc).__name__}", "tester"
                )
            )
        return JSONResponse(payload, status_code=201)

    @router.post("/test/run")
    async def tester_run(request: Request) -> JSONResponse:
        """Run in a worker thread so loopback project probes can be served."""
        _require_test()
        try:
            body = await _json_body(request)
            raw_id = body.get("session_id")
            if not isinstance(raw_id, str) or not raw_id:
                raise harness.HarnessError(400, "session_id is required", "tester")
            report = await _run_sync(harness.tester_run, raw_id)
        except harness.HarnessError as exc:
            # Spec: 500 + message/source tester on abort; report retrievable via /test/report.
            if exc.status == 500 and exc.source == "tester":
                return JSONResponse(
                    status_code=500,
                    content={"message": exc.message, "source": "tester"},
                )
            return _err(exc)
        return JSONResponse(report, status_code=200)

    @router.get("/test/report")
    async def tester_report(session_id: str | None = None) -> JSONResponse:
        _require_test()
        try:
            return JSONResponse(harness.tester_report(session_id or ""))
        except harness.HarnessError as exc:
            return _err(exc)
