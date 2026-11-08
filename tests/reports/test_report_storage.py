"""INV-14: a report is hashed and stored exactly as produced, rendered once, never again
(Phase 5 spec 3.4, 3.5, 3.8, D4, D19)."""

import hashlib
from contextlib import contextmanager
from io import BytesIO
from typing import Any

import pytest
from pypdf import PdfReader
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import personal_data, report_render, reports
from tests.api.conftest import ApiClient
from tests.reports.conftest import (
    disposed_event,
    fetch,
    get_report,
    ledger_rows,
    submit,
    table_rows,
)
from tests.worker.conftest import FakeClock


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def stored_row(owner_engine: Engine, event_id: str) -> dict[str, Any]:
    [row] = [r for r in table_rows(owner_engine, "report") if r["event_id"] == event_id]
    return row


def plain(owner_engine: Engine, row: dict[str, Any], column: str) -> bytes:
    with Session(owner_engine) as session:
        opened = personal_data.open_bytes(session, row["event_id"], f"report.{column}", row[column])
    assert opened is not None
    return bytes(opened)


@pytest.fixture
def event(client: ApiClient, fake_clock: FakeClock) -> str:
    event_id = disposed_event(client, fake_clock, "DEATH")
    fake_clock.advance(3600)
    return event_id


def test_report_bytes_are_served_exactly_as_stored(
    client: ApiClient, owner_engine: Engine, event: str
) -> None:
    json_response = fetch(client, event)
    pdf_response = fetch(client, event, "pdf")
    row = stored_row(owner_engine, event)
    assert json_response.content == plain(owner_engine, row, "json_bytes")
    assert pdf_response.content == plain(owner_engine, row, "pdf_bytes")
    assert json_response.headers["content-type"] == "application/json"
    assert pdf_response.headers["content-type"] == "application/pdf"
    assert pdf_response.content.startswith(b"%PDF-")
    for response, digest in (
        (json_response, row["json_sha256"]),
        (pdf_response, row["pdf_sha256"]),
    ):
        assert sha256(response.content) == digest
        assert response.headers["x-report-sha256"] == digest
        assert response.headers["etag"] == f'"{digest}"'
        assert response.headers["x-report-id"] == row["report_id"]
        assert response.headers["x-report-version"] == "1"


def test_the_json_has_no_trailing_newline_and_keeps_the_field_order(
    client: ApiClient, event: str
) -> None:
    content = fetch(client, event).content
    assert not content.endswith(b"\n")
    order = list(get_report(client, event))
    assert order == [
        "report_id",
        "report_version",
        "report_kind",
        "schema_version",
        "event",
        "harm_fields",
        "determination",
        "timestamps",
        "risk_indicator",
        "regulation",
        "submission_marking",
        "article73_template",
        "ledger_anchor",
    ]


def test_report_hashes_are_in_the_ledger(
    client: ApiClient, owner_engine: Engine, event: str
) -> None:
    report = get_report(client, event)
    row = stored_row(owner_engine, event)
    [entry] = [e for e in ledger_rows(owner_engine) if e["entry_type"] == "REPORT_ASSEMBLED"]
    assert entry["actor_id"] == "rev-1"
    assert entry["subject_id"] == event
    assert entry["payload"] == {
        "event_id": event,
        "report_id": row["report_id"],
        "report_version": 1,
        "report_kind": "SERIOUS_INCIDENT",
        "disposition_id": report["determination"]["disposition_id"],
        "json_sha256": row["json_sha256"],
        "pdf_sha256": row["pdf_sha256"],
        "assembled_at": report["timestamps"]["assembled_at"],
        "ledger_anchor_seq": report["ledger_anchor"]["seq"],
    }
    # The anchor is the ledger head just before the entry that records the report.
    previous = [e for e in ledger_rows(owner_engine) if e["seq"] == entry["seq"] - 1]
    assert previous[0]["seq"] == report["ledger_anchor"]["seq"]
    assert previous[0]["hash"] == report["ledger_anchor"]["hash"]
    assert entry["recorded_at"].startswith(report["timestamps"]["assembled_at"][:19])


