"""Events (Part 1): lookup, the contract Event object and list summaries. Callers own
transactions.

Breach events are written by the ingest (app/ingestion.py) and gap events by Part 3's sweeper
(app/gaps.py); both live in the event and harm_field tables (spec 1.7) and are read the same
way here. The Event keeps the contract's shape and field order (Phase 4 spec 1.6):
disposition, revisions and deadline_utc come from dispositions.summary (Part 2), imported inside
detail; until Part 2 lands, its base-commit stub gives disposition null, revisions [] and
deadline_utc as stored. The harm fields are the rows as stored. prefill_evidence is never
exposed, because the HarmField object is fixed, and detected_at is only read here.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.formats import api_time

# The four Article 3(49) harm fields (REQ-07), in the contract's order.
HARM_FIELDS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)
STATES = ("FLAGGED", "UNDER_REVIEW", "DISPOSITIONED", "CLOSED")
TYPES = ("threshold_breach", "monitoring_gap")


def exists(session: Session, event_id: str) -> bool:
    return bool(
        session.execute(
            text("SELECT EXISTS (SELECT 1 FROM event WHERE event_id = :event_id)"),
            {"event_id": event_id},
        ).scalar_one()
    )


def _time(moment: datetime | None) -> str | None:
    return None if moment is None else api_time(moment)


def detail(session: Session, event_id: str) -> dict[str, Any] | None:
    """The contract Event object (Example 2 shape), or None for an unknown id. observed_value
    and threshold_value are the stored Decimals; app.jsonout writes them as exact numbers."""
    event = session.execute(
        text(
            "SELECT event_id, system_id, type, metric_name, observed_value, threshold_value,"
            " detected_at, state, deadline_utc FROM event WHERE event_id = :event_id"
        ),
        {"event_id": event_id},
    ).one_or_none()
    if event is None:
        return None
    harm_rows = {
        row.field_key: row
        for row in session.execute(
            text(
                "SELECT field_key, status, evidence, confirmed_by, confirmed_at FROM harm_field"
                " WHERE event_id = :event_id"
            ),
            {"event_id": event_id},
        )
    }
    if set(harm_rows) != set(HARM_FIELDS):
        raise RuntimeError("an event must have exactly the four harm fields")
    from app import dispositions  # inside the function: dispositions may import events

    summary = dispositions.summary(session, event_id)
    return {
        "event_id": event.event_id,
        "system_id": event.system_id,
        "type": event.type,
        "metric_name": event.metric_name,
        "observed_value": event.observed_value,
        "threshold_value": event.threshold_value,
        "detected_at": api_time(event.detected_at),
        "state": event.state,
        "disposition": summary["disposition"],
        "deadline_utc": summary["deadline_utc"],
        "revisions": summary["revisions"],
        "harm_fields": {
            key: {
                "status": harm_rows[key].status,
                "evidence": harm_rows[key].evidence,
                "confirmed_by": harm_rows[key].confirmed_by,
                "confirmed_at": _time(harm_rows[key].confirmed_at),
            }
            for key in HARM_FIELDS
        },
    }


def summaries(
    session: Session,
    *,
    system_id: str | None,
    state: str | None,
    event_type: str | None,
    since: datetime | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Event summaries (spec 1.6), newest first (detected_at descending, ties by event_id)."""
    filters = []
    params: dict[str, Any] = {"limit": limit, "offset": offset}
    for column, value in (("system_id", system_id), ("state", state), ("type", event_type)):
        if value is not None:
            filters.append(f"{column} = :{column}")
            params[column] = value
    if since is not None:
        filters.append("detected_at >= :since")
        params["since"] = since
    where = " WHERE " + " AND ".join(filters) if filters else ""
    rows = session.execute(
        text(
            "SELECT event_id, system_id, type, metric_name, detected_at, state FROM event"
            + where
            + " ORDER BY detected_at DESC, event_id LIMIT :limit OFFSET :offset"
        ),
        params,
    )
    return [
        {
            "event_id": row.event_id,
            "system_id": row.system_id,
            "type": row.type,
            "metric_name": row.metric_name,
            "detected_at": api_time(row.detected_at),
            "state": row.state,
        }
        for row in rows
    ]
