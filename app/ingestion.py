"""Ingestion and threshold detection: POST /v1/metrics, spec 1.4 steps 2 to 11. The caller
owns commit and rollback; nothing here commits.

Lock order (spec 7): the monitored_system row first (step 4), then the open breach_series row,
then the ledger lock inside ledger.append. The open series row is locked before
gaps.close_if_open, which can append GAP_CLOSED, so no event or series row is locked after the
ledger lock is held (decision 122). Every write of one ingest commits together, or none does.
No network call happens here.
"""

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, events, evidence, gaps, ledger, notifications, observability
from app.auth import Principal
from app.errors import ApiError
from app.formats import new_id, retain_until

detection_logger = logging.getLogger("api.detection")


class MetricIn(BaseModel):
    """The request body (spec 1.4). All five fields are required; unknown extra fields are
    ignored. Numbers arrive as Decimal (app/api/ingestion_routes.py parses the JSON)."""

    model_config = ConfigDict(extra="ignore")

    system_id: str
    metric_name: str
    metric_type: Literal["performance", "drift"]
    # Finite, and exactly storable as NUMERIC (decision 121).
    value: Decimal = Field(allow_inf_nan=False, max_digits=100, decimal_places=50)
    timestamp: datetime

    @field_validator("system_id", "metric_name", mode="before")
    @classmethod
    def storable_text(cls, value: Any) -> Any:
        # PostgreSQL text cannot hold U+0000, and a lone surrogate has no UTF-8 form.
        if isinstance(value, str) and (
            "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value)
        ):
            raise ValueError("text cannot be stored")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def json_number(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, int | Decimal):
            raise ValueError("value must be a JSON number")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def iso_string(cls, value: Any) -> Any:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError("timestamp must be an ISO-8601 string")
        return value

    @field_validator("timestamp")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be UTC")
        return value


@dataclass(frozen=True)
class IngestResult:
    status: int  # 201 for a stored sample, 200 for a duplicate
    sample_id: str
    flagged_event_id: str | None
    outcome: str  # for the request log: duplicate, stored, healthy, series_join or breach

    def body(self) -> dict[str, str]:
        body = {"sample_id": self.sample_id}
        if self.flagged_event_id is not None:
            body["flagged_event_id"] = self.flagged_event_id
        return body


