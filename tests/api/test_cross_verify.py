"""M5 C: cross-implementation check of tools/verify_ledger.py against the real database.

Every scenario is set up as the owner role, then three results are taken: GET
/v1/ledger/verify through the app, verify_ledger called directly, and the script run as a
subprocess on the file saved from GET /v1/ledger/export. All three must equal each other AND
the expected value derived by hand from the spec in the scenario table. The script's check
name (stderr) must equal the server's (log).

This module cleans up after each test (trigger re-enabled, owner TRUNCATE), and the guard test
at the end checks that nothing was left behind.
"""

import json
import logging
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import ledger
from app.ledger import verify_ledger
from tests.api.conftest import (
    ApiClient,
    as_actor,
    count_entries,
    flag_event,
    trigger_enabled,
)
from tests.db_helpers import TRIGGER_NAME
from tests.script_helpers import FIXTURE, Run, run_subprocess
from tests.test_verify import recomputed_hash, stored_row

ZEROS = "0" * 64


@pytest.fixture(scope="module", autouse=True)
def start_module_empty(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entry"))


@pytest.fixture(autouse=True)
def empty_ledger(owner_engine: Engine) -> Iterator[None]:
    """Overrides the shared pre-test truncate: clean up AFTER each test, even on failure."""
    yield
    with owner_engine.begin() as conn:
        conn.execute(text(f"ALTER TABLE ledger_entry ENABLE TRIGGER {TRIGGER_NAME}"))
        conn.execute(text("TRUNCATE ledger_entry"))


@dataclass
class Results:
    api: dict[str, Any]
    direct: dict[str, Any]
    script: Run
    server_checks: list[str]


def server_checks(caplog: pytest.LogCaptureFixture) -> list[str]:
    return [
        r.getMessage().rsplit("check=", 1)[1]
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name == "ledger"
    ]


def three_results(
    client: ApiClient, app_engine: Engine, caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> Results:
    caplog.set_level(logging.INFO, logger="ledger")
    caplog.clear()
    api = client.get("/v1/ledger/verify", headers=as_actor("viewer-1")).json()
    with Session(app_engine) as session:
        direct = dict(verify_ledger(session))
        session.rollback()
    checks = server_checks(caplog)
    export = client.get("/v1/ledger/export", headers=as_actor("viewer-1"))
    assert export.status_code == 200
    path = tmp_path / "export.json"
    path.write_bytes(export.content)  # the file exactly as served
    return Results(api, direct, run_subprocess([str(path)]), checks)


def build_base_chain(app_engine: Engine) -> None:
    """Six real appends (ledger.append), including a non-ASCII payload.

    1 SEED, 2 EVENT_FLAGGED evt-a, 3 NOTIFICATION_REQUESTED evt-a, 4 EVENT_VIEWED evt-a,
    5 WEBHOOK_REGISTERED wh-1 (non-ASCII URL), 6 EVENT_VIEWED evt-a by rev-1.
    """
    with Session(app_engine) as session:
        ledger.ensure_seed(session, {"systems": ["sys-42"], "threshold_count": "2"})
        session.commit()
    flag_event(app_engine, "evt-a")  # 2 and 3
    entries: list[tuple[str, str, str | None, ledger.canonical.Payload]] = [
        ("EVENT_VIEWED", "viewer-1", "evt-a", {}),
        (
            "WEBHOOK_REGISTERED",
            "rev-1",
            "wh-1",
            {"webhook_id": "wh-1", "url": "https://ex.ample/é/日本", "event_types": ["reminder"]},
        ),
        ("EVENT_VIEWED", "rev-1", "evt-a", {}),
    ]
    for entry_type, actor_id, subject_id, payload in entries:
        with Session(app_engine) as session:
            ledger.append(session, entry_type, actor_id, subject_id, payload)
            session.commit()


def forge_middle(owner_engine: Engine) -> list[str]:
    row = stored_row(owner_engine, 3)
    row["payload"]["notification_id"] = "ntf-forged"
    return [
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{notification_id}',"
        f" '\"ntf-forged\"'), hash = '{recomputed_hash(row)}' WHERE seq = 3"
    ]


def relink_gap(owner_engine: Engine) -> list[str]:
    row2, row4 = stored_row(owner_engine, 2), stored_row(owner_engine, 4)
    row4["prev_hash"] = row2["hash"]
    return [
        "DELETE FROM ledger_entry WHERE seq = 3",
        f"UPDATE ledger_entry SET prev_hash = '{row2['hash']}',"
        f" hash = '{recomputed_hash(row4)}' WHERE seq = 4",
    ]


def first_must_be_1_forgery(owner_engine: Engine) -> list[str]:
    row2 = stored_row(owner_engine, 2)
    row2["prev_hash"] = ZEROS
    return [
        "DELETE FROM ledger_entry WHERE seq = 1",
        f"UPDATE ledger_entry SET prev_hash = '{ZEROS}', hash = '{recomputed_hash(row2)}'"
        " WHERE seq = 2",
    ]


def flip(owner_engine: Engine, seq: int, column: str) -> list[str]:
    value = stored_row(owner_engine, seq)[column]
    flipped = format(int(value, 16) ^ 1, "064x")
    return [f"UPDATE ledger_entry SET {column} = '{flipped}' WHERE seq = {seq}"]


def sql(*statements: str) -> Callable[[Engine], list[str]]:
    return lambda owner_engine: list(statements)


def payload_set(seq: int, key: str, json_value: str) -> Callable[[Engine], list[str]]:
    return sql(
        f"UPDATE ledger_entry SET payload = jsonb_set(payload, '{{{key}}}', '{json_value}')"
        f" WHERE seq = {seq}"
    )


@dataclass
class Scenario:
    tamper: Callable[[Engine], list[str]] | None  # None: no base chain at all
    expected: tuple[bool, int, str | None, str | None]  # valid, checked, mismatch, check


NO_EDIT = sql()

# Expected values, reasoned from the spec's check order (seq, prev_hash, hash), for the
# six-entry base chain. "hash" means the recomputed hash differs from the stored one.
SCENARIOS: dict[str, Scenario] = {
    # Untouched real appends, including a non-ASCII payload: all six pass.
    "valid-chain": Scenario(NO_EDIT, (True, 6, None, None)),
    # No entries at all: valid with nothing checked (addendum rule 11).
    "empty-ledger": Scenario(None, (True, 0, None, None)),
    # A payload edit makes that row's recompute differ: invalid at that row.
    "payload-first": Scenario(payload_set(1, "threshold_count", '"3"'), (False, 1, "1", "hash")),
    "payload-middle": Scenario(
        payload_set(3, "notification_id", '"ntf-x"'), (False, 3, "3", "hash")
    ),
    "payload-last": Scenario(
        sql('UPDATE ledger_entry SET payload = \'{"x": "1"}\' WHERE seq = 6'),
        (False, 6, "6", "hash"),
    ),
    # Each content field is hashed, so each edit is caught by the hash check at row 3.
    "recorded_at": Scenario(
        sql("UPDATE ledger_entry SET recorded_at = '2026-01-01T00:00:00.000Z' WHERE seq = 3"),
        (False, 3, "3", "hash"),
    ),
    "actor_id": Scenario(
        sql("UPDATE ledger_entry SET actor_id = 'sys-42-x' WHERE seq = 3"),
        (False, 3, "3", "hash"),
    ),
    "subject_id": Scenario(
        sql("UPDATE ledger_entry SET subject_id = 'evt-other' WHERE seq = 3"),
        (False, 3, "3", "hash"),
    ),
    "entry_type": Scenario(
        sql("UPDATE ledger_entry SET entry_type = 'GAP_CLOSED' WHERE seq = 3"),
        (False, 3, "3", "hash"),
    ),
    # Row 3's stored hash no longer equals its recompute: the hash check fails at 3 first.
    "hash": Scenario(lambda e: flip(e, 3, "hash"), (False, 3, "3", "hash")),
    # Row 3's prev_hash no longer equals row 2's hash: the link check fails at 3.
    "prev_hash": Scenario(lambda e: flip(e, 3, "prev_hash"), (False, 3, "3", "prev_hash")),
    # Seq 4 follows seq 2: continuity fails at the 3rd entry read, seq 4 (addendum rule 11).
    "deleted-middle": Scenario(
        sql("DELETE FROM ledger_entry WHERE seq = 3"), (False, 3, "4", "seq")
    ),
    # The first remaining entry is seq 2, not 1 (addendum rule 11).
    "deleted-first": Scenario(
        sql("DELETE FROM ledger_entry WHERE seq = 1"), (False, 1, "2", "seq")
    ),
    # Known limit (addendum rule 12): the five remaining entries form a valid chain.
    "deleted-last": Scenario(sql("DELETE FROM ledger_entry WHERE seq = 6"), (True, 5, None, None)),
    # Every row deleted: an empty ledger, valid with nothing checked.
    "all-deleted": Scenario(sql("DELETE FROM ledger_entry"), (True, 0, None, None)),
    # Rows 2 and 3 swap seqs: the row now at seq 2 links to row 2's hash, not row 1's.
    "swapped": Scenario(
        sql(
            "UPDATE ledger_entry SET seq = 1000000 WHERE seq = 2",
            "UPDATE ledger_entry SET seq = 2 WHERE seq = 3",
            "UPDATE ledger_entry SET seq = 3 WHERE seq = 1000000",
        ),
        (False, 2, "2", "prev_hash"),
    ),
    # Row 3 forged with its own hash recomputed: it passes, but row 4 still links to the old
    # hash, so the link check fails at 4.
    "forged-middle": Scenario(forge_middle, (False, 4, "4", "prev_hash")),
    # Row 3 deleted and row 4 re-linked to row 2: links and hashes pass, but seq 4 follows 2.
    "relinked-gap": Scenario(relink_gap, (False, 3, "4", "seq")),
    # Row 1 deleted and row 2 re-linked to 64 zeros: the first entry is seq 2, not 1.
    "first-must-be-1": Scenario(first_must_be_1_forgery, (False, 1, "2", "seq")),
    # Row 3's seq set to 103 sorts it last: entries 1, 2, 4, ... so seq 4 fails continuity
    # as the 3rd entry read (decision 23).
    "seq-middle-plus-100": Scenario(
        sql("UPDATE ledger_entry SET seq = 103 WHERE seq = 3"), (False, 3, "4", "seq")
    ),
    # Row 6's seq set to 106 stays last: 106 is not 5 + 1, after 6 entries (decision 23).
    "seq-last-plus-100": Scenario(
        sql("UPDATE ledger_entry SET seq = 106 WHERE seq = 6"), (False, 6, "106", "seq")
    ),
    # jsonb keeps 1.5 and 1.0 as non-integer numbers: row 2 cannot be canonicalized.
    "jsonb-float-1.5": Scenario(
        payload_set(2, "observed_value", "1.5"), (False, 2, "2", "uncanonicalizable")
    ),
    "jsonb-float-1.0": Scenario(
        payload_set(2, "observed_value", "1.0"), (False, 2, "2", "uncanonicalizable")
    ),
    # jsonb stores 1e3 as the integer 1000: row 2 canonicalizes, to a different string, so
    # the hash check fails at 2 (decisions 22 and 56).
    "jsonb-1e3": Scenario(payload_set(2, "observed_value", "1e3"), (False, 2, "2", "hash")),
}


def as_result(expected: tuple[bool, int, str | None, str | None]) -> dict[str, Any]:
    valid, checked, mismatch, _ = expected
    return {"valid": valid, "entries_checked": checked, "first_mismatch": mismatch}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_script_server_and_hand_derived_results_agree(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    owner_tamper: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
    name: str,
) -> None:
    scenario = SCENARIOS[name]
    if scenario.tamper is not None:
        build_base_chain(app_engine)
        statements = scenario.tamper(owner_engine)
        if statements:
            owner_tamper(*statements)
    results = three_results(client, app_engine, caplog, tmp_path)
    expected = as_result(scenario.expected)
    expected_check = scenario.expected[3]
    print(
        f"\n{name}: expected {expected} check={expected_check} | api {results.api} | direct"
        f" {results.direct} | script {results.script.result} check={results.script.check}"
    )
    assert results.api == results.direct == results.script.result == expected
    # The script's exit code follows its result (decision 53).
    assert results.script.code == (0 if expected["valid"] else 1)
    # Same check name: the API call and the direct call each log one mismatch, and the
    # script writes the same name to stderr (decision 60).
    expected_checks = [] if expected_check is None else [expected_check, expected_check]
    assert results.server_checks == expected_checks
    assert results.script.check == expected_check


def test_fixture_loaded_into_the_database(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with owner_engine.begin() as conn:  # plain INSERT as the owner, not through append
        for row in rows:
            conn.execute(
                text(
                    "INSERT INTO ledger_entry (seq, entry_type, actor_id, subject_id, payload,"
                    " recorded_at, prev_hash, hash) VALUES (:seq, :entry_type, :actor_id,"
                    " :subject_id, CAST(:payload AS jsonb), :recorded_at, :prev_hash, :hash)"
                ),
                {**row, "payload": json.dumps(row["payload"])},
            )
    valid_3 = {"valid": True, "entries_checked": 3, "first_mismatch": None}
    # The addendum's three entries are correct: the server, the script on the fixture file and
    # the script on the server's export all report valid with 3 checked (addendum rule 8).
    results = three_results(client, app_engine, caplog, tmp_path)
    assert results.api == results.direct == results.script.result == valid_3
    assert run_subprocess([str(FIXTURE)]).result == valid_3


def test_one_character_change_in_entry_2_in_file_and_database(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    owner_tamper: Callable[..., None],
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with owner_engine.begin() as conn:
        for row in rows:
            conn.execute(
                text(
                    "INSERT INTO ledger_entry (seq, entry_type, actor_id, subject_id, payload,"
                    " recorded_at, prev_hash, hash) VALUES (:seq, :entry_type, :actor_id,"
                    " :subject_id, CAST(:payload AS jsonb), :recorded_at, :prev_hash, :hash)"
                ),
                {**row, "payload": json.dumps(row["payload"])},
            )
    # "0.34" becomes "0.35", once in a copy of the file and once in the database.
    tampered_file = tmp_path / "tampered.json"
    original = FIXTURE.read_text(encoding="utf-8")
    assert original.count('"0.34"') == 1
    tampered_file.write_text(original.replace('"0.34"', '"0.35"'), encoding="utf-8")
    owner_tamper(
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '\"0.35\"')"
        " WHERE seq = 2"
    )
    # Entry 2 no longer hashes to its stored hash: invalid at 2 after 2 (addendum rule 8).
    expected = {"valid": False, "entries_checked": 2, "first_mismatch": "2"}
    results = three_results(client, app_engine, caplog, tmp_path)
    assert results.api == results.direct == results.script.result == expected
    file_run = run_subprocess([str(tampered_file)])
    assert (file_run.result, file_run.check) == (expected, "hash")


def test_live_export_through_the_api_verifies(
    client: ApiClient,
    app_engine: Engine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    tmp_path: Path,
) -> None:
    build_base_chain(app_engine)  # 6 entries
    flag_event(app_engine, "evt-b")  # 7, 8
    for actor in ("viewer-1", "rev-1"):
        assert client.get("/v1/events/evt-b", headers=as_actor(actor)).status_code == 200
        assert client.get("/v1/events/evt-b/audit", headers=as_actor(actor)).status_code == 200
    # 8 entries, then per actor one EVENT_VIEWED and one AUDIT_VIEWED: 12 in total.
    assert count_entries(owner_engine, "AUDIT_VIEWED") == 2
    expected = {"valid": True, "entries_checked": 12, "first_mismatch": None}
    results = three_results(client, app_engine, caplog, tmp_path)
    assert results.api == results.direct == results.script.result == expected
    assert results.script.code == 0


def test_zz_guard_every_scenario_left_the_database_clean(owner_engine: Engine) -> None:
    """Runs last in this module: the trigger is enabled and the table is empty."""
    assert trigger_enabled(owner_engine)
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one() == 0
