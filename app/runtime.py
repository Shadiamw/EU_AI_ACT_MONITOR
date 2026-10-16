"""Container start-up (decision 109).

`prepare` waits for PostgreSQL, migrates as the owner and seeds the ledger.
`api` and `worker` wait until that migration has reached the script head, seed again
(a second call appends nothing) and then become the server or the worker process.
"""

import os
import sys
from pathlib import Path
from time import monotonic, sleep

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.config import database_admin_url
from app.db import get_engine
from app.ledger.canonical import Payload
from app.ledger.service import ensure_seed

ROOT = Path(__file__).resolve().parent.parent

# Addendum rule 7, the same payload the tests seed (integration_notes.md 5.4).
SEED_PAYLOAD: Payload = {"systems": ["sys-42"], "threshold_count": "2"}


def _alembic_config() -> Config:
    return Config(str(ROOT / "alembic.ini"))


def head_revision() -> str:
    head = ScriptDirectory.from_config(_alembic_config()).get_current_head()
    if head is None:
        raise RuntimeError("alembic has no head revision")
    return head


def _admin_engine() -> Engine:
    return create_engine(database_admin_url(), poolclass=NullPool)


def wait_for_database(timeout_s: float = 60) -> None:
    """Block until the owner role can run a query. A restart during first-time init is retried."""
    deadline = monotonic() + timeout_s
    last: BaseException | None = None
    while True:
        engine = _admin_engine()
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            return
        except Exception as exc:
            last = exc
            if monotonic() >= deadline:
                raise RuntimeError("database did not become ready") from last
            sleep(1)
        finally:
            engine.dispose()


def schema_version() -> str | None:
    engine = _admin_engine()
    try:
        with engine.connect() as conn:
            present = conn.execute(
                text("SELECT to_regclass('public.alembic_version')")
            ).scalar_one()
            if present is None:
                return None
            row = conn.execute(text("SELECT version_num FROM alembic_version")).first()
            return None if row is None else str(row[0])
    finally:
        engine.dispose()


def wait_for_head(timeout_s: float = 120) -> None:
    head = head_revision()
    deadline = monotonic() + timeout_s
    while schema_version() != head:
        if monotonic() >= deadline:
            raise RuntimeError(f"migrations did not reach {head}")
        sleep(1)


def seed() -> None:
    with Session(get_engine()) as session:
        ensure_seed(session, SEED_PAYLOAD)
        session.commit()


def prepare() -> None:
    wait_for_database()
    command.upgrade(_alembic_config(), "head")
    seed()


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    command_name = args[0] if args else "api"
    if command_name == "prepare":
        prepare()
        return
    if command_name == "api":
        wait_for_head()
        seed()
        os.execvp(
            sys.executable,
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                "8000",
            ],
        )
    if command_name == "worker":
        wait_for_head()
        seed()
        from app.worker import main as worker_main

        worker_main()
        return
    raise SystemExit("usage: python -m app.runtime [prepare|api|worker]")


if __name__ == "__main__":
    main()
