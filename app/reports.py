"""Reports (Phase 5 Part 1, spec 3): assembly, storage, submission and listing.

- assemble_or_get(session, principal, event_id): the reviewer's GET (D3). The first call after
  the disposition now in force assembles the report in the caller's transaction: it locks the
  event row, builds the report dict, renders the JSON and the PDF once, stores both byte strings
  (through personal_data.seal_bytes) with their plaintext SHA-256 hashes, and appends
  REPORT_ASSEMBLED last. Every later call serves the stored bytes and writes nothing (D19). A
  revision adds a disposition row, so the next call assembles a new version bound to it; earlier
  versions stay stored and unchanged.
- parse_submission(raw) and submit(...): POST .../report/submission (D6). One REPORT_SUBMITTED
  entry; nothing on the event changes (D1).
- listing, get_by_id: the report rows and the stored bytes of one version.
- verify_stored(session): Part 3's nightly check. Every non-erased report's opened bytes must
  match the stored hashes and the REPORT_ASSEMBLED payload (INV-14).

The report status is derived when it is read and is never stored (spec 3.2), so the report
tables stay insert-only. Callers own transactions; nothing here commits. Logging never carries
report content.

Lock order (Phase 4 spec 7): the event row, then that event's child rows, then the ledger
lock. The ledger head the report records as ledger_anchor is read under the append lock
immediately before REPORT_ASSEMBLED is appended, so the lock is taken before rendering and held
to the commit (decision 258).
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock, deadlines, ledger, personal_data, report_render, report_template
from app.auth import Principal
from app.dispositions import malformed, summary
from app.errors import ApiError, not_found
from app.events import HARM_FIELDS
from app.events import detail as event_detail
from app.formats import api_time, new_id, whole_seconds
from app.ledger.canonical import GENESIS_PREV_HASH, JsonValue, Payload
from app.ledger.service import ADVISORY_LOCK_KEY

SCHEMA_VERSION = "pod-h-report-1"
SERIOUS_INCIDENT = "SERIOUS_INCIDENT"
NOT_SERIOUS_CLOSURE = "NOT_SERIOUS_CLOSURE"

# The REQ-08b label of each tier (spec 3.3.2, determination.tier_label).
TIER_LABELS = {
    "DEATH": "Death",
    "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": (
        "Critical infrastructure disruption / widespread infringement"
    ),
    "OTHER_SERIOUS": "Other serious incident",
    "NOT_SERIOUS": "Assessed — not a serious incident",
}

# Template 1.2 e: the four types a reviewer may record. "Final (Non-reportable incident)" is
# refused (C21, Q-14).
REPORT_TYPES = (
    "Initial",
    "Follow up",
    "Combined initial and final",
    "Final (Reportable incident)",
)
_TYPES_WITH_NEXT_DATE = ("Initial", "Follow up")

RECORD_ERASED_MESSAGE = "the personal data of this record has been erased"
NO_DISPOSITION_MESSAGE = "no disposition has been recorded for this event"

_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_SUBMISSION_KEYS = frozenset(
    {"report_id", "json_sha256", "report_type", "expected_next_report_date", "authority_reference"}
)
AUTHORITY_REFERENCE_MAX_LENGTH = 200


@dataclass(frozen=True)
class StoredReport:
    """A stored report with its bytes opened (plaintext, as produced)."""

    report_id: str
    event_id: str
    report_version: int
    report_kind: str
    disposition_id: str
    json_sha256: str
    pdf_sha256: str
    json_bytes: bytes
    pdf_bytes: bytes
    assembled: bool  # True when this call assembled it


def erased() -> ApiError:
    return ApiError(410, "RECORD_ERASED", RECORD_ERASED_MESSAGE)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _disposition_rows(session: Session, event_id: str) -> list[Any]:
    return list(
        session.execute(
            text(
                "SELECT disposition_id, seq_in_event, tier, reviewer_id, assigned_at, calendar,"
                " deadline_utc FROM disposition WHERE event_id = :event_id ORDER BY seq_in_event"
            ),
            {"event_id": event_id},
        )
    )


def _open(session: Session, row: Any) -> StoredReport:
    json_bytes = personal_data.open_bytes(
        session, row.event_id, "report.json_bytes", row.json_bytes
    )
    pdf_bytes = personal_data.open_bytes(session, row.event_id, "report.pdf_bytes", row.pdf_bytes)
    if json_bytes is None or pdf_bytes is None:
        raise erased()
    return StoredReport(
        report_id=row.report_id,
        event_id=row.event_id,
        report_version=row.report_version,
        report_kind=row.report_kind,
        disposition_id=row.disposition_id,
        json_sha256=row.json_sha256,
        pdf_sha256=row.pdf_sha256,
        json_bytes=bytes(json_bytes),
        pdf_bytes=bytes(pdf_bytes),
        assembled=False,
    )


_REPORT_COLUMNS = (
    "report_id, event_id, report_version, disposition_id, report_kind, json_bytes, pdf_bytes,"
    " json_sha256, pdf_sha256"
)


def _for_disposition(session: Session, disposition_id: str) -> StoredReport | None:
    row = session.execute(
        text(f"SELECT {_REPORT_COLUMNS} FROM report WHERE disposition_id = :disposition_id"),
        {"disposition_id": disposition_id},
    ).one_or_none()
    return None if row is None else _open(session, row)


def get_by_id(session: Session, report_id: str) -> StoredReport | None:
    """The stored bytes of one version, or None for an unknown id. Raises 410 for an erased
    record. Never assembles and appends nothing."""
    row = session.execute(
        text(f"SELECT {_REPORT_COLUMNS} FROM report WHERE report_id = :report_id"),
        {"report_id": report_id},
    ).one_or_none()
    if row is None:
        return None
    if personal_data.is_erased(session, row.event_id):
        raise erased()
    return _open(session, row)


def assemble_or_get(session: Session, principal: Principal, event_id: str) -> StoredReport:
    """Steps 4 to 9 of spec 3.6. The route has already checked the key, the role and the query
    format. Raises 404, 410 and 409 NO_DISPOSITION before anything is written."""
    if not _check_event(session, event_id):
        raise not_found()
    if personal_data.is_erased(session, event_id):
        raise erased()
    rows = _disposition_rows(session, event_id)
    if not rows:
        raise ApiError(409, "NO_DISPOSITION", NO_DISPOSITION_MESSAGE)
    stored = _for_disposition(session, rows[-1].disposition_id)
    if stored is not None:
        return stored  # step 8: nothing written (D19)

    # Step 9. The same lock the disposition transaction takes, so assembly and a revision
    # serialize; everything is read again under it.
    session.execute(
        text("SELECT 1 FROM event WHERE event_id = :event_id FOR UPDATE"), {"event_id": event_id}
    )
    if personal_data.is_erased(session, event_id):
        raise erased()
    rows = _disposition_rows(session, event_id)
    stored = _for_disposition(session, rows[-1].disposition_id)
    if stored is not None:  # a concurrent request assembled it first
        return stored
    return _assemble(session, principal, event_id, rows)


def _check_event(session: Session, event_id: str) -> bool:
    return bool(
        session.execute(
            text("SELECT EXISTS (SELECT 1 FROM event WHERE event_id = :event_id)"),
            {"event_id": event_id},
        ).scalar_one()
    )


def build(
    session: Session, event_id: str, rows: list[Any], report_id: str, version: int, now: datetime
) -> dict[str, Any]:
    """The report dict of spec 3.3.1, in field order, for the disposition row in force."""
    detail = event_detail(session, event_id)
    if detail is None:
        raise not_found()
    evidence = session.execute(
        text("SELECT evidence_bundle, evidence_sha256 FROM event WHERE event_id = :event_id"),
        {"event_id": event_id},
    ).one()
    decision = summary(session, event_id)
    first, latest = rows[0], rows[-1]
    tier = latest.tier
    closure = tier == deadlines.NOT_SERIOUS
    determined = api_time(first.assigned_at)
    detected = detail["detected_at"]
    harm = detail["harm_fields"]
    deadline = decision["deadline_utc"]
    serious = next((row for row in rows if row.tier != deadlines.NOT_SERIOUS), None)
    harm_status = {key: harm[key]["status"] for key in HARM_FIELDS}
    confirmed_evidence = [
        {"field": key, "evidence": harm[key]["evidence"]}
        for key in HARM_FIELDS
        if harm[key]["status"] == "CONFIRMED" and harm[key]["evidence"]
    ]
    anchor = _ledger_head(session)
    observed = detail["observed_value"]
    limit = detail["threshold_value"]
    description = {
        "what_went_wrong": (
            f"Metric {detail['metric_name']} on system {detail['system_id']} was observed at "
            f"{format(observed, 'f')} against a threshold of {format(limit, 'f')} at "
            f"{detected}."
        ),
        "effects": confirmed_evidence,
        "likely_causality": None,
    }
    fields = report_template.build(
        closure=closure,
        detected_at=detected,
        first_serious_assigned_at=None if serious is None else api_time(serious.assigned_at),
        candidates=report_template.classification_candidates(tier, harm_status),
        incident_description=description,
        rationale=decision["disposition"]["rationale"],
        provenance={
            "report_id": report_id,
            "report_version": version,
            "ledger_anchor": anchor,
        },
    )
    return {
        "report_id": report_id,
        "report_version": version,
        "report_kind": NOT_SERIOUS_CLOSURE if closure else SERIOUS_INCIDENT,
        "schema_version": SCHEMA_VERSION,
        "event": {
            "event_id": detail["event_id"],
            "system_id": detail["system_id"],
            "type": detail["type"],
            "metric_name": detail["metric_name"],
            "observed_value": observed,
            "threshold_value": limit,
            "detected_at": detected,
            "evidence_bundle": evidence.evidence_bundle,
            "evidence_sha256": evidence.evidence_sha256,
        },
        "harm_fields": harm,
        "determination": {
            "tier": tier,
            "tier_label": TIER_LABELS[tier],
            "rationale": decision["disposition"]["rationale"],
            "disposition_id": latest.disposition_id,
            "reviewer_id": decision["disposition"]["reviewer_id"],
            "assigned_at": decision["disposition"]["assigned_at"],
            "deadline_utc": deadline,
            "calendar": first.calendar,
            "revisions": decision["revisions"],
        },
        "timestamps": {
            "detected_at": detected,
            "determined_at": determined,
            "last_revised_at": api_time(latest.assigned_at) if len(rows) > 1 else None,
            "assembled_at": api_time(now),
        },
        "risk_indicator": {
            "detection_to_assignment_seconds": whole_seconds(
                _parse_time(detected), first.assigned_at
            )
        },
        "regulation": {
            "instrument": "Regulation (EU) 2024/1689",
            "reporting_article": "Article 73",
            "serious_incident_definition": "Article 3(49)",
            "reporting_window_days": deadlines.TIER_DAYS.get(tier),
        },
        "submission_marking": report_render.BANNER if closure else None,
        "article73_template": {
            "template": report_template.TEMPLATE_NAME,
            "template_version": report_template.TEMPLATE_VERSION,
            "template_reference": report_template.TEMPLATE_REFERENCE,
            "fields": fields,
        },
        "ledger_anchor": anchor,
    }


def _parse_time(moment: str) -> datetime:
    return datetime.fromisoformat(moment.replace("Z", "+00:00"))


def _ledger_head(session: Session) -> dict[str, Any]:
    """The ledger head, read under the append lock. The lock is held to the end of the
    caller's transaction, so no entry can slip in before REPORT_ASSEMBLED is appended."""
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
    head = session.execute(
        text("SELECT seq, hash FROM ledger_entry ORDER BY seq DESC LIMIT 1")
    ).one_or_none()
    if head is None:
        return {"seq": 0, "hash": GENESIS_PREV_HASH}
    return {"seq": head.seq, "hash": head.hash}


