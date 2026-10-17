"""Event lookup and gap writes shared with the worker. Callers own transactions."""

from datetime import datetime

from sqlalchemy import text
from sqlalchemy.orm import Session

GAP_EVENT_TYPE = "monitoring_gap"

# The four Article 3(49) severity fields (REQ-07). A gap event carries all four (API contract).
HARM_FIELDS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)


def exists(session: Session, event_id: str) -> bool:
    return bool(
        session.execute(
            text("SELECT EXISTS (SELECT 1 FROM event WHERE event_id = :event_id)"),
            {"event_id": event_id},
        ).scalar_one()
    )


def create_gap_event(
    session: Session, event_id: str, system_id: str, detected_at: datetime
) -> None:
    """Insert a FLAGGED monitoring_gap event. Metric fields are null; an outage carries no
    harm evidence, so each harm field is present and null."""
    session.execute(
        text(
            "INSERT INTO event (event_id, type, system_id, state, detected_at, harm_fields)"
            " VALUES (:event_id, :type, :system_id, 'FLAGGED', :detected_at,"
            " CAST(:harm_fields AS jsonb))"
        ),
        {
            "event_id": event_id,
            "type": GAP_EVENT_TYPE,
            "system_id": system_id,
            "detected_at": detected_at,
            "harm_fields": "{" + ", ".join(f'"{name}": null' for name in HARM_FIELDS) + "}",
        },
    )


def close_gap_event(session: Session, event_id: str, closed_at: datetime) -> None:
    """Move a monitoring_gap event to CLOSED when its system's data resumes."""
    session.execute(
        text("UPDATE event SET state = 'CLOSED', closed_at = :closed_at WHERE event_id = :id"),
        {"id": event_id, "closed_at": closed_at},
    )
