"""M4: GET /v1/ledger/export and GET /v1/ledger/verify.

Expected values are reasoned out in comments next to each assertion.
"""

import logging
import threading
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app import ledger
from app.ledger import verify_entries, verify_ledger
from tests.api.conftest import (
    ApiClient,
    append_lock_is_free,
    as_actor,
    deliver_notification,
    flag_event,
    owner_rows,
    seed,
)

ENTRY_KEYS = {
    "seq",
    "entry_type",
    "actor_id",
    "subject_id",
    "payload",
    "recorded_at",
    "prev_hash",
    "hash",
}
VERIFY_KEYS = {"valid", "entries_checked", "first_mismatch"}


def export(client: ApiClient, actor: str = "viewer-1") -> list[dict[str, Any]]:
    response = client.get("/v1/ledger/export", headers=as_actor(actor))
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, list)
    return body


def api_verify(client: ApiClient, actor: str = "viewer-1") -> dict[str, Any]:
    response = client.get("/v1/ledger/verify", headers=as_actor(actor))
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    assert set(body) == VERIFY_KEYS
    assert isinstance(body["valid"], bool)
    assert isinstance(body["entries_checked"], int) and not isinstance(
        body["entries_checked"], bool
    )
    assert body["first_mismatch"] is None or isinstance(body["first_mismatch"], str)
    return body


def direct_verify(app_engine: Engine) -> dict[str, Any]:
    with Session(app_engine) as session:
        result = verify_ledger(session)
        session.rollback()
    return dict(result)


def build_ledger(client: ApiClient, app_engine: Engine) -> None:
    """Seven entries: SEED, two flagged events, a delivery and two views."""
    seed(app_engine)  # 1
    flag_event(app_engine, "evt-a")  # 2, 3
    flag_event(app_engine, "evt-b")  # 4, 5
    deliver_notification(app_engine, "evt-a")  # 6
    response = client.get("/v1/events/evt-a", headers=as_actor("viewer-1"))  # 7
    assert response.status_code == 200


# D. Export


def test_export_is_every_entry_with_exactly_eight_fields_in_seq_order(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    build_ledger(client, app_engine)
    exported = export(client)
    # Seven entries, seq 1..7, each with exactly the eight stored fields.
    assert [entry["seq"] for entry in exported] == list(range(1, 8))
    assert all(set(entry) == ENTRY_KEYS for entry in exported)
    for entry in exported:
        assert type(entry["seq"]) is int
        assert entry["subject_id"] is None or isinstance(entry["subject_id"], str)
        assert isinstance(entry["payload"], dict)
    # Field for field, the export is the table as the owner reads it.
    assert exported == owner_rows(owner_engine)
    # SEED has no subject; its payload is the seed payload.
    assert exported[0]["subject_id"] is None
    assert exported[0]["payload"] == {"systems": ["sys-42"], "threshold_count": "2"}


def test_export_of_an_empty_ledger_is_an_empty_array(client: ApiClient) -> None:
    assert export(client) == []


def test_export_writes_nothing_and_its_rows_verify(
    client: ApiClient, app_engine: Engine, owner_engine: Engine
) -> None:
    build_ledger(client, app_engine)
    before = owner_rows(owner_engine)
    exported = export(client, "rev-1")
    assert owner_rows(owner_engine) == before
    # The untampered seven-entry chain verifies from the export alone.
    assert verify_entries(exported) == {"valid": True, "entries_checked": 7, "first_mismatch": None}
    assert append_lock_is_free(owner_engine)


def test_exported_tampered_rows_flag_the_same_seq_as_server_verify(
    client: ApiClient,
    app_engine: Engine,
    owner_tamper: Callable[..., None],
) -> None:
    build_ledger(client, app_engine)
    owner_tamper(
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '\"0.29\"')"
        " WHERE seq = 4"
    )
    # Seq 4 (evt-b's EVENT_FLAGGED) no longer hashes to its stored hash: invalid at 4.
    expected = {"valid": False, "entries_checked": 4, "first_mismatch": "4"}
    assert verify_entries(export(client)) == expected
    assert api_verify(client) == expected


# E. Verify


