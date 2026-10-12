"""STUB for Part 1 (errors). ApiError and the global error handlers (docs/decisions.md 27, 28).

Every error body is exactly {"error_code": "...", "message": "..."}:

- ApiError: its own status, code and message.
- Unknown route, and a wrong method on a known route: 404 NOT_FOUND (the spec has no 405 code).
- Request validation (FastAPI's default 422): 400 MALFORMED_BODY.
- LedgerError and any other unexpected exception: 500 INTERNAL_ERROR with a generic message.
  Only the exception's type name is logged: never its message, a stack trace, a payload value
  or a hash.
"""

import json
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("api")

INTERNAL_ERROR_MESSAGE = "an internal error occurred"


class ApiError(Exception):
    def __init__(self, status: int, error_code: str, message: str) -> None:
        super().__init__(error_code)
        self.status = status
        self.error_code = error_code
        self.message = message


def error_response(status: int, error_code: str, message: str) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error_code": error_code, "message": message})


def not_found() -> ApiError:
    return ApiError(404, "NOT_FOUND", "resource not found")


async def _api_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, ApiError)
    return error_response(exc.status, exc.error_code, exc.message)


async def _http_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, StarletteHTTPException)
    if exc.status_code in (404, 405):
        return error_response(404, "NOT_FOUND", "resource not found")
    # Part 2 raises no other HTTP exception; no code is invented for one.
    logger.error("unexpected HTTP exception: status=%d", exc.status_code)
    return error_response(500, "INTERNAL_ERROR", INTERNAL_ERROR_MESSAGE)


async def _validation_error(request: Request, exc: Exception) -> JSONResponse:
    return error_response(400, "MALFORMED_BODY", "the request is malformed")


class InternalErrorMiddleware:
    """Turns any exception that reaches it (LedgerError included) into a 500 contract body.

    It sits inside Starlette's ServerErrorMiddleware, so the exception is handled here and is
    not re-raised or logged with a traceback.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        response_started = False

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, receive, tracking_send)
        except Exception as exc:
            logger.error("unhandled exception: type=%s", type(exc).__name__)
            if response_started:
                raise
            body = json.dumps(
                {"error_code": "INTERNAL_ERROR", "message": INTERNAL_ERROR_MESSAGE},
                separators=(",", ":"),
            ).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 500,
                    "headers": [
                        (b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode()),
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body})


def install(app: FastAPI) -> None:
    """Register the global error handlers on an app."""
    handlers: dict[Any, Any] = {
        ApiError: _api_error,
        StarletteHTTPException: _http_error,
        RequestValidationError: _validation_error,
    }
    for exc_class, handler in handlers.items():
        app.add_exception_handler(exc_class, handler)
    app.add_middleware(InternalErrorMiddleware)
