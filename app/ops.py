"""Phase 5 Part 3: the health dashboard and operational alerts (spec 7.1 to 7.3, D14).

- dashboard(session): the body of GET /v1/ops/dashboard, exactly the spec 7.2 keys in order.
  Data comes only from the database and the environment, never from in-process counters.
  `clock` is present only when ENVIRONMENT=test.
- current_alerts(session): the alerts that hold now, in code order, in the shape
  {code, severity, since, detail}. detail carries ids, counts and codes only: never a payload
  value, hash, key, webhook URL or reviewer text.
- raise_alerts(engine): the worker's step. It logs each raised alert once per code and subject
  per wall-clock UTC day ("alert: code=A6 event_id=evt-...", ERROR for critical, WARNING
  otherwise). The memory is in-process, so a restarted worker may repeat a line (decision 309).

Clocks (spec 7.3; Part 3 decision 303): A2 and A3 read the wall clock (clock.wall_now()); A4,
A5, A6, A7, A8 and A9 read the app clock (clock.now(), simulated in tests), as the event,
deadline, delivery and retention times they compare with do. `generated_at` is wall time.

Tables of other parts that are absent are skipped with zero counts (decision 305): without
Part 1's report tables, report counts are 0 and every DISPOSITIONED event is unsubmitted;
without Part 2's event.erased_at, retention.erased is 0 and every event past retain_until counts.

Reads only, in the caller's session (dashboard, current_alerts) or a session of its own
(raise_alerts).
"""

import logging
from collections.abc import Mapping
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock, config, escalations, ledger_ops, worker
from app.formats import api_time, whole_seconds

logger = logging.getLogger("ops")

STATES = ("FLAGGED", "UNDER_REVIEW", "DISPOSITIONED", "CLOSED")
NO_RUN_HOURS = 26  # A2: one nightly run plus 2 hours of slack
FAILED_WINDOW = timedelta(hours=24)  # A8 and failed_deliveries_24h
DUE_SOON = timedelta(hours=24)  # deadlines.due_within_24h_unsubmitted

# Codes logged today: (wall-clock UTC date, code, subject). Lost on restart (decision 309).
_logged: set[tuple[date, str, str | None]] = set()


def _exists(session: Session, table: str, column: str | None = None) -> bool:
    """Whether a table (or a column of it) exists, read-only (decision 305)."""
    if column is None:
        found = session.execute(
            text("SELECT to_regclass(:table) IS NOT NULL"), {"table": table}
        ).scalar_one()
    else:
        found = session.execute(
            text(
                "SELECT EXISTS (SELECT 1 FROM information_schema.columns"
                " WHERE table_schema = current_schema() AND table_name = :table"
                " AND column_name = :column)"
            ),
            {"table": table, "column": column},
        ).scalar_one()
    return bool(found)


def _latest_run(session: Session) -> dict[str, Any] | None:
    row = session.execute(
        text(
            f"SELECT {', '.join(ledger_ops.RUN_COLUMNS)} FROM ledger_verification_run"
            " ORDER BY started_at DESC, finished_at DESC, run_id DESC LIMIT 1"
        )
    ).one_or_none()
    return None if row is None else dict(row._mapping)


def _run_summary(run: Mapping[str, Any]) -> dict[str, Any]:
    """The latest run as the dashboard shows it: its columns without head_hash, and the two
    mismatch lists as counts, because they hold payload values (spec 7.2: ids and counts,
    never personal data)."""
    return {
        "run_id": run["run_id"],
        "trigger": run["trigger"],
        "started_at": api_time(run["started_at"]),
        "finished_at": api_time(run["finished_at"]),
        "valid": run["valid"],
        "entries_checked": run["entries_checked"],
        "first_mismatch": run["first_mismatch"],
        "head_seq": run["head_seq"],
        "head_check": run["head_check"],
        "projection_mismatches": len(run["projection_mismatches"]),
        "report_mismatches": len(run["report_mismatches"]),
    }


