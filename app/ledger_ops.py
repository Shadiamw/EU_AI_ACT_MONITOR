"""Phase 5 Part 3: ledger operations.

- project_events(entries): pure. Replays ledger entries, in seq order, into the values the
  event, disposition, harm_field and report rows should hold (spec 6.3, D13).
- projection_check(session): compares those values with the current rows and returns the
  mismatches, [{event_id, column, expected, actual}], empty when every row agrees (spec 6.1).

The chain covers ledger rows only, so this is how a direct edit to an event, disposition or
harm_field row is found. It compares exactly the columns of spec 6.3: timestamps as instants
(reported in the API form), decimals as exact strings. A gap event's system_id comes from
GAP_OPENED and its metric fields are expected null; harm fields are compared only once a
DISPOSITION_* entry carries them (decision 304). The report table belongs to Part 1; when it
is absent, the report comparison is skipped (decision 305).

Reads only, in the caller's session; never writes and never commits. Mismatches carry ids,
column names and the two values, nothing else.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.events import HARM_FIELDS
from app.formats import api_time

NOT_SERIOUS = "NOT_SERIOUS"

# The entry types the projection replays (spec 6.3). Every other entry type is skipped.
PROJECTED_TYPES = (
    "EVENT_FLAGGED",
    "GAP_OPENED",
    "GAP_CLOSED",
    "REVIEW_STARTED",
    "DISPOSITION_RECORDED",
    "DISPOSITION_REVISED",
    "REPORT_ASSEMBLED",
)

_METRIC_COLUMNS = ("metric_name", "observed_value", "threshold_value")
_DISPOSITION_COLUMNS = ("tier", "deadline_utc", "anchor_at", "calendar")
_TIME_COLUMNS = frozenset({"deadline_utc", "anchor_at"})

# Stands for an unparseable time string: equal to no row value.
_NEVER = datetime.min.replace(tzinfo=UTC)


@dataclass
class Projected:
    """What the ledger says one event's rows should hold. Times are the payloads' API strings,
    decimals the payloads' exact strings. origin is the entry type that created the event
    (EVENT_FLAGGED or GAP_OPENED), or None if the ledger has later entries but no origin."""

    origin: str | None = None
    state: str | None = None
    deadline_utc: str | None = None
    system_id: str | None = None
    metric_name: str | None = None
    observed_value: str | None = None
    threshold_value: str | None = None
    dispositions: list[dict[str, Any]] = field(default_factory=list)
    harm_fields: dict[str, Any] | None = None
    reports: list[dict[str, Any]] = field(default_factory=list)


def project_events(entries: Iterable[Mapping[str, Any]]) -> dict[str, Projected]:
    """Replay entries ({seq, entry_type, payload}) in seq order into one Projected per
    payload event_id. Pure: no database, no clock.

    state: EVENT_FLAGGED or GAP_OPENED gives FLAGGED, REVIEW_STARTED UNDER_REVIEW, a
    DISPOSITION_* entry DISPOSITIONED (CLOSED for NOT_SERIOUS), GAP_CLOSED CLOSED; the last one
    wins. deadline_utc and harm_fields come from the latest DISPOSITION_* payload."""
    events: dict[str, Projected] = {}
    for entry in sorted(entries, key=lambda item: int(item["seq"])):
        entry_type = entry["entry_type"]
        if entry_type not in PROJECTED_TYPES:
            continue
        payload: Mapping[str, Any] = entry["payload"]
        event = events.setdefault(str(payload["event_id"]), Projected())
        if entry_type == "EVENT_FLAGGED":
            event.origin, event.state = entry_type, "FLAGGED"
            event.system_id = payload["system_id"]
            event.metric_name = payload["metric_name"]
            event.observed_value = payload["observed_value"]
            event.threshold_value = payload["threshold_value"]
        elif entry_type == "GAP_OPENED":
            # Decision 304: a gap has no metric, so the three metric columns stay null.
            event.origin, event.state = entry_type, "FLAGGED"
            event.system_id = payload["system_id"]
        elif entry_type == "REVIEW_STARTED":
            event.state = "UNDER_REVIEW"
        elif entry_type == "GAP_CLOSED":
            event.state = "CLOSED"
        elif entry_type == "REPORT_ASSEMBLED":
            event.reports.append(
                {
                    "report_id": payload["report_id"],
                    "json_sha256": payload["json_sha256"],
                    "pdf_sha256": payload["pdf_sha256"],
                }
            )
        else:  # DISPOSITION_RECORDED or DISPOSITION_REVISED
            tier = payload["tier"] if entry_type == "DISPOSITION_RECORDED" else payload["new_tier"]
            event.state = "CLOSED" if tier == NOT_SERIOUS else "DISPOSITIONED"
            event.deadline_utc = payload["deadline_utc"]
            event.harm_fields = dict(payload["harm_fields"])
            event.dispositions.append(
                {
                    "tier": tier,
                    "deadline_utc": payload["deadline_utc"],
                    "anchor_at": payload["anchor_at"],
                    "calendar": payload["calendar"],
                }
            )
    return events


def projection_check(session: Session) -> list[dict[str, Any]]:
    """Every difference between the ledger's projection and the current rows, ordered by
    event_id and then by the column order of spec 6.3. Empty when everything agrees."""
    projected = project_events(
        dict(row._mapping)
        for row in session.execute(
            text(
                "SELECT seq, entry_type, payload FROM ledger_entry"
                " WHERE entry_type = ANY(:types) ORDER BY seq"
            ),
            {"types": list(PROJECTED_TYPES)},
        )
    )
    events = {
        row.event_id: row
        for row in session.execute(
            text(
                "SELECT event_id, system_id, metric_name, observed_value, threshold_value, state,"
                " deadline_utc FROM event"
            )
        )
    }
    dispositions: dict[str, list[Any]] = {}
    for row in session.execute(
        text(
            "SELECT event_id, tier, deadline_utc, anchor_at, calendar FROM disposition"
            " ORDER BY event_id, seq_in_event"
        )
    ):
        dispositions.setdefault(row.event_id, []).append(row)
    harm: dict[str, dict[str, str]] = {}
    for row in session.execute(text("SELECT event_id, field_key, status FROM harm_field")):
        harm.setdefault(row.event_id, {})[row.field_key] = row.status
    reports = _reports(session)

    mismatches: list[dict[str, Any]] = []
    for event_id in sorted(set(projected) | set(events)):
        out = _Mismatches(event_id, mismatches)
        expected = projected.get(event_id)
        current = events.get(event_id)
        if expected is None or expected.origin is None or current is None:
            # A row the ledger never created, or an event the ledger created whose row is gone.
            created = expected is not None and expected.origin is not None
            out.differ(
                "event",
                "present" if created else "absent",
                "absent" if current is None else "present",
            )
            continue

        out.differ("event.state", expected.state, current.state)
        out.differ_time("event.deadline_utc", expected.deadline_utc, current.deadline_utc)
        out.differ("event.system_id", expected.system_id, current.system_id)
        for column in _METRIC_COLUMNS:
            out.differ(
                f"event.{column}", getattr(expected, column), _text(getattr(current, column))
            )

        stored = dispositions.get(event_id, [])
        out.differ("disposition.count", len(expected.dispositions), len(stored))
        if expected.dispositions and stored:
            want, have = expected.dispositions[-1], stored[-1]
            for column in _DISPOSITION_COLUMNS:
                if column in _TIME_COLUMNS:
                    out.differ_time(f"disposition.{column}", want[column], getattr(have, column))
                else:
                    out.differ(f"disposition.{column}", want[column], getattr(have, column))

        if expected.harm_fields is not None:
            statuses = harm.get(event_id, {})
            for key in HARM_FIELDS:
                out.differ(
                    f"harm_field[{key}].status", expected.harm_fields.get(key), statuses.get(key)
                )

        if reports is not None:
            stored_reports = reports.get(event_id, {})
            out.differ("report.count", len(expected.reports), len(stored_reports))
            wanted = {report["report_id"]: report for report in expected.reports}
            for report_id in sorted(set(wanted) | set(stored_reports)):
                for column in ("json_sha256", "pdf_sha256"):
                    out.differ(
                        f"report[{report_id}].{column}",
                        wanted.get(report_id, {}).get(column),
                        stored_reports.get(report_id, {}).get(column),
                    )
    return mismatches


@dataclass
class _Mismatches:
    """Appends {event_id, column, expected, actual} for one event to the shared list."""

    event_id: str
    out: list[dict[str, Any]]

    def differ(self, column: str, expected: Any, actual: Any) -> None:
        if expected != actual:
            self.out.append(
                {
                    "event_id": self.event_id,
                    "column": column,
                    "expected": expected,
                    "actual": actual,
                }
            )

    def differ_time(self, column: str, expected: str | None, actual: datetime | None) -> None:
        """Compared as instants; reported as the payload's string and the row in API form."""
        if _instant(expected) != actual:
            self.out.append(
                {
                    "event_id": self.event_id,
                    "column": column,
                    "expected": expected,
                    "actual": None if actual is None else api_time(actual),
                }
            )


def _instant(value: str | None) -> datetime | None:
    """An API time string (`...Z`) as an aware datetime; a malformed one stays unequal to any
    row value, so it shows as a mismatch instead of raising."""
    if value is None:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return _NEVER
    return moment if moment.tzinfo is not None else _NEVER


def _text(value: Any) -> Any:
    """A numeric column as the payload writes it (format(value, "f")); other values as they are."""
    return format(value, "f") if isinstance(value, Decimal) else value


def _reports(session: Session) -> dict[str, dict[str, dict[str, str]]] | None:
    """{event_id: {report_id: {json_sha256, pdf_sha256}}}, or None when Part 1's report table
    does not exist on this database (decision 305: skipped, nothing compared)."""
    if session.execute(text("SELECT to_regclass('report') IS NOT NULL")).scalar_one() is not True:
        return None
    reports: dict[str, dict[str, dict[str, str]]] = {}
    for row in session.execute(
        text("SELECT event_id, report_id, json_sha256, pdf_sha256 FROM report")
    ):
        reports.setdefault(row.event_id, {})[row.report_id] = {
            "json_sha256": row.json_sha256,
            "pdf_sha256": row.pdf_sha256,
        }
    return reports
