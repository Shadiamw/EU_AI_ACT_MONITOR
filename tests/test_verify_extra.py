"""M3 extra verification tests: exhaustive tamper and deletion matrices, pure/database
agreement, verify during live appends, and verify's database role.

Expected first_mismatch values are reasoned out in comments, not copied from output.
"""

import copy
import logging
import threading
from collections.abc import Callable, Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import ledger
from app.ledger import VerifyResult, verify_entries, verify_ledger
from app.ledger.canonical import GENESIS_PREV_HASH, canonical_json, compute_hash
from tests.test_verify import build_flagged_chain, owner_edit, recomputed_hash, stored_row

FIELDS = (
    "seq",
    "entry_type",
    "actor_id",
    "subject_id",
    "payload",
    "recorded_at",
    "prev_hash",
    "hash",
)


def build_pure_chain() -> list[dict[str, Any]]:
    """An 8-entry chain in memory, built with the M1 functions, shaped like real entries."""
    specs: list[tuple[str, str, str | None, dict[str, Any]]] = [
        ("SEED", "system", None, {"systems": ["sys-42"], "threshold_count": "2"}),
        ("EVENT_FLAGGED", "sys-42", "evt-1", {"event_id": "evt-1", "observed_value": "0.34"}),
        ("NOTIFICATION_REQUESTED", "sys-42", "evt-1", {"notification_id": "ntf-1"}),
        ("EVENT_VIEWED", "viewer-1", "evt-1", {}),
        ("AUDIT_VIEWED", "rev-1", "evt-1", {}),
        ("GAP_OPENED", "system", "evt-2", {"event_id": "evt-2", "last_received_at": "x"}),
        ("WEBHOOK_REGISTERED", "rev-1", "wh-1", {"url": "https://ex.ample/é", "n": 7}),
        ("EVENT_VIEWED", "viewer-1", "evt-2", {}),
    ]
    rows: list[dict[str, Any]] = []
    prev_hash = GENESIS_PREV_HASH
    for seq, (entry_type, actor_id, subject_id, payload) in enumerate(specs, start=1):
        row: dict[str, Any] = {
            "seq": seq,
            "entry_type": entry_type,
            "actor_id": actor_id,
            "subject_id": subject_id,
            "payload": payload,
            "recorded_at": f"2026-09-27T10:0{seq}:00.000Z",
            "prev_hash": prev_hash,
        }
        row["hash"] = compute_hash(prev_hash, canonical_json(**{f: row[f] for f in FIELDS[:6]}))
        prev_hash = row["hash"]
        rows.append(row)
    return rows


def valid(total: int) -> VerifyResult:
    return {"valid": True, "entries_checked": total, "first_mismatch": None}


def invalid(checked: int, mismatch: str) -> VerifyResult:
    return {"valid": False, "entries_checked": checked, "first_mismatch": mismatch}


def other_value(field: str, row: dict[str, Any]) -> Any:
    """A different value for one field that still has a valid format."""
    match field:
        case "seq":
            return row["seq"] + 100
        case "entry_type":
            return "GAP_CLOSED" if row["entry_type"] != "GAP_CLOSED" else "SEED"
        case "actor_id":
            return row["actor_id"] + "-x"
        case "subject_id":
            return "evt-other" if row["subject_id"] is None else row["subject_id"] + "-x"
        case "payload":
            return {**row["payload"], "tampered": "1"}
        case "recorded_at":
            return "2027-01-01T00:00:00.000Z"
        case "prev_hash" | "hash":
            return format(int(row[field], 16) ^ 1, "064x")  # flip the last bit
    raise AssertionError(field)


def verify_with_check(
    rows: list[dict[str, Any]], caplog: pytest.LogCaptureFixture
) -> tuple[VerifyResult, str | None]:
    """verify_entries plus the failed check, read from the WARNING log line (decision 21)."""
    caplog.clear()
    caplog.set_level(logging.INFO, logger="ledger")
    result = verify_entries(rows)
    checks = [
        r.getMessage().rsplit("check=", 1)[1]
        for r in caplog.records
        if r.levelno == logging.WARNING
    ]
    return result, (checks[0] if checks else None)


def expected_change_check(field: str) -> str:
    return "prev_hash" if field == "prev_hash" else "hash"


def expected_removal_check(field: str) -> str:
    return {"prev_hash": "prev_hash", "hash": "hash"}.get(field, "uncanonicalizable")


def test_pure_chain_is_valid() -> None:
    assert verify_entries(build_pure_chain()) == valid(8)


# 1. Exhaustive single-field tamper matrix (pure)