def _failed_deliveries(session: Session, now: datetime) -> list[datetime]:
    """When each FAILED delivery's last attempt was, if within 24 h of `now` (app clock): the
    recorded_at of its NOTIFICATION_FAILED entry (decision 307)."""
    result = session.execute(
        text(
            "SELECT CAST(l.recorded_at AS timestamptz) AS failed_at"
            " FROM notification_delivery d JOIN ledger_entry l"
            " ON l.entry_type = 'NOTIFICATION_FAILED'"
            " AND l.payload->>'notification_id' = d.notification_id"
            " AND l.payload->>'webhook_id' = d.webhook_id"
            " WHERE d.status = 'FAILED'"
            " AND CAST(l.recorded_at AS timestamptz) > :since"
            " AND CAST(l.recorded_at AS timestamptz) <= :now"
        ),
        {"since": now - FAILED_WINDOW, "now": now},
    )
    return [row.failed_at for row in result]


def _dispositioned(session: Session) -> list[dict[str, Any]]:
    """DISPOSITIONED events with their deadline and whether a submitted report is bound to the
    latest disposition (spec 7.3 A6). Without Part 1's tables nothing is submitted (305)."""
    if _exists(session, "report") and _exists(session, "report_submission"):
        submitted = (
            "EXISTS (SELECT 1 FROM report r JOIN report_submission s"
            " ON s.report_id = r.report_id"
            " WHERE r.event_id = e.event_id AND r.disposition_id = ("
            "SELECT d.disposition_id FROM disposition d WHERE d.event_id = e.event_id"
            " ORDER BY d.seq_in_event DESC LIMIT 1))"
        )
    else:
        submitted = "false"
    result = session.execute(
        text(
            f"SELECT e.event_id, e.deadline_utc, {submitted} AS submitted FROM event e"
            " WHERE e.state = 'DISPOSITIONED' AND e.deadline_utc IS NOT NULL"
            " ORDER BY e.deadline_utc, e.event_id"
        )
    )
    return [dict(row._mapping) for row in result]


def _past_retention(session: Session, now: datetime) -> list[datetime]:
    """retain_until of each event past it and not erased (without erased_at, every one)."""
    erased_filter = " AND erased_at IS NULL" if _exists(session, "event", "erased_at") else ""
    result = session.execute(
        text(f"SELECT retain_until FROM event WHERE retain_until < :now{erased_filter}"),
        {"now": now},
    )
    return [row.retain_until for row in result]


def _alert(
    code: str, severity: str, since: datetime | None, detail: dict[str, Any]
) -> dict[str, Any]:
    return {
        "code": code,
        "severity": severity,
        "since": None if since is None else api_time(since),
        "detail": detail,
    }


