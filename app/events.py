"""Event lookup (Part 1). Callers own transactions."""

from sqlalchemy import text
from sqlalchemy.orm import Session


def exists(session: Session, event_id: str) -> bool:
    return bool(
        session.execute(
            text("SELECT EXISTS (SELECT 1 FROM event WHERE event_id = :event_id)"),
            {"event_id": event_id},
        ).scalar_one()
    )