@pytest.mark.parametrize("field", [f for f in FIELDS if f != "seq"])
@pytest.mark.parametrize("k", range(1, 9))
def test_single_field_change_is_caught_at_that_row(
    k: int, field: str, caplog: pytest.LogCaptureFixture
) -> None:
    rows = build_pure_chain()
    rows[k - 1][field] = other_value(field, rows[k - 1])
    # Rows 1..k-1 are untouched and pass. Row k keeps its seq, so check 1 passes. prev_hash:
    # the link check fails at row k. hash: the recompute no longer equals the (changed) stored
    # hash. Any content field: the recompute from row k's stored fields differs from its stored
    # hash. Either way row k is the first failure and k entries were examined.
    assert verify_with_check(rows, caplog) == (invalid(k, str(k)), expected_change_check(field))


@pytest.mark.parametrize("k", range(1, 9))
def test_changing_seq_is_caught_at_that_row_and_reports_the_stored_seq(
    k: int, caplog: pytest.LogCaptureFixture
) -> None:
    rows = build_pure_chain()
    rows[k - 1]["seq"] = k + 100
    # Row k (by position) fails the seq check. first_mismatch is "that entry's seq as a
    # string" (spec), and that entry's stored seq is now k + 100, not k.
    assert verify_with_check(rows, caplog) == (invalid(k, str(k + 100)), "seq")


@pytest.mark.parametrize("field", [f for f in FIELDS if f != "seq"])
@pytest.mark.parametrize("k", range(1, 9))
def test_removing_a_field_is_caught_at_that_row(
    k: int, field: str, caplog: pytest.LogCaptureFixture
) -> None:
    rows = build_pure_chain()
    del rows[k - 1][field]
    # prev_hash missing: the link check fails at k. hash missing: the recompute cannot equal
    # a missing hash. entry_type, actor_id, subject_id, payload or recorded_at missing: a row
    # missing a content field is uncanonicalizable, a mismatch at k (decision 24), whether
    # its subject_id was null or not.
    assert verify_with_check(rows, caplog) == (invalid(k, str(k)), expected_removal_check(field))


@pytest.mark.parametrize("k", range(1, 9))
def test_removing_seq_is_caught_at_that_row(k: int) -> None:
    rows = build_pure_chain()
    del rows[k - 1]["seq"]
    # Row k fails the seq check. The server reports the string form of the missing value,
    # "None" (decision 19); the point here is that it is invalid after k entries.
    result = verify_entries(rows)
    assert (result["valid"], result["entries_checked"]) == (False, k)


# Decision 24: missing fields, explicit null subject_id and unknown extra keys (pure)


def test_removing_the_null_subject_id_from_the_seed_row_is_caught(
    caplog: pytest.LogCaptureFixture,
) -> None:
    rows = build_pure_chain()
    assert rows[0]["entry_type"] == "SEED" and rows[0]["subject_id"] is None
    del rows[0]["subject_id"]
    # Reading the missing key as null would give the same canonical form and a valid chain.
    # A row missing a content field is a mismatch at that seq instead (decision 24): row 1
    # passes seq and prev_hash, then fails as uncanonicalizable after 1 entry.
    assert verify_with_check(rows, caplog) == (invalid(1, "1"), "uncanonicalizable")


def test_an_explicit_null_subject_id_is_valid(caplog: pytest.LogCaptureFixture) -> None:
    rows = build_pure_chain()
    # Row 1 has the subject_id key with the value null: that is "no subject", not missing.
    assert "subject_id" in rows[0] and rows[0]["subject_id"] is None
    assert verify_with_check(rows, caplog) == (valid(8), None)


@pytest.mark.parametrize("k", range(1, 9))
def test_an_unknown_extra_key_is_ignored(k: int, caplog: pytest.LogCaptureFixture) -> None:
    rows = build_pure_chain()
    rows[k - 1]["unexpected"] = "ignored"
    # The hash covers only the six content fields (and the stored prev_hash), so an extra key
    # changes nothing that is checked: the chain stays valid (decision 24).
    assert verify_with_check(rows, caplog) == (valid(8), None)


# 2. Exhaustive deletion matrix (pure)


@pytest.mark.parametrize("i", range(1, 9))
def test_deleting_one_row(i: int, caplog: pytest.LogCaptureFixture) -> None:
    rows = build_pure_chain()
    del rows[i - 1]
    if i < 8:
        # i == 1: the first remaining row is seq 2, but the first must be 1.
        # 1 < i < 8: row i+1 follows row i-1, so its seq is not previous + 1.
        # Either way row i+1 fails the seq check, as the i-th entry examined.
        assert verify_with_check(rows, caplog) == (invalid(i, str(i + 1)), "seq")
    else:
        # Known limit (addendum rule 12): removing the last entry leaves a valid chain.
        assert verify_entries(rows) == valid(7)


