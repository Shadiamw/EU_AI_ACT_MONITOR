"""POST /v1/events/{event_id}/report/submission (Phase 5 spec 3.7, D6; INV-15): a reviewer marks
a report as submitted. Recording only; nothing is transmitted (C15)."""

from typing import Any

import pytest
from sqlalchemy import Engine

from tests.api.conftest import FORBIDDEN, MALFORMED, NOT_FOUND, UNAUTHORIZED, ApiClient
from tests.db_helpers import bearer
from tests.reports.conftest import (
    dispose,
    disposed_event,
    fetch,
    get_report,
    ledger_rows,
    listing,
    submit,
    table_rows,
)
from tests.worker.conftest import FakeClock


def error(response: Any, status: int, code: str) -> None:
    assert (response.status_code, response.json()["error_code"]) == (status, code), response.text


@pytest.fixture
def ready(client: ApiClient, fake_clock: FakeClock) -> tuple[str, str, str]:
    """An event with an assembled DEATH report: (event_id, report_id, json_sha256)."""
    event_id = disposed_event(client, fake_clock, "DEATH")
    fake_clock.advance(3600)
    report = get_report(client, event_id)
    return event_id, report["report_id"], fetch(client, event_id).headers["x-report-sha256"]


def test_submission_records_reviewer_time_and_hash(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    ready: tuple[str, str, str],
) -> None:
    event_id, report_id, sha = ready
    fake_clock.advance(120)
    response = submit(
        client,
        event_id,
        report_id,
        sha,
        "Initial",
        expected_next_report_date="2026-10-07",
        authority_reference="MSA-2026-17",
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "submission_id": body["submission_id"],
        "report_id": report_id,
        "event_id": event_id,
        "submitted_by": "rev-1",
        "submitted_at": "2026-09-27T15:07:00Z",
        "submission_kind": "INITIAL",
        "report_type": "Initial",
        "expected_next_report_date": "2026-10-07",
        "authority_reference": "MSA-2026-17",
    }
    assert body["submission_id"].startswith("sub-")
    [row] = table_rows(owner_engine, "report_submission")
    assert row["submitted_by"] == "rev-1" and row["report_id"] == report_id
    [entry] = [e for e in ledger_rows(owner_engine) if e["entry_type"] == "REPORT_SUBMITTED"]
    assert (entry["actor_id"], entry["subject_id"]) == ("rev-1", event_id)
    assert entry["payload"] == {
        "event_id": event_id,
        "report_id": report_id,
        "submission_id": body["submission_id"],
        "submission_kind": "INITIAL",
        "report_type": "Initial",
        "json_sha256": sha,
        "pdf_sha256": fetch(client, event_id, "pdf").headers["x-report-sha256"],
    }
    # No authority reference and no report content in the ledger.
    assert "MSA-2026-17" not in str(entry["payload"])
    audit = client.get(f"/v1/events/{event_id}/audit", headers=bearer("viewer-1")).json()
    assert "REPORT_SUBMITTED" in [a["action"] for a in audit]
    assert "REPORT_ASSEMBLED" in [a["action"] for a in audit]


def test_a_submission_changes_nothing_on_the_event(
    client: ApiClient, owner_engine: Engine, ready: tuple[str, str, str]
) -> None:
    event_id, report_id, sha = ready
    before = client.get(f"/v1/events/{event_id}", headers=bearer("viewer-1")).json()
    assert submit(client, event_id, report_id, sha).status_code == 201
    after = client.get(f"/v1/events/{event_id}", headers=bearer("viewer-1")).json()
    assert before == after
    assert after["state"] == "DISPOSITIONED"
    assert fetch(client, event_id).headers["x-report-sha256"] == sha  # bytes unchanged


def test_only_the_current_report_can_be_submitted(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    ready: tuple[str, str, str],
) -> None:
    event_id, old_id, old_sha = ready
    fake_clock.advance(60)
    assert dispose(client, event_id, "OTHER_SERIOUS", "reassessed").status_code == 200
    fake_clock.advance(60)
    new = get_report(client, event_id)
    assert new["report_id"] != old_id
    before = (ledger_rows(owner_engine), table_rows(owner_engine, "report_submission"))
    error(submit(client, event_id, old_id, old_sha), 409, "REPORT_NOT_CURRENT")
    assert (ledger_rows(owner_engine), table_rows(owner_engine, "report_submission")) == before
    new_sha = fetch(client, event_id).headers["x-report-sha256"]
    assert submit(client, event_id, new["report_id"], new_sha).status_code == 201


