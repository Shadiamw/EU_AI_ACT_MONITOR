"""Schema for Part 3 and the stub tables it uses (spec 1.7, 3.7, 4.4).

Expected names and types are copied from the spec's tables: 1.7 (event, harm_field), 3.7
(webhook, notification, notification_delivery, sweeper_heartbeat) and 4.4 (monitored_system,
migration order, roles).
"""

from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import IntegrityError

from tests.worker.conftest import T0, add_event, rows

TIMESTAMPTZ = "timestamp with time zone"

EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    # spec 4.4 (Part 4, the 0001 stub)
    "monitored_system": {
        "system_id": "text",
        "sampling_interval_s": "integer",
        "gap_timeout_intervals": "integer",
        "last_received_at": TIMESTAMPTZ,
    },
    # spec 3.7: last_ran_at; `singleton` keeps it to one row (decision 95)
    "sweeper_heartbeat": {"singleton": "boolean", "last_ran_at": TIMESTAMPTZ},
    # spec 1.7 (Part 1, the 0003 stub)
    "event": {
        "event_id": "text",
        "system_id": "text",
        "type": "text",
        "metric_name": "text",
        "observed_value": "numeric",
        "threshold_value": "numeric",
        "threshold_version_id": "text",
        "detected_at": TIMESTAMPTZ,
        "received_at": TIMESTAMPTZ,
        "state": "text",
        "deadline_utc": TIMESTAMPTZ,
        "evidence_bundle": "jsonb",
        "evidence_sha256": "text",
        "retain_until": TIMESTAMPTZ,
    },
    "harm_field": {
        "event_id": "text",
        "field_key": "text",
        "status": "text",
        "evidence": "text",
        "confirmed_by": "text",
        "confirmed_at": TIMESTAMPTZ,
    },
    # spec 3.7 (Part 3, migration 0004)
    "webhook": {
        "webhook_id": "text",
        "url": "text",
        "event_types": "ARRAY",
        "created_at": TIMESTAMPTZ,
    },
    "notification": {
        "notification_id": "text",
        "event_id": "text",
        "notification_type": "text",
        "created_at": TIMESTAMPTZ,
    },
    "notification_delivery": {
        "notification_id": "text",
        "webhook_id": "text",
        "status": "text",
        "attempt_count": "integer",
        "next_attempt_at": TIMESTAMPTZ,
        "lease_until": TIMESTAMPTZ,
        "delivered_at": TIMESTAMPTZ,
        "last_status": "text",
    },
}

NON_LEDGER_TABLES = sorted(EXPECTED_COLUMNS)


def columns(owner_engine: Engine, table: str) -> dict[str, str]:
    return {
        r["column_name"]: r["data_type"]
        for r in rows(
            owner_engine,
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = 'public' AND table_name = :t",
            t=table,
        )
    }


@pytest.mark.parametrize("table", NON_LEDGER_TABLES)
def test_stub_and_part3_tables_have_the_spec_columns(owner_engine: Engine, table: str) -> None:
    assert columns(owner_engine, table) == EXPECTED_COLUMNS[table]


def test_the_schema_has_exactly_the_ledger_stub_and_part3_tables(owner_engine: Engine) -> None:
    tables = [
        r["table_name"]
        for r in rows(
            owner_engine,
            "SELECT table_name FROM information_schema.tables WHERE table_schema = 'public'"
            " ORDER BY table_name",
        )
    ]
    # No monitoring_gap (plan C4), no metric_sample or breach_series (Part 1's, not needed).
    assert tables == sorted([*NON_LEDGER_TABLES, "ledger_entry", "alembic_version"])


def test_migration_head_is_0004(owner_engine: Engine) -> None:
    """Spec 4.4: 0001 Platform, 0002 Ledger, 0003 Detection, 0004 Notifications."""
    [row] = rows(owner_engine, "SELECT version_num FROM alembic_version")
    assert row == {"version_num": "0004"}


def index_definitions(owner_engine: Engine, table: str) -> dict[str, str]:
    return {
        r["indexname"]: r["indexdef"]
        for r in rows(
            owner_engine,
            "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = :t",
            t=table,
        )
    }


