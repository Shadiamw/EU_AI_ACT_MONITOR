"""Shared fixtures. Tests run against a real PostgreSQL 16 database (see README)."""

import json
import os
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

from app.platform import with_phase4_tables

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://monitor_app:monitor_app@localhost:5432/monitor_test"
)
os.environ.setdefault(
    "DATABASE_ADMIN_URL",
    "postgresql+psycopg://monitor_owner:monitor_owner@localhost:5432/monitor_test",
)
os.environ.setdefault("ENVIRONMENT", "test")
# The seeded credentials (spec 5). Test-only values; Part 1's seed stores only their digests.
os.environ.setdefault("INGEST_KEY_SYS_42", "test-ingest-key-sys-42-0d6f1c2e9b8a4f37")
os.environ.setdefault("REVIEWER_KEY", "test-reviewer-key-rev-1-5a7e3b9c1d2f4086")
os.environ.setdefault("VIEWER_KEY", "test-viewer-key-viewer-1-8c4d2e6f0a1b3957")

ROOT = Path(__file__).resolve().parent.parent


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-integration",
        action="store_true",
        default=False,
        help="also run the tests marked integration (Phase 4 Addendum 9.4)",
    )


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "integration: needs another Phase 4 part's real code (Phase 4 Addendum 9.4)"
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Skip the integration-marked tests unless --run-integration is given. Selected by marker,
    never by keyword, because a keyword also matches test names (Phase 4 Addendum 9.4)."""
    if config.getoption("--run-integration"):
        return
    skip = pytest.mark.skip(reason="integration test: run with --run-integration")
    for item in items:
        if item.get_closest_marker("integration") is not None:
            item.add_marker(skip)


def alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


@pytest.fixture(scope="session")
def owner_engine() -> Iterator[Engine]:
    engine = create_engine(os.environ["DATABASE_ADMIN_URL"], poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def app_engine() -> Iterator[Engine]:
    engine = create_engine(os.environ["DATABASE_URL"], poolclass=NullPool)
    yield engine
    engine.dispose()


@pytest.fixture(scope="session", autouse=True)
def migrated_db(owner_engine: Engine, app_engine: Engine) -> None:
    """Rebuild the schema from scratch once per run, as the owner role."""
    for engine in (owner_engine, app_engine):
        with engine.connect() as conn:
            is_superuser = conn.execute(
                text("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
            ).scalar_one()
        # A superuser bypasses grants, which would hide permission bugs.
        assert not is_superuser, f"{engine.url.username} must not be a superuser"
    command.downgrade(alembic_config(), "base")
    command.upgrade(alembic_config(), "head")


@pytest.fixture(autouse=True)
def empty_ledger(owner_engine: Engine) -> None:
    """Every test starts with an empty ledger. Truncation as the owner is allowed (addendum 1)."""
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entry"))


# Part 1's tables, and the Part 3 tables whose foreign keys reference event.
PART1_TABLES = (
    "metric_sample",
    "breach_series",
    "notification_delivery",
    "notification",
    "harm_field",
    "event",
    "api_key",
    "threshold_version",
)


@pytest.fixture(autouse=True)
def empty_part1_tables(owner_engine: Engine) -> None:
    """Every test starts with no events, samples, keys or thresholds. A separate fixture from
    empty_ledger, which the chain scenario in tests/api/test_ledger_system.py overrides."""
    with owner_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {', '.join(with_phase4_tables(conn, PART1_TABLES))}"))


# The webhook receiver (moved here from tests/worker/conftest.py, Phase 4 Addendum 9.1): a real
# HTTP server on 127.0.0.1 that records every webhook call and answers with the statuses the
# test queues. A test can run a check inside the call with `during`.


@dataclass
class Call:
    path: str
    body: Any
    observed: Any = None  # what `during` returned inside this call


@dataclass
class Receiver:
    url: str
    calls: list[Call] = field(default_factory=list)
    statuses: list[int] = field(default_factory=list)  # answered in order, then default
    default_status: int = 200
    during: Callable[[], Any] | None = None

    def next_status(self) -> int:
        return self.statuses.pop(0) if self.statuses else self.default_status


@pytest.fixture
def receiver() -> Iterator[Receiver]:
    state: Receiver

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length) or b"null")
            observed = state.during() if state.during is not None else None
            state.calls.append(Call(self.path, body, observed))
            status = state.next_status()
            self.send_response(status)
            if 300 <= status < 400:
                self.send_header("Location", "http://127.0.0.1:9/elsewhere")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state = Receiver(url=f"http://127.0.0.1:{server.server_port}/hook")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()
    thread.join()
