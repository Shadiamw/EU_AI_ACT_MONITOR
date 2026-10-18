"""GET /healthz (spec 4.6, decisions 97 and 111).

No authentication: a process probe has no API key. The body is the three worker values and
nothing else. 503 means the sweeper has run and then gone quiet; a process that has not
swept yet is 200, so a fresh start is not failed before the first pass.
"""

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app import config, db, worker

router = APIRouter()


@router.get("/healthz")
def healthz() -> JSONResponse:
    values = worker.health_values(db.get_engine())
    body = {
        "sweeper_last_ran_seconds_ago": values.sweeper_last_ran_seconds_ago,
        "pending_deliveries": values.pending_deliveries,
        "oldest_pending_delivery_seconds": values.oldest_pending_delivery_seconds,
    }
    age = values.sweeper_last_ran_seconds_ago
    stale = age is not None and age > 3 * config.sweep_interval_seconds()
    return JSONResponse(body, status_code=503 if stale else 200)


