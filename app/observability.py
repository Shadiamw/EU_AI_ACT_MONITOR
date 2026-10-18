"""Request logging and counters for the API (spec 1.8; decision 126).

- One JSON log line per request on the `api.request` logger, at INFO (ERROR for a 5xx other
  than the stubs' 501):
  ts, level, request_id, actor_id, method, path, status, duration_ms, plus event_id when the
  request is about one event, error_code for an error response and outcome for an ingest.
  The path is logged without its query string. Nothing else from the request is logged: no
  header (so never a key), no body (so never a webhook URL), no query value.
- Counters, kept in memory per process and readable with `counter_values()`: samples
  accepted, duplicates, breaches, series joins, and 4xx responses by error code. Each change
  is logged as one JSON line `{ts, level, counters}` with the process's current totals on the
  `api.counters` logger, at INFO.
- Threshold evaluations are logged at DEBUG on `api.detection` by app/ingestion.py.

`configure_logging` (called by app/main.py) gives the `api` logger its own stream handler and
sets its level from LOG_LEVEL, so the lines reach the server's output without any server log
configuration (decision 132). Records still propagate, so a root handler that Part 4 adds sees
them too; Part 4 then removes this handler (integration_notes.md 5.12).
"""

import json
import logging
import sys
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
counters_logger = logging.getLogger("api.counters")

SAMPLES_ACCEPTED = "samples_accepted"
DUPLICATES = "duplicates"
BREACHES = "breaches"
SERIES_JOINS = "series_joins"

_counters: Counter[str] = Counter()
_lock = threading.Lock()


def count(name: str) -> None:
    with _lock:
        _counters[name] += 1
        totals = dict(_counters)
    line = {"ts": api_time(clock.now()), "level": "INFO", "counters": totals}
    counters_logger.info(json.dumps(line, separators=(",", ":")))


def counter_values() -> dict[str, int]:
    """A snapshot of this process's counters. 4xx responses are under `4xx:<error_code>`."""
    with _lock:
        return dict(_counters)


def reset_counters() -> None:
    with _lock:
        _counters.clear()


class _ApiFormatter(logging.Formatter):
    """JSON lines as they are; any other line with its level and logger name."""

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if message.startswith("{"):
            return message
        return f"{record.levelname} [{record.name}] {message}"


def configure_logging(level: str) -> None:
    """Attach one stream handler to the `api` logger (once per process) and set its level."""
    api_logger = logging.getLogger("api")
    api_logger.setLevel(level)
    if not any(getattr(h, "_api_handler", False) for h in api_logger.handlers):
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_ApiFormatter())
        handler._api_handler = True  # type: ignore[attr-defined]
        api_logger.addHandler(handler)


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
