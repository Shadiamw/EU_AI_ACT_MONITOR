"""GET /healthz (spec 4.6, decisions 97 and 111; Phase 4 LIM-09; Phase 5 spec 6.5, D15).

No authentication: a process probe has no API key. The body is the worker values, then the four
Phase 5 fields (spec 6.5), and nothing else: version (APP_VERSION, null when unset), the latest
ledger verification run of any trigger as {finished_at, valid, head_check} or null, its age in
whole seconds on the wall clock or null, and reset_count (platform_counter, 0 before the first
reset). No hash, payload value or entry content is exposed.

503 means the sweeper has run and then gone quiet, or the database is unreachable (LIM-09). A
process that has not swept yet is 200, so a fresh start is not failed before the first pass.
Ledger integrity never makes /healthz 503 (spec 6.5). The 503 body for an unreachable database
carries version from the environment and the other three new fields null (Part 3 decision 306).
"""

from typing import Any

from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import clock, config, db, worker
from app.formats import api_time, whole_seconds

router = APIRouter()


@router.get("/livez")
def livez() -> JSONResponse:
    """Process liveness for cloud load balancers. Always 200 if the API process is up."""
    return JSONResponse({"ok": True})


def _phase5_values(engine: Engine) -> dict[str, Any]:
    """Read-only. The latest ledger_verification_run (any trigger) and reset_count (spec 6.5)."""
    with Session(engine) as session:
        latest = session.execute(
            text(
                "SELECT finished_at, valid, head_check FROM ledger_verification_run"
                " ORDER BY started_at DESC, finished_at DESC, run_id DESC LIMIT 1"
            )
        ).one_or_none()
        reset_count = session.execute(text("SELECT reset_count FROM platform_counter")).scalar()
        session.rollback()
    return {
        "last_verification": (
            None
            if latest is None
            else {
                "finished_at": api_time(latest.finished_at),
                "valid": latest.valid,
                "head_check": latest.head_check,
            }
        ),
        "last_verification_age_seconds": (
            None if latest is None else whole_seconds(latest.finished_at, clock.wall_now())
        ),
        "reset_count": 0 if reset_count is None else int(reset_count),
    }


@router.get("/healthz")
def healthz() -> JSONResponse:
    try:
        engine = db.get_engine()
        values = worker.health_values(engine)
        phase5 = _phase5_values(engine)
    except Exception:
        return JSONResponse(
            {
                "sweeper_last_ran_seconds_ago": None,
                "pending_deliveries": 0,
                "oldest_pending_delivery_seconds": None,
                "overdue_escalations": 0,
                "version": config.app_version(),
                "last_verification": None,
                "last_verification_age_seconds": None,
                "reset_count": None,
            },
            status_code=503,
        )
    body = {
        "sweeper_last_ran_seconds_ago": values.sweeper_last_ran_seconds_ago,
        "pending_deliveries": values.pending_deliveries,
        "oldest_pending_delivery_seconds": values.oldest_pending_delivery_seconds,
        "overdue_escalations": values.overdue_escalations,
        "version": config.app_version(),
        **phase5,
    }
    age = values.sweeper_last_ran_seconds_ago
    stale = age is not None and age > 3 * config.sweep_interval_seconds()
    return JSONResponse(body, status_code=503 if stale else 200)