def test_a_report_is_submitted_at_most_once(
    client: ApiClient, owner_engine: Engine, ready: tuple[str, str, str]
) -> None:
    event_id, report_id, sha = ready
    assert submit(client, event_id, report_id, sha).status_code == 201
    before = (ledger_rows(owner_engine), table_rows(owner_engine, "report_submission"))
    error(submit(client, event_id, report_id, sha, "Follow up"), 409, "REPORT_ALREADY_SUBMITTED")
    assert (ledger_rows(owner_engine), table_rows(owner_engine, "report_submission")) == before


def test_submission_after_a_revision_is_a_follow_up(
    client: ApiClient, fake_clock: FakeClock, ready: tuple[str, str, str]
) -> None:
    event_id, report_id, sha = ready
    assert submit(client, event_id, report_id, sha, "Initial").status_code == 201
    fake_clock.advance(60)
    assert (
        dispose(client, event_id, "OTHER_SERIOUS", "reassessed").status_code == 200
    )  # revised: not blocked
    fake_clock.advance(60)
    second = get_report(client, event_id)
    second_sha = fetch(client, event_id).headers["x-report-sha256"]
    response = submit(client, event_id, second["report_id"], second_sha, "Follow up")
    assert response.status_code == 201, response.text
    assert response.json()["submission_kind"] == "FOLLOW_UP"
    assert [r["status"] for r in listing(client, event_id).json()] == [
        "SUBMITTED_SUPERSEDED",
        "SUBMITTED",
    ]


