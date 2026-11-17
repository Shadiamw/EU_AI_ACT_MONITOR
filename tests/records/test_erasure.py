"""AC-18 and INV-10/INV-11 (Phase 5 spec 4.4, 4.5, 5.3 step 4): erasure after the retention
period destroys the record key, the personal data becomes unreadable, and the ledger chain is
untouched. Real database, fake clock, the real app."""

import json
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import personal_data
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer
from tests.records.conftest import (
    decided,
    erase,
    fetch,
    flag,
    ledger_rows,
    rows,
    submit,
)
from tests.script_helpers import run_subprocess
from tests.worker.conftest import FakeClock, Receiver

EVIDENCE = "Applicant Jane Example was refused a loan"
RATIONALE = "Raised by Jane Example's complaint"
REVISION = "Reassessed after the second complaint"


@dataclass
class Journey:
    event_id: str
    retain_until: Any
    report_id: str


def _export(client: ApiClient) -> list[dict[str, Any]]:
    response = client.get("/v1/ledger/export", headers=bearer("viewer-1"))
    assert response.status_code == 200
    entries: list[dict[str, Any]] = response.json()
    return entries


def _verify(client: ApiClient) -> dict[str, Any]:
    result: dict[str, Any] = client.get("/v1/ledger/verify", headers=bearer("viewer-1")).json()
    return result


def _event(client: ApiClient, event_id: str) -> Any:
    return client.get(f"/v1/events/{event_id}", headers=bearer("viewer-1"))


@pytest.fixture
def journey(
    client: ApiClient, fake_clock: FakeClock, receiver: Receiver, owner_engine: Engine
) -> Journey:
    """4.5 step 1: a webhook, a breach, OTHER_SERIOUS with Jane's evidence and rationale, a
    revision to DEATH, the report assembled and submitted."""
    webhook = client.request(
        "POST",
        "/v1/webhooks",
        headers=bearer("rev-1"),
        json={"url": receiver.url, "event_types": ["flag_notification"]},
    )
    assert webhook.status_code == 201, webhook.text
    event_id = flag(client)
    assert _event(client, event_id).status_code == 200  # the reviewer view
    fake_clock.advance(3600)
    harm = decided()
    harm["fundamental_rights_infringement"] = {"status": "CONFIRMED", "evidence": EVIDENCE}
    first = client.request(
        "POST",
        f"/v1/events/{event_id}/disposition",
        headers=bearer("rev-1"),
        json={"tier": "OTHER_SERIOUS", "rationale": RATIONALE, "harm_fields": harm},
    )
    assert first.status_code == 200, first.text
    fake_clock.advance(3600)
    revised = client.request(
        "POST",
        f"/v1/events/{event_id}/disposition",
        headers=bearer("rev-1"),
        json={"tier": "DEATH", "rationale": REVISION, "harm_fields": harm},
    )
    assert revised.status_code == 200, revised.text
    fake_clock.advance(3600)
    served = fetch(client, event_id)
    assert served.status_code == 200
    report_id = served.headers["x-report-id"]
    sent = submit(client, event_id, report_id, served.headers["x-report-sha256"])
    assert sent.status_code == 201, sent.text
    [row] = rows(owner_engine, "SELECT retain_until FROM event WHERE event_id = :e", e=event_id)
    return Journey(event_id, row["retain_until"], report_id)


def _stored_text(owner_engine: Engine, event_id: str) -> list[str]:
    """Every stored value of the event, its harm fields, dispositions, reports and ledger
    entries, as text (bytes decoded leniently), to search for a plain-text name."""
    values: list[Any] = []
    for sql in (
        "SELECT * FROM event WHERE event_id = :e",
        "SELECT * FROM harm_field WHERE event_id = :e",
        "SELECT * FROM disposition WHERE event_id = :e",
        "SELECT * FROM report WHERE event_id = :e",
        "SELECT * FROM report_submission WHERE event_id = :e",
        "SELECT payload FROM ledger_entry WHERE subject_id = :e",
    ):
        for row in rows(owner_engine, sql, e=event_id):
            values.extend(row.values())
    texts = []
    for value in values:
        if isinstance(value, bytes | memoryview):
            texts.append(bytes(value).decode("latin-1"))
        else:
            texts.append(json.dumps(value, default=str, ensure_ascii=False))
    return texts


