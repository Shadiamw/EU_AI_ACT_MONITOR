"""Shared fixtures. Tests run against a real PostgreSQL 16 database (see README)."""

import os
from collections.abc import Iterator
from pathlib import Path

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
