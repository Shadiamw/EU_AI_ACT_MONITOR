"""Shared fixtures. Tests run against a real PostgreSQL 16 database (see README)."""

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.pool import NullPool

os.environ.setdefault(
    "DATABASE_URL", "postgresql+psycopg://monitor_app:monitor_app@localhost:5432/monitor_test"
)
os.environ.setdefault(
    "DATABASE_ADMIN_URL",
    "postgresql+psycopg://monitor_owner:monitor_owner@localhost:5432/monitor_test",
)
os.environ.setdefault("ENVIRONMENT", "test")

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