def ingest(session: Session, principal: Principal, sample: MetricIn) -> IngestResult:
    # Step 3: after authentication and body validation.
    if principal.system_id != sample.system_id:
        raise ApiError(403, "SYSTEM_MISMATCH", "the key does not match system_id")
    keys = {"system_id": sample.system_id, "metric_name": sample.metric_name}

    # Step 4: lock the system row. A valid SYSTEM key for an unknown system cannot happen.
    locked = session.execute(
        text("SELECT system_id FROM monitored_system WHERE system_id = :system_id FOR UPDATE"),
        keys,
    ).one_or_none()
    if locked is None:
        raise RuntimeError("the key's system has no monitored_system row")

    # Step 5: insert-or-ignore on (system_id, metric_name, timestamp).
    now = clock.now()
    sample_id = session.execute(
        text(
            "INSERT INTO metric_sample (sample_id, system_id, metric_name, metric_type, value,"
            " timestamp, received_at) VALUES (:sample_id, :system_id, :metric_name,"
            " :metric_type, :value, :timestamp, :now)"
            " ON CONFLICT (system_id, metric_name, timestamp) DO NOTHING RETURNING sample_id"
        ),
        {
            **keys,
            "sample_id": new_id("smp"),
            "metric_type": sample.metric_type,
            "value": sample.value,
            "timestamp": sample.timestamp,
            "now": now,
        },
    ).scalar_one_or_none()
    if sample_id is None:
        original = session.execute(
            text(
                "SELECT sample_id, event_id FROM metric_sample WHERE system_id = :system_id"
                " AND metric_name = :metric_name AND timestamp = :timestamp"
            ),
            {**keys, "timestamp": sample.timestamp},
        ).one()
        # A duplicate still shows the system is alive; nothing else happens (decision 116).
        _set_last_received_at(session, sample.system_id, now)
        observability.count(observability.DUPLICATES)
        return IngestResult(200, original.sample_id, original.event_id, "duplicate")
    observability.count(observability.SAMPLES_ACCEPTED)

    series = session.execute(
        text(
            "SELECT series_id, event_id FROM breach_series WHERE system_id = :system_id"
            " AND metric_name = :metric_name AND closed_at IS NULL FOR UPDATE"
        ),
        keys,
    ).one_or_none()

    # Step 6.
    _set_last_received_at(session, sample.system_id, now)
    gaps.close_if_open(session, sample.system_id)

    # Step 7: the newest version for (system_id, metric_name).
    threshold = session.execute(
        text(
            "SELECT threshold_version_id, comparator, limit_value FROM threshold_version"
            " WHERE system_id = :system_id AND metric_name = :metric_name"
            " ORDER BY version DESC LIMIT 1"
        ),
        keys,
    ).one_or_none()
    if threshold is None:
        return IngestResult(201, sample_id, None, "stored")

    # Step 8: strictly past the limit, in Decimal.
    limit: Decimal = threshold.limit_value
    breached = sample.value > limit if threshold.comparator == "gt" else sample.value < limit
    detection_logger.debug(
        "threshold evaluation: sample_id=%s system_id=%s metric_name=%s"
        " threshold_version_id=%s comparator=%s limit=%s value=%s breached=%s",
        sample_id,
        sample.system_id,
        sample.metric_name,
        threshold.threshold_version_id,
        threshold.comparator,
        format(limit, "f"),
        format(sample.value, "f"),
        breached,
    )

    if not breached:
        # Step 9: a healthy sample ends the open series.
        if series is not None:
            session.execute(
                text("UPDATE breach_series SET closed_at = :now WHERE series_id = :series_id"),
                {"now": now, "series_id": series.series_id},
            )
        return IngestResult(201, sample_id, None, "healthy")

    if series is not None:
        # Step 10: join the open series. No new event, notification or ledger entry.
        session.execute(
            text(
                "UPDATE breach_series SET occurrence_count = occurrence_count + 1"
                " WHERE series_id = :series_id"
            ),
            {"series_id": series.series_id},
        )
        _link_sample(session, sample_id, series.event_id)
        observability.count(observability.SERIES_JOINS)
        return IngestResult(201, sample_id, series.event_id, "series_join")

    # Step 11: a new event (spec 1.5), its harm fields and series, then the ledger.
    event_id = new_id("evt")
    bundle = evidence.bundle(session, sample.system_id, sample.metric_name, sample.timestamp)
    session.execute(
        text(
            "INSERT INTO event (event_id, system_id, type, metric_name, observed_value,"
            " threshold_value, threshold_version_id, detected_at, received_at, state,"
            " evidence_bundle, evidence_sha256, retain_until) VALUES (:event_id, :system_id,"
            " 'threshold_breach', :metric_name, :observed, :limit, :threshold_version_id,"
            " :detected_at, :now, 'FLAGGED', CAST(:bundle AS jsonb), :sha256, :retain_until)"
        ),
        {
            **keys,
            "event_id": event_id,
            "observed": sample.value,
            "limit": limit,
            "threshold_version_id": threshold.threshold_version_id,
            "detected_at": sample.timestamp,
            "now": now,
            "bundle": json.dumps(bundle, ensure_ascii=False),
            "sha256": evidence.sha256_of(bundle),
            "retain_until": retain_until(sample.timestamp),
        },
    )
    session.execute(
        text(
            "INSERT INTO harm_field (event_id, field_key, status)"
            " VALUES (:event_id, :field_key, 'PREFILLED_PENDING_CONFIRMATION')"
        ),
        [{"event_id": event_id, "field_key": key} for key in events.HARM_FIELDS],
    )
    session.execute(
        text(
            "INSERT INTO breach_series (series_id, system_id, metric_name, event_id, opened_at,"
            " occurrence_count) VALUES (:series_id, :system_id, :metric_name, :event_id, :now, 1)"
        ),
        {**keys, "series_id": new_id("ser"), "event_id": event_id, "now": now},
    )
    _link_sample(session, sample_id, event_id)
    ledger.append(
        session,
        "EVENT_FLAGGED",
        principal.actor_id,
        event_id,
        {
            "event_id": event_id,
            "system_id": sample.system_id,
            "metric_name": sample.metric_name,
            "observed_value": format(sample.value, "f"),
            "threshold_value": format(limit, "f"),
            "threshold_version_id": threshold.threshold_version_id,
        },
    )
    notifications.enqueue(session, event_id)
    observability.count(observability.BREACHES)
    return IngestResult(201, sample_id, event_id, "breach")


def _set_last_received_at(session: Session, system_id: str, now: datetime) -> None:
    # Server receive time, never the sender's timestamp (spec 3.4).
    session.execute(
        text("UPDATE monitored_system SET last_received_at = :now WHERE system_id = :system_id"),
        {"now": now, "system_id": system_id},
    )


def _link_sample(session: Session, sample_id: str, event_id: str) -> None:
    session.execute(
        text("UPDATE metric_sample SET event_id = :event_id WHERE sample_id = :sample_id"),
        {"event_id": event_id, "sample_id": sample_id},
    )
