"""audit.record_view (REQ-14): Part 1's event-detail endpoint calls it in its transaction."""

from sqlalchemy.orm import Session

from app import ledger
from app.auth import Principal


def record_view(session: Session, principal: Principal, event_id: str) -> None:
    """Append EVENT_VIEWED for this principal and event to the caller's transaction."""
    ledger.append(session, "EVENT_VIEWED", principal.actor_id, event_id, {})
