"""Read helpers for the audit trail and the export. Each is one ordered SELECT, no lock."""

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

ENTRY_FIELDS = (
    "seq",
    "entry_type",
    "actor_id",
    "subject_id",
    "payload",
    "recorded_at",
    "prev_hash",
    "hash",
)


def audit_trail(session: Session, subject_id: str) -> list[dict[str, Any]]:
    """{actor_id, action, timestamp} for every entry about subject_id, in ascending seq."""
    rows = session.execute(
        text(
            "SELECT actor_id, entry_type, recorded_at FROM ledger_entry"
            " WHERE subject_id = :subject_id ORDER BY seq"
        ),
        {"subject_id": subject_id},
    )
    return [
        {"actor_id": row.actor_id, "action": row.entry_type, "timestamp": row.recorded_at}
        for row in rows
    ]


def all_entries(session: Session) -> list[dict[str, Any]]:
    """Every entry with its eight fields, in ascending seq."""
    rows = session.execute(text(f"SELECT {', '.join(ENTRY_FIELDS)} FROM ledger_entry ORDER BY seq"))
    return [dict(row._mapping) for row in rows]