def test_event_indexes_and_state_check(owner_engine: Engine) -> None:
    indexes = index_definitions(owner_engine, "event")
    assert "(detected_at DESC, event_id)" in indexes["ix_event_detected_at_event_id"]
    open_gap = indexes["uq_event_one_open_gap_per_system"]
    assert "UNIQUE INDEX" in open_gap
    assert "(system_id)" in open_gap
    assert "type = 'monitoring_gap'::text" in open_gap
    assert "state = 'FLAGGED'::text" in open_gap

    insert = (
        "INSERT INTO event (event_id, system_id, type, detected_at, received_at, state,"
        " retain_until) VALUES (:id, 'sys-42', 'threshold_breach', :t, :t, :state, :t)"
    )
    with owner_engine.begin() as conn:
        for i, state in enumerate(["FLAGGED", "UNDER_REVIEW", "DISPOSITIONED", "CLOSED"]):
            conn.execute(text(insert), {"id": f"evt-{i}", "t": T0, "state": state})
    with pytest.raises(IntegrityError, match="ck_event_state"), owner_engine.begin() as conn:
        conn.execute(text(insert), {"id": "evt-x", "t": T0, "state": "OPEN"})


def test_harm_field_key_and_status_check(owner_engine: Engine) -> None:
    insert = "INSERT INTO harm_field (event_id, field_key, status) VALUES ('evt-1', :key, :status)"
    with owner_engine.begin() as conn:
        for key, status in [
            ("health_safety_harm", "PREFILLED_PENDING_CONFIRMATION"),
            ("critical_infrastructure_disruption", "CONFIRMED"),
            ("fundamental_rights_infringement", "NOT_APPLICABLE"),
        ]:
            conn.execute(text(insert), {"key": key, "status": status})
    with pytest.raises(IntegrityError, match="harm_field_pkey"), owner_engine.begin() as conn:
        conn.execute(text(insert), {"key": "health_safety_harm", "status": "CONFIRMED"})
    with pytest.raises(IntegrityError, match="ck_harm_field_status"), owner_engine.begin() as conn:
        conn.execute(text(insert), {"key": "property_environmental_damage", "status": "MAYBE"})


def test_notification_references_the_event_once(owner_engine: Engine) -> None:
    """Spec 3.7: one notification row per event, with a foreign key to event."""
    insert = (
        "INSERT INTO notification (notification_id, event_id, notification_type, created_at)"
        " VALUES (:id, :event_id, 'flag_notification', :t)"
    )
    with pytest.raises(IntegrityError, match="fk_notification_event"), owner_engine.begin() as c:
        c.execute(text(insert), {"id": "ntf-1", "event_id": "evt-missing", "t": T0})
    add_event(owner_engine, "evt-1")
    with owner_engine.begin() as conn:
        conn.execute(text(insert), {"id": "ntf-1", "event_id": "evt-1", "t": T0})
    with pytest.raises(IntegrityError, match="uq_notification_event"), owner_engine.begin() as c:
        c.execute(text(insert), {"id": "ntf-2", "event_id": "evt-1", "t": T0})


def test_delivery_status_check_and_due_index(owner_engine: Engine) -> None:
    indexes = index_definitions(owner_engine, "notification_delivery")
    assert "(status, next_attempt_at)" in indexes["ix_notification_delivery_due"]
    [check] = rows(
        owner_engine,
        "SELECT pg_get_constraintdef(oid) AS def FROM pg_constraint"
        " WHERE conname = 'ck_notification_delivery_status'",
    )
    for status in ("PENDING", "DELIVERED", "FAILED"):
        assert f"'{status}'" in check["def"]
    assert "IN_FLIGHT" not in check["def"]


def test_sweeper_heartbeat_holds_one_row(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("INSERT INTO sweeper_heartbeat (last_ran_at) VALUES (:t)"), {"t": T0})
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(text("INSERT INTO sweeper_heartbeat (last_ran_at) VALUES (:t)"), {"t": T0})
    with pytest.raises(IntegrityError), owner_engine.begin() as conn:
        conn.execute(
            text("INSERT INTO sweeper_heartbeat (singleton, last_ran_at) VALUES (false, :t)"),
            {"t": T0},
        )


@pytest.mark.parametrize("table", NON_LEDGER_TABLES)
def test_application_role_has_normal_read_write_outside_the_ledger(
    owner_engine: Engine, table: str
) -> None:
    """Spec 4.4: normal read/write on every table except the ledger; PUBLIC has nothing."""

    def privileges(grantee: str) -> list[Any]:
        return sorted(
            r["privilege_type"]
            for r in rows(
                owner_engine,
                "SELECT privilege_type FROM information_schema.role_table_grants"
                " WHERE table_name = :t AND grantee = :g",
                t=table,
                g=grantee,
            )
        )

    assert privileges("monitor_app") == ["DELETE", "INSERT", "SELECT", "UPDATE"]
    assert privileges("PUBLIC") == []
