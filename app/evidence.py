"""The evidence bundle of a breach event (spec 1.5).

The bundle is a JSON snapshot of the triggering metric's samples in the 60 minutes up to and
including the triggering sample:

    {"window": {"start": "<sample timestamp - 60 min>", "end": "<sample timestamp>"},
     "samples": [{"metric_name": ..., "value": "<decimal string>", "timestamp": ...}, ...]}

Both window ends are inclusive. Samples are this system's and this metric's, oldest first, as
stored when the event is created; later arrivals are not added. Values are plain decimal
strings and timestamps use the API format, so the bundle holds no float or Decimal. That makes
it valid input for Part 2's canonical JSON (decision 56), and its stored jsonb form round-trips
exactly. evidence_sha256 is the lowercase hex SHA-256 of the UTF-8 canonical JSON (keys sorted
at every level, separators "," and ":", non-ASCII not escaped), so anyone can recompute it from
the stored bundle (decision 119).
"""

import hashlib
import json
from datetime import datetime, timedelta

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.formats import api_time
from app.ledger.canonical import Payload, validate_payload

WINDOW = timedelta(minutes=60)


def bundle(session: Session, system_id: str, metric_name: str, timestamp: datetime) -> Payload:
    """The evidence bundle for a breach whose triggering sample has this timestamp."""
    start = timestamp - WINDOW
    rows = session.execute(
        text(
            "SELECT metric_name, value, timestamp FROM metric_sample"
            " WHERE system_id = :system_id AND metric_name = :metric_name"
            " AND timestamp BETWEEN :start AND :end ORDER BY timestamp, sample_id"
        ),
        {"system_id": system_id, "metric_name": metric_name, "start": start, "end": timestamp},
    )
    return {
        "window": {"start": api_time(start), "end": api_time(timestamp)},
        "samples": [
            {
                "metric_name": row.metric_name,
                "value": format(row.value, "f"),
                "timestamp": api_time(row.timestamp),
            }
            for row in rows
        ],
    }


def sha256_of(evidence: Payload) -> str:
    """SHA-256 of the canonical JSON of a bundle. Raises LedgerError for a float or Decimal."""
    canonical = json.dumps(
        validate_payload(evidence),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