# 3. Pure and database agreement


@pytest.fixture
def cleanup_after(owner_engine: Engine) -> Iterator[None]:
    yield
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entry"))


def read_rows_as_dicts(app_engine: Engine) -> list[dict[str, Any]]:
    with app_engine.connect() as conn:
        return [
            dict(row._mapping)
            for row in conn.execute(
                text(f"SELECT {', '.join(FIELDS)} FROM ledger_entry ORDER BY seq")
            )
        ]


def forge_middle(owner_engine: Engine) -> None:
    row = stored_row(owner_engine, 3)
    row["payload"]["observed_value"] = "0.29"
    owner_edit(
        owner_engine,
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '\"0.29\"'),"
        f" hash = '{recomputed_hash(row)}' WHERE seq = 3",
    )


def relink_gap(owner_engine: Engine) -> None:
    row2, row4 = stored_row(owner_engine, 2), stored_row(owner_engine, 4)
    row4["prev_hash"] = row2["hash"]
    owner_edit(
        owner_engine,
        "DELETE FROM ledger_entry WHERE seq = 3",
        f"UPDATE ledger_entry SET prev_hash = '{row2['hash']}',"
        f" hash = '{recomputed_hash(row4)}' WHERE seq = 4",
    )


def sql(*statements: str) -> Callable[[Engine], None]:
    return lambda owner_engine: owner_edit(owner_engine, *statements)


SCENARIOS: dict[str, tuple[Callable[[Engine], None], VerifyResult]] = {
    "untampered": (lambda owner_engine: None, valid(5)),
    "payload-edit": (
        sql(
            "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '\"0.29\"')"
            " WHERE seq = 3"
        ),
        invalid(3, "3"),
    ),
    "recorded_at-edit": (
        sql("UPDATE ledger_entry SET recorded_at = '2026-01-01T00:00:00.000Z' WHERE seq = 3"),
        invalid(3, "3"),
    ),
    "deleted-middle": (sql("DELETE FROM ledger_entry WHERE seq = 3"), invalid(3, "4")),
    "deleted-first": (sql("DELETE FROM ledger_entry WHERE seq = 1"), invalid(1, "2")),
    "swapped": (
        sql(
            "UPDATE ledger_entry SET seq = 1000000 WHERE seq = 2",
            "UPDATE ledger_entry SET seq = 2 WHERE seq = 3",
            "UPDATE ledger_entry SET seq = 3 WHERE seq = 1000000",
        ),
        invalid(2, "2"),
    ),
    "forged-middle": (forge_middle, invalid(4, "4")),
    "relinked-gap": (relink_gap, invalid(3, "4")),
    "jsonb-float": (
        sql(
            "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '1.5')"
            " WHERE seq = 3"
        ),
        invalid(3, "3"),
    ),
}


@pytest.mark.parametrize("scenario", list(SCENARIOS))
def test_pure_and_database_verify_agree(
    app_engine: Engine, owner_engine: Engine, cleanup_after: None, scenario: str
) -> None:
    tamper, expected = SCENARIOS[scenario]
    build_flagged_chain(app_engine, 5)
    tamper(owner_engine)
    with Session(app_engine) as session:
        from_database = verify_ledger(session)
        session.rollback()
    from_dicts = verify_entries(copy.deepcopy(read_rows_as_dicts(app_engine)))
    assert from_dicts == from_database == expected


# 4. Verify during live appends


def test_verify_during_live_appends_never_reports_a_false_mismatch(
    app_engine: Engine, cleanup_after: None
) -> None:
    writers, per_writer = 4, 40
    errors: list[BaseException] = []

    def writer(n: int) -> None:
        try:
            for i in range(per_writer):
                with Session(app_engine) as session:
                    ledger.append(session, "EVENT_VIEWED", "viewer-1", f"evt-{n}-{i}", {"i": i})
                    session.commit()
        except BaseException as exc:  # reported by the main thread
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(writers)]
    for thread in threads:
        thread.start()
    results: list[VerifyResult] = []
    while any(thread.is_alive() for thread in threads) or not results:
        with Session(app_engine) as session:
            results.append(verify_ledger(session))
            session.rollback()
    for thread in threads:
        thread.join(30)
    with Session(app_engine) as session:
        results.append(verify_ledger(session))
        session.rollback()

    assert errors == []
    assert len(results) >= 2
    assert all(result["valid"] for result in results), [r for r in results if not r["valid"]]
    counts = [result["entries_checked"] for result in results]
    assert counts == sorted(counts), "entries_checked decreased between verifies"
    assert counts[-1] == writers * per_writer


