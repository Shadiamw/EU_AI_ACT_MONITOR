"""HTTP-only acceptance fixtures (Phase 4 spec 4.8). Marked integration as a whole."""

from __future__ import annotations

import os

import httpx
import pytest

pytestmark = pytest.mark.integration


@pytest.fixture(scope="session")
def base_url() -> str:
    return os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")


@pytest.fixture(scope="session")
def keys() -> dict[str, str]:
    return {
        "system": os.environ.get("INGEST_KEY")
        or os.environ.get("INGEST_KEY_SYS_42", "test-ingest-key-sys-42-0d6f1c2e9b8a4f37"),
        "reviewer": os.environ.get("REVIEWER_KEY", "test-reviewer-key-rev-1-5a7e3b9c1d2f4086"),
        "viewer": os.environ.get("VIEWER_KEY", "test-viewer-key-viewer-1-8c4d2e6f0a1b3957"),
    }


@pytest.fixture
def http(base_url: str) -> httpx.Client:
    with httpx.Client(base_url=base_url, timeout=60.0) as client:
        yield client


def bearer(keys: dict[str, str], role: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {keys[role]}"}
