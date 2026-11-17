"""Sealing (Phase 5 spec 4.2 Design B, 4.3): AES-256-GCM under a per-event key, destroyed by
erasure. Real database, real record_key rows."""

from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app import personal_data
from app.personal_data import KeyDestroyedError, SealError
from tests.api.conftest import ApiClient
from tests.records.conftest import disposed_event, fetch, rows
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