def test_ac18_personal_data_is_unreadable_after_erasure(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine, journey: Journey
) -> None:
    event_id = journey.event_id
    # Step 2: readable before. The API shows the text; SQL shows only sealed values.
    before = _event(client, event_id).json()
    assert before["harm_fields"]["fundamental_rights_infringement"]["evidence"] == EVIDENCE
    assert before["disposition"]["rationale"] == REVISION
    assert b"Jane Example" in fetch(client, event_id).content
    [first, second] = rows(
        owner_engine,
        "SELECT rationale FROM disposition WHERE event_id = :e ORDER BY seq_in_event",
        e=event_id,
    )
    for sealed in (first["rationale"], second["rationale"]):
        assert sealed.startswith("pd1:") and "Jane" not in sealed
    payloads = [
        e["payload"]["rationale"]
        for e in rows(
            owner_engine,
            "SELECT payload FROM ledger_entry WHERE subject_id = :e"
            " AND entry_type LIKE 'DISPOSITION_%' ORDER BY seq",
            e=event_id,
        )
    ]
    assert payloads == [first["rationale"], second["rationale"]]  # one sealed string, two homes
    with Session(owner_engine) as session:
        assert (
            personal_data.open(session, event_id, "disposition.rationale", first["rationale"])
            == RATIONALE
        )
    # Step 4: past retain_until, erasure succeeds.
    fake_clock.set(journey.retain_until)
    response = erase(client, event_id)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"event_id", "erased_at", "erased_fields"}
    assert body["event_id"] == event_id
    assert {
        "disposition.rationale",
        "ledger_entry.payload.rationale",
        "harm_field.evidence",
        "report.json_bytes",
        "report.pdf_bytes",
    } <= set(body["erased_fields"])
    # Step 5: unreadable after.
    after = _event(client, event_id)
    assert after.status_code == 200
    detail = after.json()
    assert all(field["evidence"] is None for field in detail["harm_fields"].values())
    assert detail["disposition"]["rationale"] is None
    assert detail["disposition"]["tier"] == "DEATH"  # everything else is unchanged
    assert detail["state"] == before["state"] and detail["detected_at"] == before["detected_at"]
    for path in (f"/v1/events/{event_id}/report", f"/v1/reports/{journey.report_id}"):
        gone = client.get(path, headers=bearer("rev-1"))
        assert gone.status_code == 410
        assert gone.json()["error_code"] == "RECORD_ERASED"
    assert (
        client.get(
            f"/v1/reports/{journey.report_id}?format=pdf", headers=bearer("rev-1")
        ).status_code
        == 410
    )
    listed = client.get(f"/v1/events/{event_id}/reports", headers=bearer("viewer-1"))
    assert listed.status_code == 200 and len(listed.json()) == 1
    assert (
        "RECORD_ERASED"
        in client.get(f"/v1/events/{event_id}/audit", headers=bearer("viewer-1")).text
    )
    [key] = rows(owner_engine, "SELECT * FROM record_key WHERE subject_id = :e", e=event_id)
    assert key["data_key"] is None and key["destroyed_at"] is not None
    with Session(owner_engine) as session:
        for sealed in (first["rationale"], second["rationale"]):
            assert personal_data.open(session, event_id, "disposition.rationale", sealed) is None
        [report_row] = rows(owner_engine, "SELECT * FROM report WHERE event_id = :e", e=event_id)
        assert (
            personal_data.open_bytes(
                session, event_id, "report.json_bytes", report_row["json_bytes"]
            )
            is None
        )
    assert not [t for t in _stored_text(owner_engine, event_id) if "Jane" in t]
    [erased] = rows(owner_engine, "SELECT erased_at FROM event WHERE event_id = :e", e=event_id)
    assert erased["erased_at"] is not None


def test_ac18_chain_positions_and_hashes_are_unchanged_and_verify(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine, journey: Journey
) -> None:
    before = _export(client)
    assert _verify(client) == {
        "valid": True,
        "entries_checked": len(before),
        "first_mismatch": None,
    }
    fake_clock.set(journey.retain_until)
    erased = erase(client, journey.event_id)
    assert erased.status_code == 200
    after = _export(client)
    assert len(after) == len(before) + 1
    assert after[: len(before)] == before  # same seq, same hash, same sealed payload
    last = after[-1]
    assert last["entry_type"] == "RECORD_ERASED" and last["subject_id"] == journey.event_id
    assert last["actor_id"] == "rev-1"
    assert last["payload"] == {
        "event_id": journey.event_id,
        "erased_at": erased.json()["erased_at"],
        "retain_until": last["payload"]["retain_until"],
        "key_destroyed": True,
        "erased_fields": erased.json()["erased_fields"],
    }
    assert _verify(client) == {
        "valid": True,
        "entries_checked": len(before) + 1,
        "first_mismatch": None,
    }
    # No value of the new entry is personal data.
    assert "Jane" not in json.dumps(last)
    assert ledger_rows(owner_engine)[-1]["entry_type"] == "RECORD_ERASED"


