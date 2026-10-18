"""Atomic ingestion and threshold detection. The caller owns commit/rollback."""

import json
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, events, gaps, ledger, notifications
from app.auth import Principal
from app.errors import ApiError, not_found
from app.formats import api_time, new_id


class MetricIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    system_id: str = Field(min_length=1, max_length=256)
    metric_name: str = Field(min_length=1, max_length=256)
    metric_type: Literal["performance", "drift"]
    value: Decimal = Field(allow_inf_nan=False, max_digits=100, decimal_places=50)
    timestamp: datetime

    @field_validator("system_id", "metric_name")
    @classmethod
    def valid_text(cls, value: str) -> str:
        if not value.strip() or "\x00" in value or any(0xD800 <= ord(c) <= 0xDFFF for c in value):
            raise ValueError("invalid text")
        return value

    @field_validator("value", mode="before")
    @classmethod
    def numeric_value(cls, value: Any) -> Any:
        if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
            raise ValueError("value must be a JSON number")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def string_timestamp(cls, value: Any) -> Any:
        if not isinstance(value, str) or "T" not in value:
            raise ValueError("timestamp must be an ISO-8601 string")
        return value

    @field_validator("timestamp")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("timestamp must be UTC")
        return value


def ingest(session: Session, principal: Principal, sample: MetricIn) -> dict[str, str]:
    if principal.system_id != sample.system_id:
        raise ApiError(403, "SYSTEM_MISMATCH", "the key does not match system_id")
    params = sample.model_dump()
    system = session.execute(
        text("SELECT system_id FROM monitored_system WHERE system_id = :system_id FOR UPDATE"),
        params,
    ).one_or_none()
    if system is None:
        raise not_found()
    existing = session.execute(
        text(
            "SELECT sample_id, value, event_id FROM metric_sample"
            " WHERE system_id = :system_id AND metric_name = :metric_name"
            " AND metric_type = :metric_type AND timestamp = :timestamp"
        ),
        params,
    ).one_or_none()
    if existing is not None:
        if existing.value != sample.value:
            raise ApiError(409, "DUPLICATE_CONFLICT", "the sample identity has a different value")
        return sample_response(existing.sample_id, existing.event_id)

    threshold = session.execute(
        text(
            "SELECT threshold_version_id, threshold_value, comparator FROM threshold_version"
            " WHERE system_id = :system_id AND metric_name = :metric_name"
            " AND metric_type = :metric_type AND effective_at <= :timestamp"
            " ORDER BY effective_at DESC LIMIT 1"
        ),
        params,
    ).one_or_none()
    if threshold is None:
        raise ApiError(422, "THRESHOLD_NOT_CONFIGURED", "no threshold applies to this sample")
    latest = session.execute(
        text(
            "SELECT max(timestamp) FROM metric_sample WHERE system_id = :system_id"
            " AND metric_name = :metric_name AND metric_type = :metric_type"
        ),
        params,
    ).scalar_one()
    now = clock.now()
    sample_id = new_id("smp")
    params.update(
        sample_id=sample_id, received_at=now, threshold_version_id=threshold.threshold_version_id
    )
    session.execute(
        text(
            "INSERT INTO metric_sample (sample_id, system_id, metric_name, metric_type, value,"
            " timestamp, received_at, threshold_version_id) VALUES (:sample_id, :system_id,"
            " :metric_name, :metric_type, :value, :timestamp, :received_at, :threshold_version_id)"
        ),
        params,
    )
    # Receipt time, not the sender's timestamp, drives monitoring continuity.
    session.execute(
        text(
            "UPDATE monitored_system SET last_received_at = :received_at"
            " WHERE system_id = :system_id"
        ),
        params,
    )
    gaps.close_if_open(session, sample.system_id)
    breached = (
        sample.value > threshold.threshold_value
        if threshold.comparator == "gt"
        else sample.value < threshold.threshold_value
    )
    series = session.execute(
        text(
            "SELECT event_id FROM event WHERE system_id = :system_id"
            " AND metric_name = :metric_name AND metric_type = :metric_type"
            " AND series_open FOR UPDATE"
        ),
        params,
    ).scalar_one_or_none()
    if not breached:
        # Historical healthy data cannot end a current breach series.
        if series is not None and (latest is None or sample.timestamp > latest):
            session.execute(
                text("UPDATE event SET series_open = false WHERE event_id = :id"), {"id": series}
            )
        return sample_response(sample_id, None)

    event_id = series or new_id("evt")
    if series is None:
        evidence = evidence_window(session, sample)
        description = (
            f"System {sample.system_id}: {sample.metric_name} ({sample.metric_type})"
            f" measured {format(sample.value, 'f')} at {api_time(sample.timestamp)};"
            f" threshold {threshold.comparator} {threshold.threshold_value},"
            f" version {threshold.threshold_version_id}. Human assessment required."
        )
        harm = {
            name: {
                "status": "PREFILLED_PENDING_CONFIRMATION",
                "evidence": description,
                "confirmed_by": None,
                "confirmed_at": None,
            }
            for name in events.HARM_FIELDS
        }
        session.execute(
            text(
                "INSERT INTO event (event_id, type, system_id, state, detected_at, metric_name,"
                " metric_type, observed_value, threshold_value, threshold_version_id, harm_fields,"
                " series_open, evidence_bundle) VALUES (:event_id, 'threshold_breach',"
                " :system_id, 'FLAGGED', :received_at, :metric_name, :metric_type, :observed,"
                " :threshold, :threshold_version_id, CAST(:harm AS jsonb), true,"
                " CAST(:evidence AS jsonb))"
            ),
            {
                **params,
                "event_id": event_id,
                "observed": format(sample.value, "f"),
                "threshold": format(threshold.threshold_value, "f"),
                "harm": json.dumps(harm),
                "evidence": json.dumps(evidence),
            },
        )
    # Every breaching sample is a durable occurrence via this link.
    session.execute(
        text("UPDATE metric_sample SET event_id = :event_id WHERE sample_id = :id"),
        {"event_id": event_id, "id": sample_id},
    )
    if series is None:
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
                "threshold_value": format(threshold.threshold_value, "f"),
                "threshold_version_id": threshold.threshold_version_id,
            },
        )
        notifications.enqueue(session, event_id, principal.actor_id)
    return sample_response(sample_id, event_id)


def sample_response(sample_id: str, event_id: str | None) -> dict[str, str]:
    body = {"sample_id": sample_id}
    if event_id is not None:
        body["flagged_event_id"] = event_id
    return body


def evidence_window(session: Session, sample: MetricIn) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT sample_id, metric_name, metric_type, value, timestamp, threshold_version_id"
            " FROM metric_sample WHERE system_id = :system_id"
            " AND timestamp BETWEEN :start AND :end ORDER BY timestamp, sample_id"
        ),
        {
            "system_id": sample.system_id,
            "start": sample.timestamp - timedelta(hours=1),
            "end": sample.timestamp,
        },
    )
    return [
        {
            **dict(row._mapping),
            "value": format(row.value, "f"),
            "timestamp": api_time(row.timestamp),
        }
        for row in rows
    ]
