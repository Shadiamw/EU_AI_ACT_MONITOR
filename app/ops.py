"""Phase 5 Part 3: operational alerts (spec 7.3).

current_alerts(session) returns the alerts that hold now, in the dashboard's shape (spec 7.2):
[{code, severity, since, detail}], where detail carries ids and counts only, never personal
data or payload values. Only A1 exists so far (M4); A2 to A9 come with the dashboard (M7).

- A1 (critical): the latest ledger verification run is invalid, has head_check REGRESSED, or
  lists any projection or report mismatch (spec 6.1). `since` is that run's finished_at.

Reads only, in the caller's session.
"""

from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ledger_ops
from app.formats import api_time


def current_alerts(session: Session) -> list[dict[str, Any]]:
    """The alerts that hold now, in code order."""
    alerts: list[dict[str, Any]] = []
    a1 = _a1(session)
    if a1 is not None:
        alerts.append(a1)
    return alerts


def _a1(session: Session) -> dict[str, Any] | None:
    latest = session.execute(
        text(
            f"SELECT {', '.join(ledger_ops.RUN_COLUMNS)} FROM ledger_verification_run"
            " ORDER BY started_at DESC, finished_at DESC LIMIT 1"
        )
    ).one_or_none()
    if latest is None:
        return None
    run = dict(latest._mapping)
    if not ledger_ops.is_integrity_failure(run):
        return None
    return {
        "code": "A1",
        "severity": "critical",
        "since": api_time(run["finished_at"]),
        "detail": {
            "run_id": run["run_id"],
            "valid": run["valid"],
            "first_mismatch": run["first_mismatch"],
            "head_check": run["head_check"],
            "projection_mismatches": len(run["projection_mismatches"]),
            "report_mismatches": len(run["report_mismatches"]),
        },
    }
