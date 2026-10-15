"""STUB for Part 1 (events). Only the existence check the audit endpoint needs (plan G1).

Until Part 1's event table exists, an event exists when the ledger holds an EVENT_FLAGGED or
GAP_OPENED entry whose subject_id is that id (addendum rule 1; docs/decisions.md 30). Part 1
replaces this with a lookup on the event table.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session


def exists(session: Session, event_id: str) -> bool:
    return bool(
        session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM ledger_entry WHERE subject_id = :event_id"
                " AND entry_type IN ('EVENT_FLAGGED', 'GAP_OPENED'))"
            ),
            {"event_id": event_id},
        ).scalar_one()
    )
