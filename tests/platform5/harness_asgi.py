"""Route the tester's outbound HTTP (app.harness._http and app.harness_model.fetch) into the
ASGI app, so the tester endpoints, the system endpoints and every probe run in process against
the real app and database. Only the transport is replaced: the URL's path and query, the
method, the headers and the body are passed through unchanged, and the host is ignored (every
host is "the app"). A target listed in `unreachable` gets status 0, as a timeout would."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any
from urllib.parse import urlsplit

import pytest

from app import harness, harness_model, platform
from app.main import create_app
from tests.api.conftest import ApiClient


def _path(url: str) -> str:
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def make_transport(
    client: ApiClient, unreachable: tuple[str, ...] = ()
) -> tuple[
    Callable[..., tuple[int, str]],
    Callable[..., harness_model.Response],
]:
    def blocked(url: str) -> bool:
        return any(url.startswith(prefix) for prefix in unreachable)

    def http(
        method: str,
        url: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 12.0,
    ) -> tuple[int, str]:
        if blocked(url):
            return 0, "TimeoutError: timed out"
        sent = dict(headers or {})
        if body is not None:
            sent.setdefault("Content-Type", "application/json")
        response = client.request(method, _path(url), content=body, headers=sent)
        return response.status_code, response.text

    def fetch(
        method: str, url: str, payload: Any = None, key: str = "", timeout: float = 20.0
    ) -> harness_model.Response:
        if blocked(url):
            return harness_model.Response(0, {}, b"TimeoutError: timed out")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        kwargs: dict[str, Any] = {} if payload is None else {"json": payload}
        response = client.request(method, _path(url), headers=headers, **kwargs)
        return harness_model.Response(
            response.status_code,
            {k.lower(): v for k, v in response.headers.items()},
            response.content,
        )

    return http, fetch


SELF_BASE = "http://suite-self.pod-h.test"
"""SUITE_BASE_URL for these tests: the tester treats SELF_BASE + "/system" as its own system."""


def target_keys() -> dict[str, str]:
    """The seeded test keys, in the tester setup body's names (app.harness._access_from_body)."""
    from app import config

    return {
        "ingest_key": config.ingest_key_sys_42(),
        "reviewer_key": config.reviewer_key(),
        "viewer_key": config.viewer_key(),
    }


@pytest.fixture
def harness_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[ApiClient]:
    """The app with the tester's outbound HTTP routed into it, from a reset database. The test
    clock is put back afterwards (the probes move it by 186 days)."""
    monkeypatch.setenv("SUITE_BASE_URL", SELF_BASE)
    monkeypatch.delenv("SAME_TEAM_SYSTEM_URLS", raising=False)
    with ApiClient(create_app()) as client:
        http, fetch = make_transport(client)
        monkeypatch.setattr(harness, "_http", http)
        monkeypatch.setattr(harness_model, "fetch", fetch)
        platform.reset()
        yield client
    platform.set_offset_seconds(0)
