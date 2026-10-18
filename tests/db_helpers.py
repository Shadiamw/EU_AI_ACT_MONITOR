"""Raw database helpers for schema and tamper tests."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta

from sqlalchemy import Connection, Engine, text

from app import config
from app.auth import key_digest

TRIGGER_NAME = "ledger_entry_block_update_delete"


def insert_raw_entry(conn: Connection, seq: int, payload: str = "{}") -> None:
    """Insert a row with well-formed placeholder hashes (not a real chain)."""
    conn.execute(
        text(
            "INSERT INTO ledger_entry"
            " (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash)"
            " VALUES (:seq, 'SEED', 'system', NULL, CAST(:payload AS jsonb),"
            " '2026-09-27T10:00:00.000Z', :prev_hash, :hash)"
        ),
        {
            "seq": seq,
            "payload": payload,
            "prev_hash": format(seq - 1, "064x"),
            "hash": format(seq, "064x"),
        },
    )


def test_keys() -> dict[str, tuple[str, str, str | None]]:
    """actor id -> (key, role, system_id), the three seeded keys from the environment (spec 5)."""
    return {
        "sys-42": (config.ingest_key_sys_42(), "SYSTEM", "sys-42"),
        "rev-1": (config.reviewer_key(), "REVIEWER", None),
        "viewer-1": (config.viewer_key(), "VIEWER", None),
    }


def install_keys(engine: Engine) -> None:
    """The api_key rows Part 4's seed writes: the SHA-256 digest of each seeded key."""
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM api_key"))
        for actor, (key, role, system_id) in test_keys().items():
            conn.execute(
                text(
                    "INSERT INTO api_key (key_hash, role, actor_id, system_id)"
                    " VALUES (:key_hash, :role, :actor, :system_id)"
                ),
                {"key_hash": key_digest(key), "role": role, "actor": actor, "system_id": system_id},
            )


def bearer(actor: str) -> dict[str, str]:
    """The Authorization header for one of the seeded actors."""
    return {"Authorization": f"Bearer {test_keys()[actor][0]}"}


HARM_FIELDS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)


def insert_event(
    conn: Connection, event_id: str, *, event_type: str, detected_at: datetime
) -> None:
    """An event row and its four harm_field rows, as Part 1's ingest (threshold_breach) or Part
    3's sweeper (monitoring_gap) writes them (spec 1.5, 3.4), for tests that build the ledger
    by hand. The breach values match the EVENT_FLAGGED payloads these tests append."""
    breach = event_type == "threshold_breach"
    conn.execute(
        text(
            "INSERT INTO event (event_id, system_id, type, metric_name, observed_value,"
            " threshold_value, threshold_version_id, detected_at, received_at, state,"
            " retain_until) VALUES (:event_id, 'sys-42', :type, :metric_name, :observed,"
            " :limit, :version, :detected_at, :detected_at, 'FLAGGED', :retain_until)"
        ),
        {
            "event_id": event_id,
            "type": event_type,
            "metric_name": "accuracy_drift" if breach else None,
            "observed": "0.34" if breach else None,
            "limit": "0.30" if breach else None,
            "version": "thr-1" if breach else None,
            "detected_at": detected_at,
            "retain_until": detected_at + timedelta(days=184),
        },
    )
    status = "PREFILLED_PENDING_CONFIRMATION" if breach else "NOT_APPLICABLE"
    conn.execute(
        text(
            "INSERT INTO harm_field (event_id, field_key, status)"
            " VALUES (:event_id, :field_key, :status)"
        ),
        [{"event_id": event_id, "field_key": key, "status": status} for key in HARM_FIELDS],
    )


@contextmanager
def trigger_disabled(owner_engine: Engine) -> Iterator[Connection]:
    """Addendum rule 1: disable the trigger as the owner, make the change, re-enable it.

    All three steps share one transaction, so the trigger is never left disabled and no
    other session ever sees it disabled.
    """
    with owner_engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE ledger_entry DISABLE TRIGGER {TRIGGER_NAME}"))
        yield conn
        conn.execute(text(f"ALTER TABLE ledger_entry ENABLE TRIGGER {TRIGGER_NAME}"))
