"""Request logging and counters for the API (spec 1.8; decision 126).

- One JSON log line per request on the `api.request` logger, at INFO (ERROR for a 5xx other
  than the stubs' 501):
  ts, level, request_id, actor_id, method, path, status, duration_ms, plus event_id when the
  request is about one event, error_code for an error response and outcome for an ingest.
  The path is logged without its query string. Nothing else from the request is logged: no
  header (so never a key), no body (so never a webhook URL), no query value.
- Counters, kept in memory per process and readable with `counter_values()`: samples
  accepted, duplicates, breaches, series joins, and 4xx responses by error code. The ingest
  outcome on the log line makes the same counts recoverable from the logs.
- Threshold evaluations are logged at DEBUG on `api.detection` by app/ingestion.py.

The web process's log handler is Part 4's (the server's logging configuration); the `api`
logger's level follows LOG_LEVEL (app/main.py).
"""

import json
import logging
import threading
import time
import uuid
from collections import Counter
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app import clock
from app.formats import api_time

logger = logging.getLogger("api.request")

SAMPLES_ACCEPTED = "samples_accepted"
DUPLICATES = "duplicates"
BREACHES = "breaches"
SERIES_JOINS = "series_joins"

_counters: Counter[str] = Counter()
_lock = threading.Lock()


def count(name: str) -> None:
    with _lock:
        _counters[name] += 1


def counter_values() -> dict[str, int]:
    """A snapshot of this process's counters. 4xx responses are under `4xx:<error_code>`."""
    with _lock:
        return dict(_counters)


def reset_counters() -> None:
    with _lock:
        _counters.clear()


def note(request: Request, **fields: str) -> None:
    """Attach fields (event_id, outcome) to the current request's log line."""
    for key, value in fields.items():
        setattr(request.state, f"log_{key}", value)


class RequestLogMiddleware:
    """Writes the request log line after the response, and counts 4xx by error code."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()
        request_id = uuid.uuid4().hex
        status = 500
        error_body = bytearray()

        async def logging_send(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and status >= 400:
                error_body.extend(message.get("body", b""))
            await send(message)

        try:
            await self.app(scope, receive, logging_send)
        finally:
            try:
                self._log(scope, request_id, status, started, bytes(error_body))
            except Exception as exc:  # a log line must never change the response
                logger.warning("request log failed: type=%s", type(exc).__name__)

    @staticmethod
    def _log(scope: Scope, request_id: str, status: int, started: float, body: bytes) -> None:
        state = scope.get("state", {})
        # A 501 from the Phase 3 stubs is the expected answer, not a failure (decision 126).
        level = logging.ERROR if status >= 500 and status != 501 else logging.INFO
        line: dict[str, Any] = {
            "ts": api_time(clock.now()),
            "level": logging.getLevelName(level),
            "request_id": request_id,
            "actor_id": state.get("actor_id"),
            "method": scope["method"],
            "path": scope["path"],
            "status": status,
            "duration_ms": int((time.perf_counter() - started) * 1000),
        }
        for key in ("event_id", "outcome"):
            if f"log_{key}" in state:
                line[key] = state[f"log_{key}"]
        if status >= 400:
            error_code = _error_code(body)
            if error_code is not None:
                line["error_code"] = error_code
                if status < 500:
                    count(f"4xx:{error_code}")
        logger.log(level, json.dumps(line, separators=(",", ":")))


def _error_code(body: bytes) -> str | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    code = parsed.get("error_code") if isinstance(parsed, dict) else None
    return code if isinstance(code, str) else None