def _assemble(
    session: Session, principal: Principal, event_id: str, rows: list[Any]
) -> StoredReport:
    # After the disposition transaction has committed, so the three report timestamps are
    # distinct and ordered (spec 3.3.3, Q-3).
    now = clock.now()
    report_id = new_id("rpt")
    version = (
        session.execute(
            text("SELECT count(*) FROM report WHERE event_id = :event_id"), {"event_id": event_id}
        ).scalar_one()
        + 1
    )
    report = build(session, event_id, rows, report_id, version, now)
    json_bytes = report_render.render_json(report)
    json_sha256 = _sha256(json_bytes)
    pdf_bytes = report_render.render_pdf(report, json_sha256)
    pdf_sha256 = _sha256(pdf_bytes)
    latest = rows[-1]
    session.execute(
        text(
            "INSERT INTO report (report_id, event_id, report_version, disposition_id,"
            " report_kind, schema_version, json_bytes, pdf_bytes, json_sha256, pdf_sha256,"
            " assembled_at, assembled_by) VALUES (:report_id, :event_id, :version,"
            " :disposition_id, :kind, :schema_version, :json_bytes, :pdf_bytes, :json_sha256,"
            " :pdf_sha256, :assembled_at, :assembled_by)"
        ),
        {
            "report_id": report_id,
            "event_id": event_id,
            "version": version,
            "disposition_id": latest.disposition_id,
            "kind": report["report_kind"],
            "schema_version": SCHEMA_VERSION,
            "json_bytes": personal_data.seal_bytes(
                session, event_id, "report.json_bytes", json_bytes
            ),
            "pdf_bytes": personal_data.seal_bytes(session, event_id, "report.pdf_bytes", pdf_bytes),
            "json_sha256": json_sha256,
            "pdf_sha256": pdf_sha256,
            "assembled_at": now,
            "assembled_by": principal.actor_id,
        },
    )
    payload: Payload = {
        "event_id": event_id,
        "report_id": report_id,
        "report_version": version,
        "report_kind": report["report_kind"],
        "disposition_id": latest.disposition_id,
        "json_sha256": json_sha256,
        "pdf_sha256": pdf_sha256,
        "assembled_at": api_time(now),
        "ledger_anchor_seq": report["ledger_anchor"]["seq"],
    }
    ledger.append(session, "REPORT_ASSEMBLED", principal.actor_id, event_id, payload)
    return StoredReport(
        report_id=report_id,
        event_id=event_id,
        report_version=version,
        report_kind=report["report_kind"],
        disposition_id=latest.disposition_id,
        json_sha256=json_sha256,
        pdf_sha256=pdf_sha256,
        json_bytes=json_bytes,
        pdf_bytes=pdf_bytes,
        assembled=True,
    )