def test_pdf_and_json_are_produced_once_and_never_regenerated(
    client: ApiClient, event: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = {"json": 0, "pdf": 0}
    real_json, real_pdf = report_render.render_json, report_render.render_pdf

    def count_json(report: dict[str, Any]) -> bytes:
        calls["json"] += 1
        return real_json(report)

    def count_pdf(report: dict[str, Any], json_sha256: str | None = None) -> bytes:
        calls["pdf"] += 1
        return real_pdf(report, json_sha256)

    monkeypatch.setattr(report_render, "render_json", count_json)
    monkeypatch.setattr(report_render, "render_pdf", count_pdf)
    bodies = [fetch(client, event, fmt).content for fmt in (None, "pdf", None, "pdf", "json")]
    assert calls == {"json": 1, "pdf": 1}
    assert bodies[0] == bodies[2] == bodies[4]
    assert bodies[1] == bodies[3]


def test_pdf_contains_every_required_field(client: ApiClient, event: str) -> None:
    report = get_report(client, event)
    pages = [
        p.extract_text() for p in PdfReader(BytesIO(fetch(client, event, "pdf").content)).pages
    ]
    printed = "\n".join(pages)
    flat = " ".join(printed.split())
    assert event in flat
    for name in report["harm_fields"]:
        assert name in flat
    assert flat.count("NOT_APPLICABLE") >= 4
    assert report["determination"]["tier_label"] in flat
    for stamp in report["timestamps"].values():
        if stamp is not None:
            assert stamp in flat
    assert report["determination"]["deadline_utc"] in flat
    assert report["report_id"] in flat
    assert fetch(client, event).headers["x-report-sha256"] in flat  # printed under provenance
    # Every template field is listed by number.
    for field in report["article73_template"]["fields"]:
        assert field["field"] in flat


def test_renderer_is_reproducible_in_invariant_mode(client: ApiClient, event: str) -> None:
    """Test-only (spec 3.4 item 5): the same report dict gives the same PDF bytes. The stored
    bytes remain the record either way."""
    report = get_report(client, event)
    first = report_render.render_pdf(report, "a" * 64)
    assert first == report_render.render_pdf(report, "a" * 64)
    assert first != report_render.render_pdf(report, "b" * 64)


def test_reading_a_stored_report_appends_nothing(
    client: ApiClient, owner_engine: Engine, event: str
) -> None:
    fetch(client, event)
    before = ledger_rows(owner_engine)
    for _ in range(3):
        assert fetch(client, event).status_code == 200
        assert fetch(client, event, "pdf").status_code == 200
    assert ledger_rows(owner_engine) == before
    assert len(table_rows(owner_engine, "report")) == 1


def test_the_report_has_one_ledger_entry_and_the_chain_verifies(
    client: ApiClient, owner_engine: Engine, event: str
) -> None:
    from app.ledger import verify_ledger

    fetch(client, event)
    fetch(client, event)
    types = [e["entry_type"] for e in ledger_rows(owner_engine) if e["subject_id"] == event]
    assert types.count("REPORT_ASSEMBLED") == 1
    assert types[-1] == "REPORT_ASSEMBLED"
    with Session(owner_engine) as session:
        assert verify_ledger(session)["valid"]


@contextmanager
def triggers_off(owner_engine: Engine) -> Any:
    with owner_engine.begin() as conn:
        conn.execute(text("ALTER TABLE report DISABLE TRIGGER report_block_update_delete"))
        yield conn
        conn.execute(text("ALTER TABLE report ENABLE TRIGGER report_block_update_delete"))


def test_verify_stored_is_clean_then_finds_a_changed_byte(
    client: ApiClient, owner_engine: Engine, event: str
) -> None:
    fetch(client, event)
    with Session(owner_engine) as session:
        assert reports.verify_stored(session) == []
    row = stored_row(owner_engine, event)
    with triggers_off(owner_engine) as conn:
        conn.execute(
            text("UPDATE report SET pdf_bytes = :b WHERE report_id = :id"),
            {"b": b"%PDF-" + bytes(row["pdf_bytes"])[5:-1] + b"X", "id": row["report_id"]},
        )
    with Session(owner_engine) as session:
        assert reports.verify_stored(session) == [
            {"report_id": row["report_id"], "which": "pdf_bytes"}
        ]


def test_verify_stored_finds_a_changed_hash_or_ledger_payload(
    client: ApiClient, owner_engine: Engine, event: str
) -> None:
    fetch(client, event)
    row = stored_row(owner_engine, event)
    with triggers_off(owner_engine) as conn:
        conn.execute(
            text("UPDATE report SET json_sha256 = :h WHERE report_id = :id"),
            {"h": "0" * 64, "id": row["report_id"]},
        )
    with Session(owner_engine) as session:
        found = reports.verify_stored(session)
    assert {"report_id": row["report_id"], "which": "json_bytes"} in found
    assert {"report_id": row["report_id"], "which": "ledger_payload"} in found


@pytest.mark.parametrize("table", ["report", "report_submission"])
def test_report_tables_are_insert_only(
    client: ApiClient,
    owner_engine: Engine,
    app_engine: Engine,
    event: str,
    table: str,
) -> None:
    report = get_report(client, event)
    sha = fetch(client, event).headers["x-report-sha256"]
    assert submit(client, event, report["report_id"], sha).status_code == 201
    assert len(table_rows(owner_engine, table)) == 1
    statements = (f"UPDATE {table} SET report_id = report_id", f"DELETE FROM {table}")
    for engine in (app_engine, owner_engine):
        for statement in statements:
            with pytest.raises(DBAPIError), engine.begin() as conn:
                conn.execute(text(statement))
    # Even a statement that matches no row is blocked by the trigger (as the owner).
    with pytest.raises(DBAPIError), owner_engine.begin() as conn:
        conn.execute(text(f"DELETE FROM {table} WHERE false"))
    assert len(table_rows(owner_engine, table)) == 1


def test_a_second_report_for_one_disposition_is_refused(
    client: ApiClient, owner_engine: Engine, event: str
) -> None:
    fetch(client, event)
    row = stored_row(owner_engine, event)
    with pytest.raises(DBAPIError), owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO report (report_id, event_id, report_version, disposition_id,"
                " report_kind, schema_version, json_bytes, pdf_bytes, json_sha256, pdf_sha256,"
                " assembled_at, assembled_by) VALUES ('rpt-dup', :e, 2, :d, 'SERIOUS_INCIDENT',"
                " 'pod-h-report-1', '\\x00', '\\x00', :h, :h, now(), 'rev-1')"
            ),
            {"e": event, "d": row["disposition_id"], "h": "0" * 64},
        )
