"""Fixtures for the Phase 5 Part 4 tests: the real app, real keys and a real PostgreSQL database,
re-exported from the Part 1 and Part 2 fixtures (fake clock, seed, in-process client)."""

import pytest

from app import harness
from tests.part1.conftest import client as client
from tests.part1.conftest import fake_clock as fake_clock
from tests.part1.conftest import seeded as seeded

REVIEWER_2_KEY = "test-reviewer-key-rev-2-3e8b1f6a9c2d4705"


@pytest.fixture(autouse=True)
def _fresh_system_sessions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fix round 2 (D-1, one active system session): system sessions live in module memory for
    the whole pytest process, so each test starts with none; a session left open by one test
    must not answer 409 to the next."""
    monkeypatch.setattr(harness, "_system_sessions", {})
