"""Part 1's tables: the constraints and indexes of spec 1.7, and the migration chain of spec 4.4
(decision 127). Column names and types are checked in tests/worker/test_schema.py."""

from datetime import timedelta
from typing import Any

import pytest
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.conftest import alembic_config
from tests.db_helpers import insert_event
from tests.part1.conftest import T0, rows


def sample(**changes: Any) -> dict[str, Any]:
    return {
        "sample_id": "smp-1",
        "system_id": "sys-42",
        "metric_name": "accuracy_drift",
        "metric_type": "drift",
        "value": "0.34",
        "timestamp": T0,
        "received_at": T0,
        **changes,
    }


INSERT_SAMPLE = (
    "INSERT INTO metric_sample (sample_id, system_id, metric_name, metric_type, value,"
    " timestamp, received_at) VALUES (:sample_id, :system_id, :metric_name, :metric_type,"
    " :value, :timestamp, :received_at)"
)
INSERT_SERIES = (
    "INSERT INTO breach_series (series_id, system_id, metric_name, event_id, opened_at,"
    " closed_at, occurrence_count) VALUES (:series_id, 'sys-42', 'accuracy_drift', 'evt-1',"
    " :at, :closed_at, :count)"
)


def test_one_sample_per_system_metric_and_timestamp(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text(INSERT_SAMPLE), sample())
        conn.execute(text(INSERT_SAMPLE), sample(sample_id="smp-2", metric_name="accuracy"))
        conn.execute(text(INSERT_SAMPLE), sample(sample_id="smp-3", timestamp=T0 + timedelta(1)))
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(text(INSERT_SAMPLE), sample(sample_id="smp-4", value="0.10"))


@pytest.mark.parametrize("metric_type", ["latency", "Drift", ""])
def test_metric_type_is_performance_or_drift(owner_engine: Engine, metric_type: str) -> None:
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(text(INSERT_SAMPLE), sample(metric_type=metric_type))


def test_at_most_one_open_series_per_metric(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text(INSERT_SERIES), {"series_id": "s1", "at": T0, "closed_at": T0, "count": 1}
        )
        conn.execute(
            text(INSERT_SERIES), {"series_id": "s2", "at": T0, "closed_at": None, "count": 1}
        )
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(
            text(INSERT_SERIES), {"series_id": "s3", "at": T0, "closed_at": None, "count": 1}
        )


def test_occurrence_count_starts_at_one(owner_engine: Engine) -> None:
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(
            text(INSERT_SERIES), {"series_id": "s1", "at": T0, "closed_at": None, "count": 0}
        )


@pytest.mark.parametrize("state", ["OPEN", "flagged", "REVIEWED"])
def test_event_state_is_one_of_the_contract_states(owner_engine: Engine, state: str) -> None:
    with owner_engine.begin() as conn:
        insert_event(conn, "evt-1", event_type="threshold_breach", detected_at=T0)
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(text("UPDATE event SET state = :s"), {"s": state})


def test_event_accepts_all_four_contract_states(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        insert_event(conn, "evt-1", event_type="threshold_breach", detected_at=T0)
        for state in ("UNDER_REVIEW", "DISPOSITIONED", "CLOSED", "FLAGGED"):
            conn.execute(text("UPDATE event SET state = :s"), {"s": state})


def test_one_harm_field_row_per_event_and_key(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        insert_event(conn, "evt-1", event_type="threshold_breach", detected_at=T0)
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO harm_field (event_id, field_key, status)"
                " VALUES ('evt-1', 'health_safety_harm', 'CONFIRMED')"
            )
        )


def test_event_list_index_is_detected_at_desc_then_event_id(owner_engine: Engine) -> None:
    [index] = rows(
        owner_engine,
        "SELECT indexdef FROM pg_indexes WHERE tablename = 'event'"
        " AND indexdef LIKE '%detected_at%'",
    )
    assert index["indexdef"].endswith("(detected_at DESC, event_id)")


def test_at_most_one_open_gap_per_system(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        insert_event(conn, "evt-1", event_type="monitoring_gap", detected_at=T0)
        insert_event(conn, "evt-2", event_type="threshold_breach", detected_at=T0)
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        insert_event(conn, "evt-3", event_type="monitoring_gap", detected_at=T0)


def test_alembic_chain_is_0001_to_0007_with_one_head() -> None:
    """Spec 4.4: platform, ledger, detection, notifications, then the Phase 4 review,
    dispositions and escalations (Phase 4 Addendum 9.1), in that order."""
    script = ScriptDirectory.from_config(alembic_config())
    assert script.get_heads() == ["0007"]
    chain = [revision.revision for revision in script.walk_revisions("base", "heads")]
    assert chain == ["0007", "0006", "0005", "0004", "0003", "0002", "0001"]
    assert [script.get_revision(r).down_revision for r in chain] == [
        "0006",
        "0005",
        "0004",
        "0003",
        "0002",
        "0001",
        None,
    ]


def test_database_is_at_head_0007(owner_engine: Engine) -> None:
    assert rows(owner_engine, "SELECT version_num FROM alembic_version") == [
        {"version_num": "0007"}
    ]
