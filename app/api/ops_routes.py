"""Phase 5 Part 3 HTTP: the ledger-verification endpoints (spec 6.1, 7.4; contract delta 5).

- POST /v1/ledger/verifications (REVIEWER only): runs the verification of spec 6.1 now, as a
  MANUAL run, and returns 201 with the stored run.
- GET /v1/ledger/verifications?limit=n (REVIEWER or VIEWER): the stored runs, newest first by
  started_at, as a bare JSON array; limit defaults to 10 and is at most 100.

A run is exactly the twelve columns of ledger_verification_run, with started_at and
finished_at in the API's time format. Order of checks: 401 and 403 (the `require` dependency),
then 400 MALFORMED_QUERY for the limit. GET /v1/ledger/verify is not here and is unchanged.
"""

import re
from collections.abc import Mapping
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import db, ledger_ops
from app.auth import Principal, require
from app.db import get_session
from app.errors import ApiError
from app.formats import api_time
from app.jsonout import ExactJSONResponse

router = APIRouter()
DbSession = Annotated[Session, Depends(get_session)]
Reviewer = Annotated[Principal, Depends(require("REVIEWER"))]
Reader = Annotated[Principal, Depends(require("REVIEWER", "VIEWER"))]

DEFAULT_LIMIT = 10
MAX_LIMIT = 100
_LIMIT = re.compile(r"[0-9]{1,3}")

_SELECT_RUNS = f"SELECT {', '.join(ledger_ops.RUN_COLUMNS)} FROM ledger_verification_run"


def _limit(request: Request) -> int:
    """The `limit` query parameter: absent gives 10; otherwise exactly one value, an integer
    from 1 to 100. Anything else (empty, repeated, not an integer, out of range) is 400."""
    values = request.query_params.getlist("limit")
    if not values:
        return DEFAULT_LIMIT
    if len(values) == 1 and _LIMIT.fullmatch(values[0]) and 1 <= int(values[0]) <= MAX_LIMIT:
        return int(values[0])
    raise ApiError(400, "MALFORMED_QUERY", "limit must be an integer from 1 to 100")


def _run(row: Mapping[str, Any]) -> dict[str, Any]:
    """A stored run as the API returns it: the twelve columns in table order, times as API
    strings."""
    run = {column: row[column] for column in ledger_ops.RUN_COLUMNS}
    run["started_at"] = api_time(row["started_at"])
    run["finished_at"] = api_time(row["finished_at"])
    return run


@router.post("/v1/ledger/verifications", status_code=201)
def post_verification(principal: Reviewer, session: DbSession) -> ExactJSONResponse:
    stored = ledger_ops.run_verification(db.get_engine(), ledger_ops.MANUAL)
    if stored is None:  # only a NIGHTLY run can be skipped
        raise RuntimeError("a MANUAL verification run returned no run")
    row = session.execute(
        text(f"{_SELECT_RUNS} WHERE run_id = :run_id"), {"run_id": stored["run_id"]}
    ).one()
    session.commit()
    return ExactJSONResponse(_run(dict(row._mapping)), status_code=201)


@router.get("/v1/ledger/verifications")
def get_verifications(request: Request, principal: Reader, session: DbSession) -> ExactJSONResponse:
    limit = _limit(request)
    rows = session.execute(
        text(f"{_SELECT_RUNS} ORDER BY started_at DESC, finished_at DESC, run_id DESC LIMIT :n"),
        {"n": limit},
    )
    body = [_run(dict(row._mapping)) for row in rows]
    session.commit()
    return ExactJSONResponse(body)
