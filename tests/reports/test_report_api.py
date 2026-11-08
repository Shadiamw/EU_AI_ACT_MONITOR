"""GET /v1/events/{event_id}/report, the report listing and retrieval by id (Phase 5 spec 3.6,
3.9; AC-13, AC-14). The Phase 4 guard (409, then 501) is replaced by the real report."""

import asyncio

import httpx
import pytest
from sqlalchemy import Engine

from app import personal_data
from app.main import create_app
from tests.api.conftest import FORBIDDEN, NOT_FOUND, UNAUTHORIZED, ApiClient
from tests.db_helpers import bearer
from tests.part2.conftest import add_gap_event
from tests.reports.conftest import (
    NO_DISPOSITION,
    TIERS,
    dispose,
    disposed_event,
    fetch,
    flag,
    get_report,
    ledger_rows,
    listing,
    submit,
    table_rows,
)
from tests.worker.conftest import FakeClock

MALFORMED_QUERY = {"error_code": "MALFORMED_QUERY", "message": "format must be json or pdf"}
ERASED = {
    "error_code": "RECORD_ERASED",
    "message": "the personal data of this record has been erased",
}


@pytest.mark.parametrize("tier", TIERS)
def test_report_after_a_disposition_is_200_not_501(
    client: ApiClient, fake_clock: FakeClock, tier: str
) -> None:
    event_id = disposed_event(client, fake_clock, tier)
    fake_clock.advance(60)
    response = fetch(client, event_id)
    assert response.status_code == 200, response.text
    assert response.json()["report_kind"] == (
        "NOT_SERIOUS_CLOSURE" if tier == "NOT_SERIOUS" else "SERIOUS_INCIDENT"
    )


def test_ac14_the_reviewer_gets_the_report_and_others_are_denied(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "DEATH")
    before = ledger_rows(owner_engine)
    for actor in ("viewer-1", "sys-42"):
        response = fetch(client, event_id, actor=actor)
        assert (response.status_code, response.json()) == (403, FORBIDDEN)
        response = fetch(client, event_id, "pdf", actor=actor)
        assert response.status_code == 403
    response = fetch(client, event_id, actor=None)
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    # Nothing was assembled or appended for a denied call.
    assert ledger_rows(owner_engine) == before
    assert table_rows(owner_engine, "report") == []
    assert fetch(client, event_id).status_code == 200