def current_alerts(session: Session) -> list[dict[str, Any]]:
    """The alerts that hold now, in code order (spec 7.3). `since` is when the condition
    became true: the run's finished_at for A1, 26 hours after it for A2, the moment the
    threshold was crossed for A3, A4 and A5, the deadline for A6, the deadline less the warning
    window for A7, and the earliest failure or retain_until counted for A8 and A9."""
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    values = worker.health_values(engine)
    wall = clock.wall_now()
    now = clock.now()
    alerts: list[dict[str, Any]] = []

    # A1 (critical): the latest run is invalid, REGRESSED or has a mismatch (spec 6.1).
    run = _latest_run(session)
    if run is not None and ledger_ops.is_integrity_failure(run):
        alerts.append(
            _alert(
                "A1",
                "critical",
                run["finished_at"],
                {
                    "run_id": run["run_id"],
                    "valid": run["valid"],
                    "first_mismatch": run["first_mismatch"],
                    "head_check": run["head_check"],
                    "projection_mismatches": len(run["projection_mismatches"]),
                    "report_mismatches": len(run["report_mismatches"]),
                },
            )
        )

    # A2 (warning): no run for more than 26 hours (wall clock), or no run at all (308).
    stale_after = timedelta(hours=NO_RUN_HOURS)
    if run is None:
        alerts.append(_alert("A2", "warning", None, {"last_run": None}))
    elif wall - run["finished_at"] > stale_after:
        alerts.append(
            _alert("A2", "warning", run["finished_at"] + stale_after, {"last_run": run["run_id"]})
        )

    # A3 (critical): the sweeper is quiet for more than 3 intervals (the /healthz 503 rule).
    bound = 3 * config.sweep_interval_seconds()
    age = values.sweeper_last_ran_seconds_ago
    if age is not None and age > bound:
        alerts.append(
            _alert(
                "A3",
                "critical",
                wall - timedelta(seconds=age - bound),
                {"sweeper_last_ran_seconds_ago": age},
            )
        )

    # A4 (warning): the oldest PENDING delivery is older than NOTIFY_MAX_DELAY_SECONDS.
    max_delay = config.notify_max_delay_seconds()
    oldest = values.oldest_pending_delivery_seconds
    if oldest is not None and oldest > max_delay:
        alerts.append(
            _alert(
                "A4",
                "warning",
                now - timedelta(seconds=oldest - max_delay),
                {
                    "pending_deliveries": values.pending_deliveries,
                    "oldest_pending_delivery_seconds": oldest,
                },
            )
        )

    # A5 (warning): overdue escalations (LIM-08).
    if values.overdue_escalations > 0:
        first_due = session.execute(
            text(
                "SELECT min(due_at) FROM escalation WHERE status = 'PENDING' AND due_at < :before"
            ),
            {"before": now - timedelta(seconds=escalations.OVERDUE_AFTER_SECONDS)},
        ).scalar()
        since = (
            None
            if first_due is None
            else first_due + timedelta(seconds=escalations.OVERDUE_AFTER_SECONDS)
        )
        alerts.append(
            _alert("A5", "warning", since, {"overdue_escalations": values.overdue_escalations})
        )

    # A6 (critical) and A7 (warning): one per DISPOSITIONED event without a submitted report.
    # A7 covers deadlines not yet passed and closer than ALERT_DEADLINE_WARNING_HOURS; a passed
    # deadline is A6 only.
    warning = timedelta(hours=config.alert_deadline_warning_hours())
    unsubmitted = [event for event in _dispositioned(session) if not event["submitted"]]
    for event in unsubmitted:
        if event["deadline_utc"] < now:
            alerts.append(
                _alert("A6", "critical", event["deadline_utc"], {"event_id": event["event_id"]})
            )
    for event in unsubmitted:
        if now <= event["deadline_utc"] and event["deadline_utc"] - now < warning:
            alerts.append(
                _alert(
                    "A7",
                    "warning",
                    event["deadline_utc"] - warning,
                    {"event_id": event["event_id"]},
                )
            )

    # A8 (warning): FAILED deliveries whose last attempt was in the last 24 h (decision 307).
    failed = _failed_deliveries(session, now)
    if failed:
        alerts.append(_alert("A8", "warning", min(failed), {"failed_deliveries_24h": len(failed)}))

    # A9 (info): events past retain_until and not erased (REQ-16 operator task).
    past = _past_retention(session, now)
    if past:
        alerts.append(_alert("A9", "info", min(past), {"past_retain_until_not_erased": len(past)}))
    return alerts