def test_ac18_standalone_verifier_agrees_after_erasure(
    client: ApiClient, fake_clock: FakeClock, journey: Journey, tmp_path: Path
) -> None:
    fake_clock.set(journey.retain_until)
    assert erase(client, journey.event_id).status_code == 200
    export = _export(client)
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export), encoding="utf-8")
    run = run_subprocess([str(path)])
    assert run.code == 0
    assert run.result == {"valid": True, "entries_checked": len(export), "first_mismatch": None}


def test_erasure_before_retain_until_is_409_and_changes_nothing(
    client: ApiClient, owner_engine: Engine, journey: Journey
) -> None:
    before = {
        "ledger": ledger_rows(owner_engine),
        "key": rows(owner_engine, "SELECT * FROM record_key"),
        "harm": rows(owner_engine, "SELECT * FROM harm_field ORDER BY event_id, field_key"),
        "event": rows(owner_engine, "SELECT * FROM event"),
    }
    response = erase(client, journey.event_id)
    assert (response.status_code, response.json()) == (
        409,
        {
            "error_code": "RETENTION_NOT_EXPIRED",
            "message": "the retention period of this record has not ended",
        },
    )
    assert before == {
        "ledger": ledger_rows(owner_engine),
        "key": rows(owner_engine, "SELECT * FROM record_key"),
        "harm": rows(owner_engine, "SELECT * FROM harm_field ORDER BY event_id, field_key"),
        "event": rows(owner_engine, "SELECT * FROM event"),
    }
    assert not [e for e in ledger_rows(owner_engine) if e["entry_type"] == "RECORD_ERASED"]
    assert fetch(client, journey.event_id).status_code == 200  # still readable


def test_nothing_is_erased_before_retain_until(
    client: ApiClient, fake_clock: FakeClock, journey: Journey
) -> None:
    """INV-10, 5.3 step 4: at 5 months 29 days and one second before retain_until it is 409;
    at retain_until itself it is allowed."""
    for moment in (
        fake_clock.now(),  # shortly after the breach
        journey.retain_until - timedelta(days=1),
        journey.retain_until - timedelta(seconds=1),
    ):
        fake_clock.set(moment)
        assert erase(client, journey.event_id).status_code == 409
    fake_clock.set(journey.retain_until)
    assert erase(client, journey.event_id).status_code == 200


def test_erasure_twice_is_409(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine, journey: Journey
) -> None:
    fake_clock.set(journey.retain_until)
    assert erase(client, journey.event_id).status_code == 200
    count = len(ledger_rows(owner_engine))
    again = erase(client, journey.event_id)
    assert (again.status_code, again.json()["error_code"]) == (409, "ALREADY_ERASED")
    assert len(ledger_rows(owner_engine)) == count


def test_erasure_roles_body_and_unknown_event(
    client: ApiClient, fake_clock: FakeClock, owner_engine: Engine, journey: Journey
) -> None:
    fake_clock.set(journey.retain_until)
    count = len(ledger_rows(owner_engine))
    assert erase(client, journey.event_id, actor=None).status_code == 401
    assert erase(client, journey.event_id, actor="viewer-1").status_code == 403
    assert erase(client, journey.event_id, actor="sys-42").status_code == 403
    path = f"/v1/events/{journey.event_id}/erasure"
    for content in (b"[]", b'{"x": 1}', b"not json", b'"s"'):
        response = client.request(
            "POST",
            path,
            headers={**bearer("rev-1"), "Content-Type": "application/json"},
            content=content,
        )
        assert (response.status_code, response.json()["error_code"]) == (400, "MALFORMED_BODY")
    missing = erase(client, "evt-unknown")
    assert (missing.status_code, missing.json()["error_code"]) == (404, "NOT_FOUND")
    assert len(ledger_rows(owner_engine)) == count
    # An empty body is accepted like {}.
    empty = client.request("POST", path, headers=bearer("rev-1"))
    assert empty.status_code == 200
