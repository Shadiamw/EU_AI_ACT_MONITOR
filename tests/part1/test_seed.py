"""The Part 4 seed stand-in (tools/seed_part1.py; decision 128) installs exactly the spec's seed
fixtures (spec 4, Seed fixtures; spec 4.4; spec 5)."""

import hashlib
from decimal import Decimal

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import config
from app.platform import with_phase4_tables
from tests.part1.conftest import T0, rows
from tools.seed_part1 import seed

FIXTURE_QUERIES = {
    "monitored_system": "SELECT system_id, sampling_interval_s, gap_timeout_intervals,"
    " last_received_at FROM monitored_system",
    "threshold_version": "SELECT threshold_version_id, system_id, metric_name, metric_type,"
    " comparator, limit_value, version FROM threshold_version ORDER BY threshold_version_id",
    "api_key": "SELECT key_hash, role, actor_id, system_id FROM api_key ORDER BY actor_id",
    "ledger_entry": "SELECT seq, entry_type, actor_id, subject_id, payload FROM ledger_entry",
}


def digest(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def test_seed_installs_the_spec_fixtures_from_the_environment(
    seeded: None, owner_engine: Engine
) -> None:
    assert rows(owner_engine, FIXTURE_QUERIES["monitored_system"]) == [
        {
            "system_id": "sys-42",
            "sampling_interval_s": 300,
            "gap_timeout_intervals": 3,
            "last_received_at": T0,  # seed time
        }
    ]
    assert rows(owner_engine, FIXTURE_QUERIES["threshold_version"]) == [
        {
            "threshold_version_id": "thr-1",
            "system_id": "sys-42",
            "metric_name": "accuracy_drift",
            "metric_type": "drift",
            "comparator": "gt",
            "limit_value": Decimal("0.30"),
            "version": 1,
        },
        {
            "threshold_version_id": "thr-2",
            "system_id": "sys-42",
            "metric_name": "accuracy",
            "metric_type": "performance",
            "comparator": "lt",
            "limit_value": Decimal("0.70"),
            "version": 1,
        },
    ]
    assert rows(owner_engine, FIXTURE_QUERIES["api_key"]) == [
        {
            "key_hash": digest(config.reviewer_key()),
            "role": "REVIEWER",
            "actor_id": "rev-1",
            "system_id": None,
        },
        {
            "key_hash": digest(config.ingest_key_sys_42()),
            "role": "SYSTEM",
            "actor_id": "sys-42",
            "system_id": "sys-42",
        },
        {
            "key_hash": digest(config.viewer_key()),
            "role": "VIEWER",
            "actor_id": "viewer-1",
            "system_id": None,
        },
    ]
    # SEED { systems: [ids], threshold_count } (spec 2.4), with Part 2's string count.
    assert rows(owner_engine, FIXTURE_QUERIES["ledger_entry"]) == [
        {
            "seq": 1,
            "entry_type": "SEED",
            "actor_id": "system",
            "subject_id": None,
            "payload": {"systems": ["sys-42"], "threshold_count": "2"},
        }
    ]
    assert rows(owner_engine, "SELECT count(*) AS n FROM webhook") == [{"n": 0}]


def test_seed_is_idempotent(seeded: None, owner_engine: Engine) -> None:
    before = {name: rows(owner_engine, sql) for name, sql in FIXTURE_QUERIES.items()}
    with Session(owner_engine) as session:
        seed(session)
        session.commit()
    assert {name: rows(owner_engine, sql) for name, sql in FIXTURE_QUERIES.items()} == before


@pytest.mark.parametrize("environment", ["production", "staging", ""])
def test_seed_refuses_outside_the_test_environment(
    owner_engine: Engine, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    with owner_engine.begin() as conn:
        tables = ("notification_delivery", "webhook", "sweeper_heartbeat", "monitored_system")
        conn.execute(text(f"TRUNCATE {', '.join(with_phase4_tables(conn, tables))}"))
    monkeypatch.setenv("ENVIRONMENT", environment)
    with Session(owner_engine) as session:
        with pytest.raises(RuntimeError):
            seed(session)
        session.rollback()
    for name in ("monitored_system", "threshold_version", "api_key", "ledger_entry"):
        assert rows(owner_engine, f"SELECT 1 FROM {name}") == [], name
