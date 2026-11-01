"""HTTP-only acceptance fixtures (Phase 4 spec 4.8; Part 4 decision 145).

These tests talk to a running stack over HTTP and nothing else. Run them with

    BASE_URL=http://127.0.0.1:8000 pytest tests/acceptance --confcutdir=tests/acceptance

`--confcutdir=tests/acceptance` keeps tests/conftest.py out: its session fixture downgrades and
upgrades the database named by DATABASE_ADMIN_URL, which must never happen against a stack the
runner is only visiting. Without BASE_URL every test here is skipped. The in-process suite
ignores this folder (`--ignore=tests/acceptance` in CI).

Environment: BASE_URL, INGEST_KEY (or INGEST_KEY_SYS_42), REVIEWER_KEY, VIEWER_KEY,
WEBHOOK_SINK_URL (where the API posts) and WEBHOOK_SINK_READ_URL (where this runner reads the
same channel back; defaults to WEBHOOK_SINK_URL).
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from tests.acceptance.api import Api, Sink

HERE = Path(__file__).resolve().parent

DEFAULT_KEYS = {
    "system": "test-ingest-key-sys-42-0d6f1c2e9b8a4f37",
    "reviewer": "test-reviewer-key-rev-1-5a7e3b9c1d2f4086",
    "viewer": "test-viewer-key-viewer-1-8c4d2e6f0a1b3957",
}


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: needs another Phase 4 part's real code (Phase 4 Addendum 9.4)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Without BASE_URL there is no stack to talk to: skip this folder's tests (only these)."""
    if os.environ.get("BASE_URL"):
        return
    skip = pytest.mark.skip(reason="BASE_URL is not set: no stack to run the HTTP runner against")
    for item in items:
        if item.path.is_relative_to(HERE):
            item.add_marker(skip)


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ["BASE_URL"].rstrip("/")


@pytest.fixture(scope="session")
def keys() -> dict[str, str]:
    return {
        "system": os.environ.get("INGEST_KEY")
        or os.environ.get("INGEST_KEY_SYS_42", DEFAULT_KEYS["system"]),
        "reviewer": os.environ.get("REVIEWER_KEY", DEFAULT_KEYS["reviewer"]),
        "viewer": os.environ.get("VIEWER_KEY", DEFAULT_KEYS["viewer"]),
    }


@pytest.fixture(scope="session")
def headers(keys: dict[str, str]) -> dict[str, dict[str, str]]:
    """Bearer headers per role: headers["reviewer"], headers["viewer"], headers["system"]."""
    return {role: {"Authorization": f"Bearer {key}"} for role, key in keys.items()}


@pytest.fixture
def http(base_url: str) -> Iterator[httpx.Client]:
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        yield client


@pytest.fixture(autouse=True)
def reset(http: httpx.Client, headers: dict[str, dict[str, str]]) -> None:
    """Every test starts from a reset stack (spec 4.8)."""
    response = http.post("/v1/test/reset", headers=headers["reviewer"])
    assert response.status_code == 204, response.text


@pytest.fixture
def api(http: httpx.Client, headers: dict[str, dict[str, str]]) -> Api:
    """The HTTP helpers of tests/acceptance/api.py, after the reset."""
    return Api(http, headers)


@pytest.fixture
def sink() -> Sink:
    url = os.environ.get("WEBHOOK_SINK_URL")
    if not url:
        pytest.skip("WEBHOOK_SINK_URL is not set: no webhook receiver")
    channel = Sink(url=url, read_url=os.environ.get("WEBHOOK_SINK_READ_URL") or url)
    channel.clear()
    return channel
