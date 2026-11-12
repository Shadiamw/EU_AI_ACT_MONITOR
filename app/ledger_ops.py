"""Phase 5 Part 3: ledger operations.

- project_events(entries): pure. Replays ledger entries, in seq order, into the values the
  event, disposition, harm_field and report rows should hold (spec 6.3, D13).
- projection_check(session): compares those values with the current rows and returns the
  mismatches, [{event_id, column, expected, actual}], empty when every row agrees (spec 6.1).
- nightly_due(engine) and run_verification(engine, trigger): the nightly verification (spec
  6.1, D11; decisions 302 and 303), run by the worker; its results go to
  ledger_verification_run, never into the ledger.

The chain covers ledger rows only, so this is how a direct edit to an event, disposition or
harm_field row is found. It compares exactly the columns of spec 6.3: timestamps as instants
(reported in the API form), decimals as exact strings. A gap event's system_id comes from
GAP_OPENED and its metric fields are expected null; harm fields are compared only once a
DISPOSITION_* entry carries them (decision 304). The report table belongs to Part 1; when it
is absent, the report comparison is skipped (decision 305).

projection_check reads only, in the caller's session; it never writes and never commits.
Mismatches carry ids, column names and the two values, nothing else; the alert log line of a
run carries seqs and ids only, never a value.
"""

import json
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import Connection, Engine, text
from sqlalchemy.orm import Session

from app import clock, config, reports
from app.events import HARM_FIELDS
from app.formats import api_time, new_id
from app.ledger.verify import verify_ledger

logger = logging.getLogger("ledger_ops")

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
    wins. deadline_utc and harm_fields come from the latest DISPOSITION_* payload.

    A payload that has been edited outside the app (Model 5 Ex-5) never raises: a missing key
    reads as null and an entry without a string event_id is skipped, so the edit shows up as
    mismatches instead of stopping the check."""
    events: dict[str, Projected] = {}
    for entry in sorted(entries, key=lambda item: int(item["seq"])):
        entry_type = entry["entry_type"]
        if entry_type not in PROJECTED_TYPES:
            continue
        payload: Mapping[str, Any] = entry["payload"] if isinstance(entry["payload"], dict) else {}
        event_id = payload.get("event_id")
        if not isinstance(event_id, str):
            continue
        event = events.setdefault(event_id, Projected())
        if entry_type == "EVENT_FLAGGED":
            event.origin, event.state = entry_type, "FLAGGED"
            event.system_id = payload.get("system_id")
            event.metric_name = payload.get("metric_name")
            event.observed_value = payload.get("observed_value")
            event.threshold_value = payload.get("threshold_value")
        elif entry_type == "GAP_OPENED":
            # Decision 304: a gap has no metric, so the three metric columns stay null.
            event.origin, event.state = entry_type, "FLAGGED"
            event.system_id = payload.get("system_id")
        elif entry_type == "REVIEW_STARTED":
            event.state = "UNDER_REVIEW"
        elif entry_type == "GAP_CLOSED":
            event.state = "CLOSED"
        elif entry_type == "REPORT_ASSEMBLED":
            event.reports.append(
                {
                    "report_id": payload.get("report_id"),
                    "json_sha256": payload.get("json_sha256"),
                    "pdf_sha256": payload.get("pdf_sha256"),
                }
            )
        else:  # DISPOSITION_RECORDED or DISPOSITION_REVISED
            tier = (
                payload.get("tier")
                if entry_type == "DISPOSITION_RECORDED"
                else payload.get("new_tier")
            )
            event.state = "CLOSED" if tier == NOT_SERIOUS else "DISPOSITIONED"
            event.deadline_utc = payload.get("deadline_utc")
            harm_fields = payload.get("harm_fields")
            event.harm_fields = dict(harm_fields) if isinstance(harm_fields, dict) else {}
            event.dispositions.append(
                {
                    "tier": tier,
                    "deadline_utc": payload.get("deadline_utc"),
                    "anchor_at": payload.get("anchor_at"),
                    "calendar": payload.get("calendar"),
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
            wanted = {str(report["report_id"]): report for report in expected.reports}
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

    def differ_time(self, column: str, expected: Any, actual: datetime | None) -> None:
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


def _instant(value: Any) -> datetime | None:
    """An API time string (`...Z`) as an aware datetime; a malformed one stays unequal to any
    row value, so it shows as a mismatch instead of raising."""
    if value is None:
        return None
    if not isinstance(value, str):
        return _NEVER
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


# The nightly verification (spec 6.1).

NIGHTLY = "NIGHTLY"
MANUAL = "MANUAL"
TRIGGERS = (NIGHTLY, MANUAL)

# ASCII "LVRN": serializes verification runs. Never the ledger's append key ("LEDG"), so a run
# never blocks or waits for an append (decision 302).
VERIFICATION_LOCK_KEY = 1280725582

RUN_COLUMNS = (
    "run_id",
    "trigger",
    "started_at",
    "finished_at",
    "valid",
    "entries_checked",
    "first_mismatch",
    "head_seq",
    "head_hash",
    "head_check",
    "projection_mismatches",
    "report_mismatches",
)

_NIGHTLY_ON = text(
    "SELECT EXISTS (SELECT 1 FROM ledger_verification_run WHERE trigger = 'NIGHTLY'"
    " AND (started_at AT TIME ZONE 'UTC')::date = :day)"
)


def _due_at(conn: Connection | Session, moment: datetime) -> bool:
    """The nightly condition at a wall-clock moment: the hour has come and no NIGHTLY run has
    started on that UTC date."""
    moment = moment.astimezone(UTC)
    if moment.hour < config.nightly_verify_hour_utc():
        return False
    return not conn.execute(_NIGHTLY_ON, {"day": moment.date()}).scalar_one()


def nightly_due(engine: Engine) -> bool:
    """True when no NIGHTLY run exists for the current wall-clock UTC date and the wall-clock
    hour is at least NIGHTLY_VERIFY_HOUR_UTC (spec 6.1). Read-only."""
    with engine.connect() as conn:
        due = _due_at(conn, clock.wall_now())
        conn.rollback()
    return due


def run_verification(engine: Engine, trigger: str) -> dict[str, Any] | None:
    """Run the verification once and store it in ledger_verification_run (spec 6.1). Returns
    the stored run, or None for a NIGHTLY run that is no longer due when the lock is held
    (another worker ran it, or the date or hour changed): then nothing is written.

    One connection holds the verification lock (a transaction advisory lock, decision 302)
    from before the checks until the insert commits. The checks run in a second, REPEATABLE
    READ and READ ONLY transaction, so they all see one snapshot of the ledger and the tables:
    verify_ledger (unchanged), the head comparison with the previous run, projection_check and
    reports.verify_stored. Then the lock holder inserts the row and commits."""
    if trigger not in TRIGGERS:
        raise ValueError(f"unknown verification trigger {trigger!r}")
    with engine.connect() as holder:
        holder.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": VERIFICATION_LOCK_KEY})
        started_at = clock.wall_now()
        if trigger == NIGHTLY and not _due_at(holder, started_at):
            holder.rollback()
            return None
        checks = _checks(engine)
        run: dict[str, Any] = {
            "run_id": new_id("lvr"),
            "trigger": trigger,
            "started_at": started_at,
            "finished_at": clock.wall_now(),
            **checks,
        }
        holder.execute(
            text(
                f"INSERT INTO ledger_verification_run ({', '.join(RUN_COLUMNS)}) VALUES"
                f" ({', '.join(':' + column for column in RUN_COLUMNS)})"
            ),
            {
                **run,
                "projection_mismatches": json.dumps(run["projection_mismatches"]),
                "report_mismatches": json.dumps(run["report_mismatches"]),
            },
        )
        holder.commit()
    if is_integrity_failure(run):
        _log_alert(run)
    return run


def is_integrity_failure(run: Mapping[str, Any]) -> bool:
    """Alert A1's condition on one run (spec 7.3): invalid, REGRESSED, or any mismatch."""
    return (
        not run["valid"]
        or run["head_check"] == "REGRESSED"
        or bool(run["projection_mismatches"])
        or bool(run["report_mismatches"])
    )


