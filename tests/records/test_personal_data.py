"""Sealing (Phase 5 spec 4.2 Design B, 4.3): AES-256-GCM under a per-event key, destroyed by
erasure. Real database, real record_key rows."""

import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import personal_data, reports
from app.formats import api_time
from app.personal_data import KeyDestroyedError, SealError
from tests.api.conftest import ApiClient
from tests.records.conftest import disposed_event, erase, fetch, post, retain_until, rows
from tests.reports.conftest import dispose
from tests.worker.conftest import FakeClock

WHEN = datetime(2027, 1, 1, tzinfo=UTC)


@pytest.fixture
def session(owner_engine: Engine, seeded: Any) -> Any:
    with Session(owner_engine) as s:
        yield s
        s.rollback()


def test_seal_and_open_round_trip(session: Session) -> None:
    sealed = personal_data.seal(session, "evt-a", "disposition.rationale", "Jane Example")
    assert sealed.startswith("pd1:") and "Jane" not in sealed
    assert personal_data.open(session, "evt-a", "disposition.rationale", sealed) == "Jane Example"
    data = b"%PDF-1.4 secret \x00\xff"
    blob = personal_data.seal_bytes(session, "evt-a", "report.pdf_bytes", data)
    assert blob.startswith(b"pd1:") and data not in blob
    assert personal_data.open_bytes(session, "evt-a", "report.pdf_bytes", blob) == data
    # Non-ASCII text and the empty string survive.
    for value in ("Zoë — 名前", ""):
        s = personal_data.seal(session, "evt-a", "f", value)
        assert personal_data.open(session, "evt-a", "f", s) == value


def test_sealed_values_differ_for_the_same_text(session: Session) -> None:
    first = personal_data.seal(session, "evt-a", "f", "same")
    second = personal_data.seal(session, "evt-a", "f", "same")
    assert first != second  # a random nonce each time
    assert personal_data.open(session, "evt-a", "f", first) == "same"
    assert personal_data.open(session, "evt-a", "f", second) == "same"


def test_one_key_per_event_and_each_event_has_its_own(session: Session) -> None:
    personal_data.seal(session, "evt-a", "f", "x")
    personal_data.seal(session, "evt-a", "g", "y")
    personal_data.seal(session, "evt-b", "f", "x")
    found = session.execute(text("SELECT subject_id, length(data_key) FROM record_key")).all()
    assert sorted((r[0], r[1]) for r in found) == [("evt-a", 32), ("evt-b", 32)]


def test_a_value_cannot_be_opened_for_another_event_or_field(session: Session) -> None:
    sealed = personal_data.seal(session, "evt-a", "f", "secret")
    personal_data.seal(session, "evt-b", "f", "other")  # evt-b has a key too
    with pytest.raises(SealError):
        personal_data.open(session, "evt-b", "f", sealed)
    with pytest.raises(SealError):
        personal_data.open(session, "evt-a", "g", sealed)


def test_a_changed_sealed_value_is_refused(session: Session) -> None:
    sealed = personal_data.seal(session, "evt-a", "f", "secret")
    flipped = sealed[:-2] + ("A" if sealed[-2] != "A" else "B") + sealed[-1]
    with pytest.raises(SealError):
        personal_data.open(session, "evt-a", "f", flipped)
    with pytest.raises(SealError):
        personal_data.open(session, "evt-a", "f", "pd1:!!!not base64!!!")
    blob = bytearray(personal_data.seal_bytes(session, "evt-a", "f", b"secret"))
    blob[-1] ^= 1
    with pytest.raises(SealError):
        personal_data.open_bytes(session, "evt-a", "f", bytes(blob))


def test_open_after_destroy_is_none(session: Session) -> None:
    sealed = personal_data.seal(session, "evt-a", "f", "secret")
    blob = personal_data.seal_bytes(session, "evt-a", "g", b"secret")
    assert personal_data.destroy(session, "evt-a", WHEN) is True
    assert personal_data.open(session, "evt-a", "f", sealed) is None
    assert personal_data.open_bytes(session, "evt-a", "g", blob) is None
    row = session.execute(
        text("SELECT data_key, destroyed_at FROM record_key WHERE subject_id = 'evt-a'")
    ).one()
    assert row.data_key is None and row.destroyed_at == WHEN
    # Destroying again, or an event that never had a key, destroys nothing.
    assert personal_data.destroy(session, "evt-a", WHEN) is False
    assert personal_data.destroy(session, "evt-none", WHEN) is False
    # No new value can be sealed for a destroyed key.
    with pytest.raises(KeyDestroyedError):
        personal_data.seal(session, "evt-a", "f", "again")


def test_open_passes_pre_0009_plain_values_through(session: Session) -> None:
    assert personal_data.open(session, "evt-a", "f", "plain rationale") == "plain rationale"
    assert personal_data.open(session, "evt-a", "f", None) is None
    assert personal_data.open_bytes(session, "evt-a", "f", b'{"plain": true}') == b'{"plain": true}'
    assert personal_data.open_bytes(session, "evt-a", "f", None) is None
    # Nothing was created by opening.
    assert session.execute(text("SELECT count(*) FROM record_key")).scalar_one() == 0