# Submission (spec 3.7).


@dataclass(frozen=True)
class SubmissionRequest:
    report_id: str
    json_sha256: str
    report_type: str
    expected_next_report_date: date | None
    authority_reference: str | None


def _bad_text(value: str) -> bool:
    return any(char == "\x00" or "\ud800" <= char <= "\udfff" for char in value)


def parse_submission(raw: Any) -> SubmissionRequest:
    """Step 3 of spec 3.7: 400 MALFORMED_BODY for a body that is not an object, an unknown or
    missing field, a field of the wrong type, a report_type outside the four template strings
    (including "Final (Non-reportable incident)"), a bad date, or expected_next_report_date with
    a final type. Pure."""
    if not isinstance(raw, dict) or not set(raw) <= _SUBMISSION_KEYS:
        raise malformed()
    report_id, digest, report_type = (
        raw.get("report_id"),
        raw.get("json_sha256"),
        raw.get("report_type"),
    )
    if not (isinstance(report_id, str) and isinstance(digest, str)):
        raise malformed()
    if not isinstance(report_type, str) or report_type not in REPORT_TYPES:
        raise malformed()
    if _bad_text(report_id) or _bad_text(digest) or not report_id or len(report_id) > 100:
        raise malformed()
    next_date: date | None = None
    given = raw.get("expected_next_report_date")
    if given is not None:
        if not isinstance(given, str) or _DATE.fullmatch(given) is None:
            raise malformed()
        try:
            next_date = date.fromisoformat(given)
        except ValueError:
            raise malformed() from None
        if report_type not in _TYPES_WITH_NEXT_DATE:
            raise malformed()
    reference = raw.get("authority_reference")
    if reference is not None and (
        not isinstance(reference, str)
        or len(reference) > AUTHORITY_REFERENCE_MAX_LENGTH
        or _bad_text(reference)
    ):
        raise malformed()
    return SubmissionRequest(report_id, digest, report_type, next_date, reference)


