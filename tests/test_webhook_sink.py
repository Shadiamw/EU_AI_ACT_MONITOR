"""tools/webhook_sink.py (Phase 4 spec 4.9; decision 143), against a real server thread."""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import datetime
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from tools import webhook_sink


@pytest.fixture
def sink_url() -> Iterator[str]:
    with webhook_sink._LOCK:
        webhook_sink._STORE.clear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), webhook_sink.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _call(method: str, url: str, body: bytes | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(url, data=body, method=method)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"null")


def test_post_stores_the_body_with_its_receive_time_and_returns_200(sink_url: str) -> None:
    status, answer = _call("POST", f"{sink_url}/hook/ci", b'{"notification_type": "reminder"}')
    assert (status, answer) == (200, {"ok": True})
    status, items = _call("GET", f"{sink_url}/hook/ci")
    assert status == 200
    assert [item["body"] for item in items] == [{"notification_type": "reminder"}]
    received = items[0]["received_at"]
    assert received.endswith("Z")
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", received), received
    assert datetime.fromisoformat(received.replace("Z", "+00:00")).tzinfo is not None


def test_get_lists_in_arrival_order_and_delete_clears(sink_url: str) -> None:
    for n in range(3):
        _call("POST", f"{sink_url}/hook/ci", json.dumps({"n": n}).encode())
    assert [item["body"]["n"] for item in _call("GET", f"{sink_url}/hook/ci")[1]] == [0, 1, 2]
    assert _call("DELETE", f"{sink_url}/hook/ci") == (200, {"ok": True})
    assert _call("GET", f"{sink_url}/hook/ci") == (200, [])


def test_channels_are_independent(sink_url: str) -> None:
    _call("POST", f"{sink_url}/hook/a", b'{"to": "a"}')
    _call("POST", f"{sink_url}/hook/b", b'{"to": "b"}')
    _call("DELETE", f"{sink_url}/hook/a")
    assert _call("GET", f"{sink_url}/hook/a") == (200, [])
    assert [item["body"] for item in _call("GET", f"{sink_url}/hook/b")[1]] == [{"to": "b"}]


def test_an_unknown_channel_is_an_empty_list(sink_url: str) -> None:
    assert _call("GET", f"{sink_url}/hook/never-used") == (200, [])


def test_a_body_that_is_not_json_is_kept_raw(sink_url: str) -> None:
    assert _call("POST", f"{sink_url}/hook/raw", b"not json")[0] == 200
    assert _call("GET", f"{sink_url}/hook/raw")[1][0]["body"] == {"_raw": "not json"}


@pytest.mark.parametrize("path", ["/", "/hook", "/hook/", "/hook/a/b", "/other/a"])
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_other_paths_are_404(sink_url: str, method: str, path: str) -> None:
    body = b"{}" if method == "POST" else None
    assert _call(method, f"{sink_url}{path}", body) == (404, {"error": "not found"})