def _checks(engine: Engine) -> dict[str, Any]:
    """The four checks of spec 6.1 in one REPEATABLE READ, READ ONLY transaction."""
    with Session(engine) as session:
        session.connection(
            execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
        )
        result = verify_ledger(session)
        head = session.execute(
            text("SELECT seq, hash FROM ledger_entry ORDER BY seq DESC LIMIT 1")
        ).one_or_none()
        head_seq = None if head is None else int(head.seq)
        head_hash = None if head is None else str(head.hash)
        projection = projection_check(session)
        report_mismatches = reports.verify_stored(session)
        head_check = _head_check(session, head_seq)
        session.rollback()
    return {
        "valid": result["valid"],
        "entries_checked": result["entries_checked"],
        "first_mismatch": result["first_mismatch"],
        "head_seq": head_seq,
        "head_hash": head_hash,
        "head_check": head_check,
        "projection_mismatches": projection,
        "report_mismatches": report_mismatches,
    }


def _head_check(session: Session, head_seq: int | None) -> str:
    """FIRST_RUN without a previous run; REGRESSED when the head is shorter than the previous
    run's head or the entry at the previous head's seq no longer has its hash; otherwise OK."""
    previous = session.execute(
        text(
            "SELECT head_seq, head_hash FROM ledger_verification_run"
            " ORDER BY started_at DESC, finished_at DESC LIMIT 1"
        )
    ).one_or_none()
    if previous is None:
        return "FIRST_RUN"
    if previous.head_seq is None:  # the previous run saw an empty ledger
        return "OK"
    if head_seq is None or head_seq < previous.head_seq:
        return "REGRESSED"
    stored = session.execute(
        text("SELECT hash FROM ledger_entry WHERE seq = :seq"), {"seq": previous.head_seq}
    ).scalar_one_or_none()
    return "OK" if stored == previous.head_hash else "REGRESSED"


def _log_alert(run: Mapping[str, Any]) -> None:
    """One ERROR line per failing run: seqs, ids and counts, never a value (spec 6.1)."""
    event_ids = sorted({str(item.get("event_id")) for item in run["projection_mismatches"]})
    report_ids = sorted({str(item.get("report_id")) for item in run["report_mismatches"]})
    logger.error(
        "alert: code=LEDGER_INTEGRITY run_id=%s trigger=%s valid=%s first_mismatch=%s"
        " head_check=%s head_seq=%s projection_mismatches=%d event_ids=%s"
        " report_mismatches=%d report_ids=%s",
        run["run_id"],
        run["trigger"],
        str(run["valid"]).lower(),
        run["first_mismatch"],
        run["head_check"],
        run["head_seq"],
        len(run["projection_mismatches"]),
        ",".join(event_ids) or "-",
        len(run["report_mismatches"]),
        ",".join(report_ids) or "-",
    )