# 5. Verify runs as the application role and needs only SELECT


def test_verify_runs_as_application_role_with_select_only(
    app_engine: Engine, cleanup_after: None
) -> None:
    build_flagged_chain(app_engine, 3)
    with Session(app_engine) as session:
        # A READ ONLY transaction rejects any write, so this proves verify only reads.
        session.execute(text("SET TRANSACTION READ ONLY"))
        assert session.execute(text("SELECT current_user")).scalar_one() == "monitor_app"
        assert verify_ledger(session) == valid(3)
        held_advisory_locks = session.execute(
            text(
                "SELECT count(*) FROM pg_locks"
                " WHERE locktype = 'advisory' AND pid = pg_backend_pid()"
            )
        ).scalar_one()
        assert held_advisory_locks == 0
        session.rollback()
    with app_engine.connect() as conn:
        privileges = {
            p: conn.execute(
                text("SELECT has_table_privilege('ledger_entry', :p)"), {"p": p}
            ).scalar_one()
            for p in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE")
        }
    # Verify succeeded with a role whose only privileges on the ledger are SELECT and INSERT,
    # inside a read-only transaction, so it used SELECT alone.
    assert privileges == {
        "SELECT": True,
        "INSERT": True,
        "UPDATE": False,
        "DELETE": False,
        "TRUNCATE": False,
    }


# 6. Editing a stored seq in the database (decision 23)


def trigger_enabled(owner_engine: Engine) -> bool:
    with owner_engine.connect() as conn:
        state = conn.execute(
            text(
                "SELECT tgenabled FROM pg_trigger WHERE tgname = 'ledger_entry_block_update_delete'"
            )
        ).scalar_one()
    return bool(state == "O")


def edit_seq_and_verify(
    app_engine: Engine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    old_seq: int,
    new_seq: int,
) -> tuple[VerifyResult, str | None]:
    """Edit one stored seq as the owner, then verify the database; return result and check.

    Also checks that the trigger is enabled again afterwards, and that the pure function on
    the rows read back in database order gives the same result as verify_ledger.
    """
    # owner_edit disables the trigger, edits and re-enables it in one transaction; if the edit
    # fails, the rollback also undoes the disable, so the trigger is never left off.
    owner_edit(owner_engine, f"UPDATE ledger_entry SET seq = {new_seq} WHERE seq = {old_seq}")
    assert trigger_enabled(owner_engine)
    caplog.clear()
    caplog.set_level(logging.INFO, logger="ledger")
    with Session(app_engine) as session:
        from_database = verify_ledger(session)
        session.rollback()
    checks = [
        r.getMessage().rsplit("check=", 1)[1]
        for r in caplog.records
        if r.levelno == logging.WARNING
    ]
    rows_in_database_order = read_rows_as_dicts(app_engine)
    assert [row["seq"] for row in rows_in_database_order] == sorted(
        row["seq"] for row in rows_in_database_order
    )
    assert verify_entries(rows_in_database_order) == from_database
    return from_database, (checks[0] if checks else None)


@pytest.mark.parametrize("k", range(1, 6))
def test_editing_a_non_last_seq_in_the_database_is_caught_at_the_next_row(
    app_engine: Engine,
    owner_engine: Engine,
    cleanup_after: None,
    caplog: pytest.LogCaptureFixture,
    k: int,
) -> None:
    build_flagged_chain(app_engine, 6)
    # Verify reads ORDER BY the stored seq, so row k (now k + 100) sorts to the end. The rows
    # are read as 1..k-1, k+1, ..., 6, k+100. Rows 1..k-1 pass. The k-th row read is the old
    # row k+1: for k > 1 its seq is not previous + 1 (k-1 + 1 = k), and for k = 1 it is the
    # first row but not seq 1. Either way the seq check fails there, after k entries, and
    # first_mismatch is that row's stored seq, k + 1 (decision 23). The edited row itself is
    # never reached.
    assert edit_seq_and_verify(app_engine, owner_engine, caplog, k, k + 100) == (
        invalid(k, str(k + 1)),
        "seq",
    )


def test_editing_the_last_seq_in_the_database_is_caught_at_that_row(
    app_engine: Engine, owner_engine: Engine, cleanup_after: None, caplog: pytest.LogCaptureFixture
) -> None:
    n = 6
    build_flagged_chain(app_engine, n)
    # Row N keeps its place at the end. Rows 1..N-1 pass, then row N's stored seq is N + 100,
    # not N: the seq check fails at the N-th entry and first_mismatch is "N+100".
    assert edit_seq_and_verify(app_engine, owner_engine, caplog, n, n + 100) == (
        invalid(n, str(n + 100)),
        "seq",
    )