def submit(
    session: Session, principal: Principal, event_id: str, request: SubmissionRequest
) -> dict[str, Any]:
    """Steps 4 to 10 of spec 3.7 in the caller's transaction. A check that fails has written
    nothing."""
    locked = session.execute(
        text("SELECT 1 FROM event WHERE event_id = :event_id FOR UPDATE"), {"event_id": event_id}
    ).one_or_none()
    if locked is None:
        raise not_found()
    report = session.execute(
        text(
            "SELECT report_id, disposition_id, report_kind, json_sha256, pdf_sha256 FROM report"
            " WHERE report_id = :report_id AND event_id = :event_id"
        ),
        {"report_id": request.report_id, "event_id": event_id},
    ).one_or_none()
    if report is None:
        raise not_found()
    if personal_data.is_erased(session, event_id):
        raise erased()
    if report.report_kind == NOT_SERIOUS_CLOSURE:
        raise ApiError(409, "NOT_SUBMITTABLE", "a not-serious closure report cannot be submitted")
    rows = _disposition_rows(session, event_id)
    if not rows or rows[-1].disposition_id != report.disposition_id:
        raise ApiError(
            409, "REPORT_NOT_CURRENT", "the report was superseded by a later disposition"
        )
    if request.json_sha256 != report.json_sha256:
        raise ApiError(409, "REPORT_HASH_MISMATCH", "json_sha256 does not match the stored report")
    already = session.execute(
        text("SELECT EXISTS (SELECT 1 FROM report_submission WHERE report_id = :report_id)"),
        {"report_id": report.report_id},
    ).scalar_one()
    if already:
        raise ApiError(409, "REPORT_ALREADY_SUBMITTED", "this report has already been submitted")
    earlier = session.execute(
        text("SELECT EXISTS (SELECT 1 FROM report_submission WHERE event_id = :event_id)"),
        {"event_id": event_id},
    ).scalar_one()
    kind = "FOLLOW_UP" if earlier else "INITIAL"
    submission_id = new_id("sub")
    now = clock.now()
    session.execute(
        text(
            "INSERT INTO report_submission (submission_id, report_id, event_id, submitted_by,"
            " submitted_at, submission_kind, report_type, expected_next_report_date,"
            " authority_reference) VALUES (:submission_id, :report_id, :event_id, :submitted_by,"
            " :submitted_at, :kind, :report_type, :next_date, :reference)"
        ),
        {
            "submission_id": submission_id,
            "report_id": report.report_id,
            "event_id": event_id,
            "submitted_by": principal.actor_id,
            "submitted_at": now,
            "kind": kind,
            "report_type": request.report_type,
            "next_date": request.expected_next_report_date,
            "reference": request.authority_reference,
        },
    )
    payload: Payload = {
        "event_id": event_id,
        "report_id": report.report_id,
        "submission_id": submission_id,
        "submission_kind": kind,
        "report_type": request.report_type,
        "json_sha256": report.json_sha256,
        "pdf_sha256": report.pdf_sha256,
    }
    ledger.append(session, "REPORT_SUBMITTED", principal.actor_id, event_id, payload)
    return {
        "submission_id": submission_id,
        "report_id": report.report_id,
        "event_id": event_id,
        "submitted_by": principal.actor_id,
        "submitted_at": api_time(now),
        "submission_kind": kind,
        "report_type": request.report_type,
        "expected_next_report_date": (
            None
            if request.expected_next_report_date is None
            else request.expected_next_report_date.isoformat()
        ),
        "authority_reference": request.authority_reference,
    }