def test_verify_of_a_valid_chain_and_of_an_empty_ledger(
    client: ApiClient, app_engine: Engine
) -> None:
    # An empty ledger is valid with nothing checked (addendum rule 11).
    assert api_verify(client) == {"valid": True, "entries_checked": 0, "first_mismatch": None}
    build_ledger(client, app_engine)
    assert api_verify(client, "rev-1") == {
        "valid": True,
        "entries_checked": 7,
        "first_mismatch": None,
    }


def test_ac06_payload_tamper_through_the_api(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    owner_tamper: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
) -> None:
    build_ledger(client, app_engine)
    flagged = [r for r in owner_rows(owner_engine) if r["entry_type"] == "EVENT_FLAGGED"][0]
    k = flagged["seq"]  # evt-a's EVENT_FLAGGED, seq 2
    recorded_hash = flagged["hash"]
    owner_tamper(
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '\"0.29\"')"
        f" WHERE seq = {k}"
    )
    after = [r for r in owner_rows(owner_engine) if r["seq"] == k][0]
    assert after["hash"] == recorded_hash  # the hash was left as recorded
    caplog.set_level(logging.INFO, logger="ledger")
    body = api_verify(client)
    # Seq 1 passes; seq k's payload no longer matches its hash: invalid at k after k entries.
    assert body == {"valid": False, "entries_checked": k, "first_mismatch": str(k)}
    assert body == direct_verify(app_engine)
    # The M3 log format is unchanged when verify runs through the API (decision 21).
    assert f"ledger verify mismatch: seq={k} check=hash" in caplog.messages


@pytest.mark.parametrize(
    ("deleted", "expected"),
    [
        # A deleted middle row: seq 5 follows seq 3, so 5 fails continuity as the 4th entry.
        (4, {"valid": False, "entries_checked": 4, "first_mismatch": "5"}),
        # A deleted first row: the first remaining row is seq 2, not 1 (addendum rule 11).
        (1, {"valid": False, "entries_checked": 1, "first_mismatch": "2"}),
    ],
)
def test_deleted_rows_through_the_api(
    client: ApiClient,
    app_engine: Engine,
    owner_tamper: Callable[..., None],
    deleted: int,
    expected: dict[str, Any],
) -> None:
    build_ledger(client, app_engine)
    owner_tamper(f"DELETE FROM ledger_entry WHERE seq = {deleted}")
    assert api_verify(client) == expected == direct_verify(app_engine)


def test_verify_writes_nothing(client: ApiClient, app_engine: Engine, owner_engine: Engine) -> None:
    build_ledger(client, app_engine)
    before = owner_rows(owner_engine)
    api_verify(client)
    api_verify(client, "rev-1")
    assert owner_rows(owner_engine) == before
    assert append_lock_is_free(owner_engine)


def test_export_and_verify_do_not_wait_for_a_held_append_lock(
    api_app: FastAPI, client: ApiClient, app_engine: Engine
) -> None:
    build_ledger(client, app_engine)
    responses: dict[str, httpx.Response] = {}

    def call_both() -> None:
        with ApiClient(api_app) as other_client:
            for name in ("verify", "export"):
                responses[name] = other_client.get(
                    f"/v1/ledger/{name}", headers=as_actor("viewer-1")
                )

    holder = Session(app_engine)
    caller = threading.Thread(target=call_both, daemon=True)
    try:
        # An append that is not committed: it holds the append lock until rollback.
        ledger.append(holder, "EVENT_VIEWED", "viewer-1", "evt-a", {})
        caller.start()
        caller.join(timeout=10)
        # Both requests must finish while the lock is still held. If either waited for the
        # lock, the thread is still alive here and the rollback below lets it finish.
        finished_while_locked = not caller.is_alive()
    finally:
        holder.rollback()
        holder.close()
        caller.join(timeout=30)
    assert finished_while_locked, "verify or export waited for the append lock"
    # The uncommitted 8th entry is invisible to both.
    assert responses["verify"].json() == {
        "valid": True,
        "entries_checked": 7,
        "first_mismatch": None,
    }
    assert [entry["seq"] for entry in responses["export"].json()] == list(range(1, 8))
