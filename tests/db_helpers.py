"""Raw database helpers for schema and tamper tests."""

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, text

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