# Listing (spec 3.9).


def listing(session: Session, event_id: str) -> list[dict[str, Any]]:
    """The report rows of an event, oldest first, without content. 404 for an unknown event.
    Still lists the rows and hashes after erasure."""
    if not _check_event(session, event_id):
        raise not_found()
    rows = _disposition_rows(session, event_id)
    latest = rows[-1].disposition_id if rows else None
    result = session.execute(
        text(
            "SELECT r.report_id, r.report_version, r.report_kind, r.disposition_id,"
            " r.assembled_at, r.assembled_by, r.json_sha256, r.pdf_sha256,"
            " s.submission_id, s.submitted_by, s.submitted_at, s.submission_kind,"
            " s.report_type, s.expected_next_report_date, s.authority_reference"
            " FROM report r LEFT JOIN report_submission s ON s.report_id = r.report_id"
            " WHERE r.event_id = :event_id ORDER BY r.report_version"
        ),
        {"event_id": event_id},
    )
    out: list[dict[str, Any]] = []
    for row in result:
        current = row.disposition_id == latest
        submitted = row.submission_id is not None
        if submitted:
            status = "SUBMITTED" if current else "SUBMITTED_SUPERSEDED"
        else:
            status = "ASSEMBLED" if current else "SUPERSEDED"
        submission = None
        if submitted:
            submission = {
                "submission_id": row.submission_id,
                "submitted_by": row.submitted_by,
                "submitted_at": api_time(row.submitted_at),
                "submission_kind": row.submission_kind,
                "report_type": row.report_type,
                "expected_next_report_date": (
                    None
                    if row.expected_next_report_date is None
                    else row.expected_next_report_date.isoformat()
                ),
                "authority_reference": row.authority_reference,
            }
        out.append(
            {
                "report_id": row.report_id,
                "report_version": row.report_version,
                "report_kind": row.report_kind,
                "disposition_id": row.disposition_id,
                "assembled_at": api_time(row.assembled_at),
                "assembled_by": row.assembled_by,
                "json_sha256": row.json_sha256,
                "pdf_sha256": row.pdf_sha256,
                "status": status,
                "submission": submission,
            }
        )
    return out