def test_closure_report_cannot_be_submitted(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = disposed_event(client, fake_clock, "NOT_SERIOUS", rationale="seasonal shift")
    fake_clock.advance(60)
    report = get_report(client, event_id)
    sha = fetch(client, event_id).headers["x-report-sha256"]
    before = ledger_rows(owner_engine)
    for report_type in ("Initial", "Combined initial and final"):
        error(
            submit(client, event_id, report["report_id"], sha, report_type), 409, "NOT_SUBMITTABLE"
        )
    assert ledger_rows(owner_engine) == before
    assert table_rows(owner_engine, "report_submission") == []


def test_submission_hash_mismatch_is_409(
    client: ApiClient, owner_engine: Engine, ready: tuple[str, str, str]
) -> None:
    event_id, report_id, sha = ready
    before = ledger_rows(owner_engine)
    error(submit(client, event_id, report_id, "0" * 64), 409, "REPORT_HASH_MISMATCH")
    error(submit(client, event_id, report_id, sha.upper()), 409, "REPORT_HASH_MISMATCH")
    # The PDF hash is not the JSON hash.
    pdf_sha = fetch(client, event_id, "pdf").headers["x-report-sha256"]
    error(submit(client, event_id, report_id, pdf_sha), 409, "REPORT_HASH_MISMATCH")
    assert ledger_rows(owner_engine) == before


@pytest.mark.parametrize(
    "report_type",
    ["Initial", "Follow up", "Combined initial and final", "Final (Reportable incident)"],
)
def test_submission_report_type_is_one_of_four_template_values(
    client: ApiClient, owner_engine: Engine, ready: tuple[str, str, str], report_type: str
) -> None:
    event_id, report_id, sha = ready
    response = submit(client, event_id, report_id, sha, report_type)
    assert response.status_code == 201, response.text
    assert response.json()["report_type"] == report_type
    [row] = table_rows(owner_engine, "report_submission")
    assert row["report_type"] == report_type


@pytest.mark.parametrize(
    "extra",
    [
        {"report_type": "Final (Non-reportable incident)"},
        {"report_type": "initial"},
        {"report_type": "Other"},
        {"report_type": None},
        {"report_type": 3},
        {"report_type": "Combined initial and final", "expected_next_report_date": "2026-10-07"},
        {"report_type": "Final (Reportable incident)", "expected_next_report_date": "2026-10-07"},
        {"expected_next_report_date": "2026-13-40"},
        {"expected_next_report_date": "27/09/2026"},
        {"expected_next_report_date": 20261007},
        {"authority_reference": 7},
        {"authority_reference": "x" * 201},
        {"authority_reference": "bad\x00ref"},
        {"unknown_field": 1},
        {"report_id": 5},
        {"json_sha256": None},
    ],
)
def test_a_bad_submission_body_is_400_and_writes_nothing(
    client: ApiClient,
    owner_engine: Engine,
    ready: tuple[str, str, str],
    extra: dict[str, Any],
) -> None:
    event_id, report_id, sha = ready
    before = ledger_rows(owner_engine)
    body: dict[str, Any] = {"report_id": report_id, "json_sha256": sha, "report_type": "Initial"}
    response = client.request(
        "POST",
        f"/v1/events/{event_id}/report/submission",
        headers=bearer("rev-1"),
        json=body | extra,
    )
    assert (response.status_code, response.json()) == (400, MALFORMED), extra
    assert ledger_rows(owner_engine) == before
    assert table_rows(owner_engine, "report_submission") == []


@pytest.mark.parametrize("content", [b"", b"[]", b"null", b"not json", b'{"report_id": "x"}'])
def test_a_body_that_is_not_a_full_object_is_400(
    client: ApiClient, ready: tuple[str, str, str], content: bytes
) -> None:
    event_id, _, _ = ready
    response = client.request(
        "POST",
        f"/v1/events/{event_id}/report/submission",
        headers={**bearer("rev-1"), "Content-Type": "application/json"},
        content=content,
    )
    assert (response.status_code, response.json()) == (400, MALFORMED)


def test_the_expected_next_report_date_is_stored_for_a_follow_up(
    client: ApiClient, owner_engine: Engine, ready: tuple[str, str, str]
) -> None:
    event_id, report_id, sha = ready
    response = submit(client, event_id, report_id, sha, "Follow up", expected_next_report_date=None)
    assert response.status_code == 201
    assert response.json()["expected_next_report_date"] is None
    assert response.json()["authority_reference"] is None


def test_submission_roles(
    client: ApiClient, owner_engine: Engine, ready: tuple[str, str, str]
) -> None:
    event_id, report_id, sha = ready
    before = ledger_rows(owner_engine)
    for actor in ("viewer-1", "sys-42"):
        response = submit(client, event_id, report_id, sha, actor=actor)
        assert (response.status_code, response.json()) == (403, FORBIDDEN)
    response = submit(client, event_id, report_id, sha, actor=None)
    assert (response.status_code, response.json()) == (401, UNAUTHORIZED)
    # Authentication comes before the body is read: a broken body is still 403 for a viewer.
    response = client.request(
        "POST",
        f"/v1/events/{event_id}/report/submission",
        headers=bearer("viewer-1"),
        content=b"not json",
    )
    assert response.status_code == 403
    assert ledger_rows(owner_engine) == before


def test_unknown_event_or_a_report_of_another_event_is_404(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, ready: tuple[str, str, str]
) -> None:
    event_id, report_id, sha = ready
    before = ledger_rows(owner_engine)
    for target, rid in (("evt-nope", report_id), (event_id, "rpt-nope")):
        response = submit(client, target, rid, sha)
        assert (response.status_code, response.json()) == (404, NOT_FOUND)
    # A report that belongs to a different event is also 404.
    from tests.part1.conftest import post

    other = post(client, metric_name="accuracy", metric_type="performance", value=0.5)
    other_id = other.json()["flagged_event_id"]
    fake_clock.advance(60)
    assert dispose(client, other_id, "DEATH").status_code == 200
    response = submit(client, other_id, report_id, sha)
    assert (response.status_code, response.json()) == (404, NOT_FOUND)
    assert [e for e in ledger_rows(owner_engine) if e["entry_type"] == "REPORT_SUBMITTED"] == []
    assert len(ledger_rows(owner_engine)) > len(before) - 1


def test_a_submission_of_an_erased_record_is_410(
    client: ApiClient,
    ready: tuple[str, str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app import personal_data

    event_id, report_id, sha = ready
    monkeypatch.setattr(personal_data, "is_erased", lambda session, event_id: True)
    error(submit(client, event_id, report_id, sha), 410, "RECORD_ERASED")
