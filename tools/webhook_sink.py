#!/usr/bin/env python3
"""Public webhook receiver for Phase 4 demos and acceptance (spec 4.9, LIM-06).

Standard library only. POST /hook/{channel} stores the JSON body with its receive time and
returns 200 so the verification ping passes. GET /hook/{channel} returns the stored list.
DELETE /hook/{channel} clears it. Memory only; a restart clears every channel.

Anything sent to a channel is readable by anyone who knows the channel name.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Lock
from urllib.parse import unquote

_STORE: dict[str, list[dict[str, object]]] = {}
_LOCK = Lock()


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _channel(path: str) -> str | None:
    parts = path.strip("/").split("/")
    if len(parts) != 2 or parts[0] != "hook" or not parts[1]:
        return None
    return unquote(parts[1])


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A003
        return

    def _send(self, status: int, body: object) -> None:
        raw = json.dumps(body).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self) -> None:  # noqa: N802
        channel = _channel(self.path)
        if channel is None:
            self._send(404, {"error": "not found"})
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            payload = json.loads(raw.decode("utf-8") or "null")
        except json.JSONDecodeError:
            payload = {"_raw": raw.decode("utf-8", errors="replace")}
        entry = {"received_at": _now(), "body": payload}
        with _LOCK:
            _STORE.setdefault(channel, []).append(entry)
        self._send(200, {"ok": True})

    def do_GET(self) -> None:  # noqa: N802
        channel = _channel(self.path)
        if channel is None:
            self._send(404, {"error": "not found"})
            return
        with _LOCK:
            items = list(_STORE.get(channel, []))
        self._send(200, items)

    def do_DELETE(self) -> None:  # noqa: N802
        channel = _channel(self.path)
        if channel is None:
            self._send(404, {"error": "not found"})
            return
        with _LOCK:
            _STORE[channel] = []
        self._send(200, {"ok": True})


def main() -> None:
    host = os.environ.get("WEBHOOK_SINK_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBHOOK_SINK_PORT", "8765"))
    server = ThreadingHTTPServer((host, port), Handler)
    print(f"webhook sink on http://{host}:{port}/hook/{{channel}}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
