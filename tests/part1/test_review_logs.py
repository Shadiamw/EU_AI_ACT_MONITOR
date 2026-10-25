"""Phase 4 Part 1: no evidence or context text in any log line, at any level (Phase 4 spec 1.8).

One test walks every Part 1 path that handles the text: the pre-fill at flag time, viewer and
reviewer views of the detail (the review start), the list and the audit trail, two harm-field
writes (a reviewer's override, then the pre-fill put back) and a failed view. Every record any
logger captured, at DEBUG and above, is checked; the three kinds of api.review line are
required to appear, so the check cannot pass on an empty capture.
"""

import logging
from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import audit
from app.auth import Principal
from app.review import apply_harm_fields, parse_harm_fields, require_complete
from tests.api.conftest import ApiClient
from tests.db_helpers import HARM_FIELDS
from tests.part1.conftest import get, post
from tests.part1.test_review_prefill import AC07_AT, CONTEXT_TEXT, RIGHTS, post_ac07
from tests.worker.conftest import FakeClock

OVERRIDE = "reviewer override 5c1e: the routing outputs were wrong"
# Pieces of each text, so a line that quotes only part of one is still caught.
SECRETS = (
    CONTEXT_TEXT,
    "makes automated decisions",
    "degraded performance",
    "is below the limit",
    "is above the limit",
    OVERRIDE,
    "routing outputs",
)


def write_fields(app_engine: Engine, event_id: str, rights: dict[str, Any], now: datetime) -> None:
    raw = {key: {"status": "NOT_APPLICABLE"} for key in HARM_FIELDS}
    raw[RIGHTS] = rights
    decisions = require_complete(parse_harm_fields(raw))
    with Session(app_engine) as session:
        session.execute(text("SELECT 1 FROM event WHERE event_id = :e FOR UPDATE"), {"e": event_id})
        apply_harm_fields(session, event_id, Principal("REVIEWER", "rev-1", None), decisions, now)
        session.commit()


def test_no_evidence_or_context_text_in_any_log_line(
    client: ApiClient,
    app_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger="api")
    caplog.set_level(logging.DEBUG, logger="worker")

    event_id = post_ac07(client, fake_clock)
    assert get(client, f"/v1/events/{event_id}").status_code == 200
    assert get(client, f"/v1/events/{event_id}", actor="rev-1").json()["state"] == "UNDER_REVIEW"
    assert get(client, "/v1/events").status_code == 200
    assert get(client, f"/v1/events/{event_id}/audit").status_code == 200
    write_fields(app_engine, event_id, {"status": "CONFIRMED", "evidence": OVERRIDE}, AC07_AT)
    write_fields(app_engine, event_id, {"status": "CONFIRMED"}, AC07_AT)
    assert get(client, f"/v1/events/{event_id}").json()["harm_fields"][RIGHTS]["evidence"] != (
        OVERRIDE
    )
    with monkeypatch.context() as patch:

        def failing(*args: Any, **kwargs: Any) -> None:
            raise RuntimeError(f"view failed {OVERRIDE}")

        patch.setattr(audit, "record_view", failing)
        assert get(client, f"/v1/events/{event_id}", actor="rev-1").status_code == 500
    # A second breach on a system with the text, flagged while the view above failed.
    assert post(client, value=0.35, timestamp="2026-10-05T09:00:00Z").status_code == 201

    review_lines = {r.getMessage().split(":")[0] for r in caplog.records if r.name == "api.review"}
    assert review_lines == {"prefill", "review started", "harm field changed"}
    assert any(r.levelno == logging.DEBUG for r in caplog.records)
    for record in caplog.records:
        logged = " ".join(part for part in (record.getMessage(), record.exc_text or "") if part)
        for secret in SECRETS:
            assert secret not in logged, (record.name, record.levelname)