def test_the_destroyed_check_and_app_grants(owner_engine: Engine, app_engine: Engine) -> None:
    with Session(owner_engine) as s:
        personal_data.seal(s, "evt-a", "f", "x")
        s.commit()
    # data_key and destroyed_at must agree (the table check).
    with pytest.raises(DBAPIError), owner_engine.begin() as conn:
        conn.execute(text("UPDATE record_key SET data_key = NULL"))
    # The application role may not delete a key row (it is kept as a tombstone).
    with pytest.raises(DBAPIError), app_engine.begin() as conn:
        conn.execute(text("DELETE FROM record_key"))
    with app_engine.begin() as conn:  # but it may update (destroy) it
        conn.execute(text("UPDATE record_key SET data_key = NULL, destroyed_at = now()"))


def test_report_bytes_are_sealed_at_rest(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine
) -> None:
    """Integration with Part 1: the stored JSON and PDF start with the sealed form, and
    open_bytes returns the bytes the route served."""
    event_id = disposed_event(client, fake_clock, "DEATH", rationale="Raised by Jane Example")
    served_json = fetch(client, event_id).content
    served_pdf = fetch(client, event_id, "pdf").content
    [row] = rows(owner_engine, "SELECT * FROM report WHERE event_id = :e", e=event_id)
    for column in ("json_bytes", "pdf_bytes"):
        stored = bytes(row[column])
        assert stored.startswith(b"pd1:")
        assert not stored.startswith((b"{", b"%PDF"))
        assert b"Jane" not in stored
    with Session(owner_engine) as s:
        assert (
            personal_data.open_bytes(s, event_id, "report.json_bytes", row["json_bytes"])
            == served_json
        )
        assert (
            personal_data.open_bytes(s, event_id, "report.pdf_bytes", row["pdf_bytes"])
            == served_pdf
        )
    assert b"Jane Example" in served_json


# Audit B1 (decision 286): a damaged sealed report is a mismatch, never an exception. The stored
# form is b"pd1:" + nonce (12 bytes) + ciphertext + tag (16 bytes); each case flips one bit of
# one byte, as the owner with report's own trigger disabled.
DAMAGE_OFFSETS = {
    "prefix": 0,
    "nonce": len(personal_data.PREFIX_BYTES),
    "ciphertext": len(personal_data.PREFIX_BYTES) + personal_data.NONCE_LENGTH + 5,
    "tag": -1,
}


def _flip_byte(owner_engine: Engine, report_id: str, column: str, offset: int) -> None:
    with owner_engine.begin() as conn:
        stored = bytearray(
            conn.execute(
                text(f"SELECT {column} FROM report WHERE report_id = :r"), {"r": report_id}
            ).scalar_one()
        )
        stored[offset] ^= 1
        conn.execute(text("ALTER TABLE report DISABLE TRIGGER USER"))
        conn.execute(
            text(f"UPDATE report SET {column} = :b WHERE report_id = :r"),
            {"b": bytes(stored), "r": report_id},
        )
        conn.execute(text("ALTER TABLE report ENABLE TRIGGER USER"))


@pytest.mark.parametrize("part", list(DAMAGE_OFFSETS))
@pytest.mark.parametrize("column", ["json_bytes", "pdf_bytes"])
def test_verify_stored_reports_a_damaged_sealed_report(
    client: ApiClient,
    fake_clock: FakeClock,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    column: str,
    part: str,
) -> None:
    report_ids = {}
    for _ in range(3):  # three events, each with its own sealed report
        fake_clock.advance(600)
        flagged = post(client, value=0.34, timestamp=api_time(fake_clock.now()))
        assert flagged.status_code == 201, flagged.text
        event_id = flagged.json()["flagged_event_id"]
        fake_clock.advance(3600)
        disposed = dispose(client, event_id, "DEATH", "Raised by Jane Example")
        assert disposed.status_code == 200, disposed.text
        fake_clock.advance(3600)
        served = fetch(client, event_id)
        assert served.status_code == 200, served.text
        report_ids[event_id] = served.headers["x-report-id"]
        # A healthy sample ends the breach series, so the next breach is a new event.
        healthy_sample = post(client, value=0.1, timestamp=api_time(fake_clock.now()))
        assert healthy_sample.status_code == 201, healthy_sample.text
    damaged, healthy, erased = report_ids
    assert len({damaged, healthy, erased}) == 3
    with Session(owner_engine) as session:
        assert reports.verify_stored(session) == []  # all healthy

    # The erased report is skipped: its key is destroyed and its bytes are damaged too.
    fake_clock.set(retain_until(owner_engine, erased))
    assert erase(client, erased).status_code == 200
    _flip_byte(owner_engine, report_ids[erased], column, DAMAGE_OFFSETS[part])
    _flip_byte(owner_engine, report_ids[damaged], column, DAMAGE_OFFSETS[part])
    stored = bytes(
        rows(
            owner_engine,
            f"SELECT {column} FROM report WHERE report_id = :r",
            r=report_ids[damaged],
        )[0][column]
    )

    with caplog.at_level(logging.DEBUG), Session(owner_engine) as session:
        found = reports.verify_stored(session)  # no exception escapes
    assert found == [{"report_id": report_ids[damaged], "which": column}]
    # Nothing about the damaged value is logged: no bytes of it, no exception message.
    logged = "\n".join(record.getMessage() for record in caplog.records)
    assert stored.hex()[:24] not in logged
    assert "cannot open" not in logged
    assert "Jane" not in logged