# The nightly check (spec 6.1, INV-14).


def verify_stored(session: Session) -> list[dict[str, JsonValue]]:
    """Every non-erased report whose stored bytes no longer match: [{report_id, which}], where
    which is json_bytes, pdf_bytes or ledger_payload (no REPORT_ASSEMBLED entry for it, or one
    whose hashes differ). Empty when everything holds. Reads only; never writes values."""
    assembled: dict[str, tuple[str, str]] = {}
    for entry in session.execute(
        text(
            "SELECT payload->>'report_id' AS report_id, payload->>'json_sha256' AS j,"
            " payload->>'pdf_sha256' AS p FROM ledger_entry WHERE entry_type = 'REPORT_ASSEMBLED'"
        )
    ):
        assembled[entry.report_id] = (entry.j, entry.p)
    mismatches: list[dict[str, JsonValue]] = []
    rows = session.execute(
        text(f"SELECT {_REPORT_COLUMNS} FROM report ORDER BY event_id, report_version")
    ).all()
    for row in rows:
        if personal_data.is_erased(session, row.event_id):
            continue
        json_bytes = personal_data.open_bytes(
            session, row.event_id, "report.json_bytes", row.json_bytes
        )
        pdf_bytes = personal_data.open_bytes(
            session, row.event_id, "report.pdf_bytes", row.pdf_bytes
        )
        if json_bytes is None or _sha256(bytes(json_bytes)) != row.json_sha256:
            mismatches.append({"report_id": row.report_id, "which": "json_bytes"})
        if pdf_bytes is None or _sha256(bytes(pdf_bytes)) != row.pdf_sha256:
            mismatches.append({"report_id": row.report_id, "which": "pdf_bytes"})
        if assembled.get(row.report_id) != (row.json_sha256, row.pdf_sha256):
            mismatches.append({"report_id": row.report_id, "which": "ledger_payload"})
    return mismatches