def dashboard(session: Session) -> dict[str, Any]:
    """GET /v1/ops/dashboard: exactly the spec 7.2 keys, in order."""
    engine = session.get_bind()
    assert isinstance(engine, Engine)
    values = worker.health_values(engine)
    wall = clock.wall_now()
    now = clock.now()
    run = _latest_run(session)
    entries, head_seq = session.execute(text("SELECT count(*), max(seq) FROM ledger_entry")).one()
    by_state = dict.fromkeys(STATES, 0)
    for state, count in session.execute(text("SELECT state, count(*) FROM event GROUP BY state")):
        if state in by_state:
            by_state[state] = int(count)
    open_gaps = session.execute(
        text("SELECT count(*) FROM event WHERE type = 'monitoring_gap' AND state <> 'CLOSED'")
    ).scalar_one()
    dispositioned = _dispositioned(session)
    reports = {"assembled": 0, "submitted": 0, "superseded": 0}
    if _exists(session, "report") and _exists(session, "report_submission"):
        assembled, submitted, superseded = session.execute(
            text(
                "SELECT count(*),"
                " count(*) FILTER (WHERE EXISTS (SELECT 1 FROM report_submission s"
                " WHERE s.report_id = r.report_id)),"
                " count(*) FILTER (WHERE r.disposition_id <> (SELECT d.disposition_id"
                " FROM disposition d WHERE d.event_id = r.event_id"
                " ORDER BY d.seq_in_event DESC LIMIT 1))"
                " FROM report r"
            )
        ).one()
        reports = {
            "assembled": int(assembled),
            "submitted": int(submitted),
            "superseded": int(superseded),
        }
    erased = 0
    if _exists(session, "event", "erased_at"):
        erased = int(
            session.execute(
                text("SELECT count(*) FROM event WHERE erased_at IS NOT NULL")
            ).scalar_one()
        )
    body: dict[str, Any] = {
        "generated_at": api_time(wall),
        "version": config.app_version(),
        "environment": config.environment(),
        "worker": {
            "sweeper_last_ran_seconds_ago": values.sweeper_last_ran_seconds_ago,
            "pending_deliveries": values.pending_deliveries,
            "oldest_pending_delivery_seconds": values.oldest_pending_delivery_seconds,
            "failed_deliveries_24h": len(_failed_deliveries(session, now)),
            "overdue_escalations": values.overdue_escalations,
        },
        "ledger": {
            "entries": int(entries),
            "head_seq": head_seq,
            "last_verification": None if run is None else _run_summary(run),
            "last_verification_age_seconds": (
                None if run is None else whole_seconds(run["finished_at"], wall)
            ),
        },
        "events": {"by_state": by_state, "open_gaps": int(open_gaps)},
        "deadlines": {
            "due_within_24h_unsubmitted": sum(
                1
                for event in dispositioned
                if not event["submitted"] and now <= event["deadline_utc"] < now + DUE_SOON
            ),
            "passed_unsubmitted": sum(
                1
                for event in dispositioned
                if not event["submitted"] and event["deadline_utc"] < now
            ),
            "passed_submitted": sum(
                1 for event in dispositioned if event["submitted"] and event["deadline_utc"] < now
            ),
        },
        "reports": reports,
        "retention": {
            "past_retain_until_not_erased": len(_past_retention(session, now)),
            "erased": erased,
        },
    }
    if config.is_test_environment():
        body["clock"] = {"simulated_time": api_time(now), "offset_seconds": clock.offset_seconds()}
    body["alerts"] = current_alerts(session)
    return body


def _subject(alert: Mapping[str, Any]) -> str | None:
    """The subject an alert is about, for the once-a-day rule: the run for A1, the event for A6
    and A7; the others are about the whole platform."""
    detail = alert["detail"]
    if alert["code"] == "A1":
        return str(detail["run_id"])
    if alert["code"] in ("A6", "A7"):
        return str(detail["event_id"])
    return None


def raise_alerts(engine: Engine) -> list[dict[str, Any]]:
    """The worker's alert step (spec 7.3): log each raised alert once per code and subject per
    wall-clock UTC day, in its own read-only transaction. Returns the alerts logged now.

    A1 is logged as "alert: code=A1 run_id=..."; the run's own "alert: code=LEDGER_INTEGRITY"
    line (spec 6.1) is written once by ledger_ops when the run is recorded, never here."""
    with Session(engine) as session:
        alerts = current_alerts(session)
        session.rollback()
    today = clock.wall_now().date()
    for key in [key for key in _logged if key[0] != today]:
        _logged.discard(key)
    logged = []
    for alert in alerts:
        key = (today, alert["code"], _subject(alert))
        if key in _logged:
            continue
        _logged.add(key)
        fields = " ".join(f"{name}={value}" for name, value in alert["detail"].items())
        level = logging.ERROR if alert["severity"] == "critical" else logging.WARNING
        logger.log(level, "alert: code=%s %s", alert["code"], fields)
        logged.append(alert)
    return logged