def test_report_check_order(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 3.6: the first check that fails decides the response."""
    undisposed = flag(client)
    disposed = disposed_event_after(client, fake_clock)
    # 1: no key beats everything; 2: a viewer is 403 even with a bad format and a bad event.
    assert fetch(client, "evt-nope", "xml", actor=None).status_code == 401
    assert fetch(client, "evt-nope", "xml", actor="viewer-1").status_code == 403
    # 3: a bad format is 400 before the event is looked up.
    for fmt in ("xml", "", "JSON", "pdf%20"):
        response = fetch(client, "evt-nope", fmt)
        assert (response.status_code, response.json()) == (400, MALFORMED_QUERY), fmt
    both = client.get(
        f"/v1/events/{disposed}/report?format=pdf&format=json", headers=bearer("rev-1")
    )
    assert (both.status_code, both.json()) == (400, MALFORMED_QUERY)
    # 4: an unknown event is 404.
    response = fetch(client, "evt-nope")
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    # 6: no disposition row is 409, for a breach event and for a gap event, and writes nothing.
    gap = add_gap_event(owner_engine)
    before = ledger_rows(owner_engine)
    for event_id in (undisposed, gap):
        response = fetch(client, event_id)
        assert (response.status_code, response.json()) == (409, NO_DISPOSITION), event_id
    assert ledger_rows(owner_engine) == before
    assert table_rows(owner_engine, "report") == []
    # 5: an erased record is 410, before the 409 for a missing disposition.
    monkeypatch.setattr(personal_data, "is_erased", lambda session, event_id: True)
    for event_id in (undisposed, disposed):
        response = fetch(client, event_id)
        assert (response.status_code, response.json()) == (410, ERASED), event_id
    assert table_rows(owner_engine, "report") == []


def disposed_event_after(client: ApiClient, fake_clock: FakeClock) -> str:
    """A second event: a different metric so it is not a duplicate of the first flag."""
    from tests.part1.conftest import post

    response = post(client, metric_name="accuracy", metric_type="performance", value=0.5)
    assert response.status_code == 201, response.text
    event_id: str = response.json()["flagged_event_id"]
    fake_clock.advance(3600)
    assert dispose(client, event_id, "DEATH").status_code == 200
    return event_id


def test_a_revision_creates_a_new_report_version(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "OTHER_SERIOUS")
    fake_clock.advance(3600)
    first_response = fetch(client, event_id)
    first = first_response.json()
    first_pdf = fetch(client, event_id, "pdf").content
    assert first["report_version"] == 1
    fake_clock.advance(86400)
    assert dispose(client, event_id, "DEATH").status_code == 200
    fake_clock.advance(3600)
    second = get_report(client, event_id)
    assert second["report_version"] == 2
    assert second["report_id"] != first["report_id"]
    assert second["determination"]["tier"] == "DEATH"
    assert second["determination"]["disposition_id"] != first["determination"]["disposition_id"]
    assert second["timestamps"]["last_revised_at"] == "2026-09-28T15:05:00Z"
    # Version 1 is unchanged and still served by id.
    old = client.get(f"/v1/reports/{first['report_id']}", headers=bearer("rev-1"))
    assert old.content == first_response.content
    old_pdf = client.get(f"/v1/reports/{first['report_id']}?format=pdf", headers=bearer("rev-1"))
    assert old_pdf.content == first_pdf
    assert sorted(r["report_version"] for r in table_rows(owner_engine, "report")) == [1, 2]
    # The next GET serves version 2 again without a new report.
    assert fetch(client, event_id).content == fetch(client, event_id).content
    assert len(table_rows(owner_engine, "report")) == 2


def test_concurrent_first_requests_assemble_one_report(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "DEATH")
    fake_clock.advance(3600)
    calls = 20

    async def fire() -> list[httpx.Response]:
        transport = httpx.ASGITransport(app=create_app())
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as parallel:
            return await asyncio.gather(
                *(
                    parallel.get(f"/v1/events/{event_id}/report", headers=bearer("rev-1"))
                    for _ in range(calls)
                )
            )

    responses = asyncio.run(fire())
    assert [r.status_code for r in responses] == [200] * calls
    assert len({r.content for r in responses}) == 1
    assert len(table_rows(owner_engine, "report")) == 1
    types = [e["entry_type"] for e in ledger_rows(owner_engine) if e["subject_id"] == event_id]
    assert types.count("REPORT_ASSEMBLED") == 1


def test_report_status_is_ready_then_submitted(client: ApiClient, fake_clock: FakeClock) -> None:
    event_id = disposed_event(client, fake_clock, "OTHER_SERIOUS")
    fake_clock.advance(3600)
    assert listing(client, event_id).json() == []
    first = get_report(client, event_id)
    sha = fetch(client, event_id).headers["x-report-sha256"]
    [row] = listing(client, event_id).json()
    assert (row["status"], row["submission"]) == ("ASSEMBLED", None)
    assert row["report_id"] == first["report_id"]
    assert row["assembled_by"] == "rev-1"
    assert row["json_sha256"] == sha
    assert submit(client, event_id, first["report_id"], sha).status_code == 201
    [row] = listing(client, event_id).json()
    assert row["status"] == "SUBMITTED"
    assert row["submission"]["report_type"] == "Initial"
    assert row["submission"]["submission_kind"] == "INITIAL"
    # The event stays DISPOSITIONED (D1).
    event = client.get(f"/v1/events/{event_id}", headers=bearer("viewer-1")).json()
    assert event["state"] == "DISPOSITIONED"
    fake_clock.advance(60)
    assert dispose(client, event_id, "DEATH").status_code == 200
    [row] = listing(client, event_id).json()
    assert row["status"] == "SUBMITTED_SUPERSEDED"
    fake_clock.advance(60)
    get_report(client, event_id)
    rows = listing(client, event_id).json()
    assert [r["status"] for r in rows] == ["SUBMITTED_SUPERSEDED", "ASSEMBLED"]
    assert [r["report_version"] for r in rows] == [1, 2]


def test_a_superseded_unsubmitted_report_is_superseded(
    client: ApiClient, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "OTHER_SERIOUS")
    fake_clock.advance(60)
    get_report(client, event_id)
    assert dispose(client, event_id, "DEATH").status_code == 200
    assert [r["status"] for r in listing(client, event_id).json()] == ["SUPERSEDED"]


def test_the_listing_roles_and_errors(client: ApiClient, fake_clock: FakeClock) -> None:
    event_id = disposed_event(client, fake_clock, "DEATH")
    fake_clock.advance(60)
    get_report(client, event_id)
    for actor in ("viewer-1", "rev-1"):
        assert listing(client, event_id, actor).status_code == 200
    response = listing(client, event_id, "sys-42")
    assert (response.status_code, response.json()) == (403, FORBIDDEN)
    assert listing(client, event_id, None).status_code == 401
    response = listing(client, "evt-nope")
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    # The listing holds no report content.
    [row] = listing(client, event_id).json()
    assert set(row) == {
        "report_id",
        "report_version",
        "report_kind",
        "disposition_id",
        "assembled_at",
        "assembled_by",
        "json_sha256",
        "pdf_sha256",
        "status",
        "submission",
    }


def test_retrieval_by_id_roles_format_and_unknown_id(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "DEATH")
    fake_clock.advance(60)
    report = get_report(client, event_id)
    url = f"/v1/reports/{report['report_id']}"
    assert client.get(url, headers=bearer("rev-1")).json() == report
    pdf = client.get(url + "?format=pdf", headers=bearer("rev-1"))
    assert pdf.headers["content-type"] == "application/pdf"
    assert pdf.content == fetch(client, event_id, "pdf").content
    for actor in ("viewer-1", "sys-42"):
        assert client.get(url, headers=bearer(actor)).status_code == 403
    assert client.get(url).status_code == 401
    bad = client.get(url + "?format=xml", headers=bearer("rev-1"))
    assert (bad.status_code, bad.json()) == (400, MALFORMED_QUERY)
    missing = client.get("/v1/reports/rpt-nope", headers=bearer("rev-1"))
    assert (missing.status_code, missing.json()) == (404, NOT_FOUND)
    before = ledger_rows(owner_engine)
    client.get(url, headers=bearer("rev-1"))
    assert ledger_rows(owner_engine) == before  # reading by id appends nothing


def test_an_erased_record_is_410_and_still_lists_its_reports(
    client: ApiClient, fake_clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    event_id = disposed_event(client, fake_clock, "DEATH")
    fake_clock.advance(60)
    report = get_report(client, event_id)
    monkeypatch.setattr(personal_data, "is_erased", lambda session, event_id: True)
    for url in (f"/v1/events/{event_id}/report", f"/v1/reports/{report['report_id']}"):
        response = client.get(url, headers=bearer("rev-1"))
        assert (response.status_code, response.json()) == (410, ERASED)
    [row] = listing(client, event_id).json()
    assert row["report_id"] == report["report_id"] and row["json_sha256"]
