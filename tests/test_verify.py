"""M3: verify core and tamper detection.

Every expected first_mismatch below is derived by reasoning about the chain, stated next to
the assertion. Tamper edits run as the owner with the trigger disabled for one transaction
(addendum rule 1). This module cleans up after each test itself (trigger re-enabled, owner
TRUNCATE) instead of relying on the shared pre-test truncate, so the guard test at the end
can check what the previous test left behind.
"""

import json
import logging
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text
from sqlalchemy.orm import Session

from app import audit, ledger
from app.auth import Principal
from app.ledger import VerifyResult, verify_entries, verify_ledger
from app.ledger.canonical import GENESIS_PREV_HASH, Payload, canonical_json, compute_hash
from tests.db_helpers import TRIGGER_NAME, trigger_disabled

FIXTURE = Path(__file__).parent / "fixtures" / "fixture_export.json"
SECRET = "SECRET-payload-value-7731"  # must never appear in logs


def flagged_payload(n: int) -> Payload:
    return {
        "event_id": f"evt-{n}",
        "system_id": "sys-42",
        "metric_name": "accuracy_drift",
        "observed_value": "0.34",
        "threshold_value": "0.30",
        "threshold_version_id": "thr-1",
        "note": SECRET,
    }


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
    assert trigger_enabled(owner_engine)


def trigger_enabled(owner_engine: Engine) -> bool:
    with owner_engine.connect() as conn:
        state = conn.execute(
            text("SELECT tgenabled FROM pg_trigger WHERE tgname = :name"), {"name": TRIGGER_NAME}
        ).scalar_one()
    return bool(state == "O")


def verify(app_engine: Engine) -> VerifyResult:
    with Session(app_engine) as session:
        result = verify_ledger(session)
        session.rollback()
    return result


def verify_logging_check(
    app_engine: Engine, caplog: pytest.LogCaptureFixture
) -> tuple[VerifyResult, str | None]:
    """Verify and also return which check failed, taken from the WARNING log line."""
    caplog.clear()
    caplog.set_level(logging.INFO, logger="ledger")
    result = verify(app_engine)
    checks = [
        r.getMessage().rsplit("check=", 1)[1]
        for r in caplog.records
        if r.levelno == logging.WARNING
    ]
    assert len(checks) <= 1
    return result, (checks[0] if checks else None)


def build_flagged_chain(app_engine: Engine, count: int) -> list[ledger.LedgerEntry]:
    entries = []
    for n in range(1, count + 1):
        with Session(app_engine) as session:
            entries.append(
                ledger.append(session, "EVENT_FLAGGED", "sys-42", f"evt-{n}", flagged_payload(n))
            )
            session.commit()
    return entries


def owner_edit(owner_engine: Engine, *statements: str) -> None:
    with trigger_disabled(owner_engine) as conn:
        for statement in statements:
            conn.execute(text(statement))


def stored_row(owner_engine: Engine, seq: int) -> dict[str, Any]:
    with owner_engine.connect() as conn:
        row = conn.execute(text("SELECT * FROM ledger_entry WHERE seq = :seq"), {"seq": seq}).one()
    return dict(row._mapping)


def recomputed_hash(row: dict[str, Any]) -> str:
    """The hash an attacker with owner access would compute for an edited row."""
    return compute_hash(
        row["prev_hash"],
        canonical_json(
            seq=row["seq"],
            entry_type=row["entry_type"],
            actor_id=row["actor_id"],
            subject_id=row["subject_id"],
            payload=row["payload"],
            recorded_at=row["recorded_at"],
        ),
    )


def invalid(checked: int, seq: int) -> VerifyResult:
    return {"valid": False, "entries_checked": checked, "first_mismatch": str(seq)}


def valid(total: int) -> VerifyResult:
    return {"valid": True, "entries_checked": total, "first_mismatch": None}


# 1-2. Valid chains


def test_valid_chain_of_real_appends(app_engine: Engine) -> None:
    with Session(app_engine) as session:
        ledger.ensure_seed(session, {"systems": ["sys-42"], "threshold_count": "2"})
        ledger.append(session, "EVENT_FLAGGED", "sys-42", "evt-1", flagged_payload(1))
        audit.record_view(session, Principal("VIEWER", "viewer-1", None), "evt-1")
        ledger.append(
            session, "WEBHOOK_REGISTERED", "rev-1", "wh-1", {"url": "https://ex.ample/é日😀"}
        )
        session.commit()
    assert verify(app_engine) == valid(4)


def test_thousand_entry_chain_verifies_quickly(app_engine: Engine) -> None:
    with Session(app_engine) as session:
        for n in range(1000):
            ledger.append(session, "EVENT_VIEWED", "viewer-1", f"evt-{n}", {"n": n})
        session.commit()
    started = time.perf_counter()
    result = verify(app_engine)
    elapsed = time.perf_counter() - started
    assert result == valid(1000)
    assert elapsed < 2.0, f"verify of 1,000 entries took {elapsed:.2f}s"


def test_pure_verify_is_linear() -> None:
    """20,000 in-memory entries: a quadratic implementation would take far longer than this."""
    rows = []
    prev_hash = GENESIS_PREV_HASH
    for seq in range(1, 20_001):
        row: dict[str, Any] = {
            "seq": seq,
            "entry_type": "EVENT_VIEWED",
            "actor_id": "viewer-1",
            "subject_id": "evt-1",
            "payload": {},
            "recorded_at": "2026-09-27T10:06:00.000Z",
            "prev_hash": prev_hash,
        }
        row["hash"] = recomputed_hash(row)
        prev_hash = row["hash"]
        rows.append(row)
    started = time.perf_counter()
    assert verify_entries(rows) == valid(20_000)
    assert time.perf_counter() - started < 5.0


def test_empty_ledger_is_valid(app_engine: Engine) -> None:
    assert verify(app_engine) == valid(0)
    assert verify_entries([]) == valid(0)


# 3. AC-06 payload tamper (spec + addendum rule 1), on the first, a middle and the last entry


@pytest.mark.parametrize("target", [1, 3, 5])
def test_ac06_payload_tamper_is_detected(
    app_engine: Engine, owner_engine: Engine, target: int, caplog: pytest.LogCaptureFixture
) -> None:
    entries = build_flagged_chain(app_engine, 5)
    recorded_hash = entries[target - 1].hash
    assert verify(app_engine) == valid(5)

    owner_edit(
        owner_engine,
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '\"0.29\"')"
        f" WHERE seq = {target}",
    )

    row = stored_row(owner_engine, target)
    assert row["payload"]["observed_value"] == "0.29"
    assert row["hash"] == recorded_hash  # the stored hash still says "0.34"
    assert trigger_enabled(owner_engine)
    # Rows before the target are untouched; the target's seq and prev_hash are intact, so the
    # hash recompute fails at the target, which is the last entry examined.
    assert verify_logging_check(app_engine, caplog) == (invalid(target, target), "hash")


# 4. Other column edits on a middle row (seq 3 of 5)


@pytest.mark.parametrize(
    ("assignment", "expected_check"),
    [
        # Passes ck_ledger_entry_recorded_at (decision 4).
        pytest.param("recorded_at = '2026-01-01T00:00:00.000Z'", "hash", id="recorded_at"),
        pytest.param("actor_id = 'rev-1'", "hash", id="actor_id"),
        pytest.param("subject_id = 'evt-999'", "hash", id="subject_id"),
        pytest.param("subject_id = NULL", "hash", id="subject_id-null"),
        pytest.param("entry_type = 'EVENT_VIEWED'", "hash", id="entry_type"),
        # Passes the hex CHECK constraints. hash: recompute fails at 3. prev_hash: link fails at 3.
        pytest.param("hash = repeat('f', 64)", "hash", id="hash"),
        pytest.param("prev_hash = repeat('e', 64)", "prev_hash", id="prev_hash"),
    ],
)
def test_column_edit_is_detected_at_that_row(
    app_engine: Engine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    assignment: str,
    expected_check: str,
) -> None:
    build_flagged_chain(app_engine, 5)
    owner_edit(owner_engine, f"UPDATE ledger_entry SET {assignment} WHERE seq = 3")
    # Rows 1 and 2 pass; row 3 fails one of its own checks.
    assert verify_logging_check(app_engine, caplog) == (invalid(3, 3), expected_check)


# 5. Deletions


def test_deleting_a_middle_row_reports_the_next_seq(
    app_engine: Engine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build_flagged_chain(app_engine, 5)
    owner_edit(owner_engine, "DELETE FROM ledger_entry WHERE seq = 3")
    # Remaining 1, 2, 4, 5: entry seq 4 follows seq 2, so the third entry examined fails.
    assert verify_logging_check(app_engine, caplog) == (invalid(3, 4), "seq")


def test_deleting_the_first_row_reports_the_first_remaining_seq(
    app_engine: Engine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build_flagged_chain(app_engine, 5)
    owner_edit(owner_engine, "DELETE FROM ledger_entry WHERE seq = 1")
    # Remaining 2..5: the first entry examined is seq 2, not 1 (addendum rule 11).
    assert verify_logging_check(app_engine, caplog) == (invalid(1, 2), "seq")


def test_deleting_all_rows_gives_the_empty_result(app_engine: Engine, owner_engine: Engine) -> None:
    build_flagged_chain(app_engine, 5)
    owner_edit(owner_engine, "DELETE FROM ledger_entry")
    assert verify(app_engine) == valid(0)


# 6. Reorder


def test_swapping_two_rows_is_detected(
    app_engine: Engine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build_flagged_chain(app_engine, 4)
    owner_edit(
        owner_engine,
        "UPDATE ledger_entry SET seq = 1000000 WHERE seq = 2",
        "UPDATE ledger_entry SET seq = 2 WHERE seq = 3",
        "UPDATE ledger_entry SET seq = 3 WHERE seq = 1000000",
    )
    # Seq order is now orig1, orig3, orig2, orig4. Position 2 holds orig3: its seq field is 2
    # (seq check passes) but its prev_hash is orig2's hash, not orig1's, so the prev_hash check
    # fails at seq 2 after examining 2 entries.
    assert verify_logging_check(app_engine, caplog) == (invalid(2, 2), "prev_hash")


# 7. Forgeries that only one specific check can catch


def test_forged_middle_row_is_caught_only_by_the_next_prev_hash(
    app_engine: Engine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build_flagged_chain(app_engine, 5)
    row = stored_row(owner_engine, 3)
    row["payload"]["observed_value"] = "0.29"
    forged_hash = recomputed_hash(row)
    owner_edit(
        owner_engine,
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '\"0.29\"'),"
        f" hash = '{forged_hash}' WHERE seq = 3",
    )
    # Row 3 now passes all three of its own checks. Row 4's seq is fine and its own hash is
    # unchanged, but its prev_hash still names the old row 3 hash: prev_hash check, seq 4.
    assert verify_logging_check(app_engine, caplog) == (invalid(4, 4), "prev_hash")


def test_relinked_gap_is_caught_only_by_seq_continuity(
    app_engine: Engine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build_flagged_chain(app_engine, 4)
    row2 = stored_row(owner_engine, 2)
    row4 = stored_row(owner_engine, 4)
    row4["prev_hash"] = row2["hash"]
    forged_hash = recomputed_hash(row4)
    owner_edit(
        owner_engine,
        "DELETE FROM ledger_entry WHERE seq = 3",
        f"UPDATE ledger_entry SET prev_hash = '{row2['hash']}', hash = '{forged_hash}'"
        " WHERE seq = 4",
    )
    # Row 4 links to row 2 and its hash is consistent; only seq 4 after seq 2 is wrong.
    # Entries examined: 1, 2, 4.
    assert verify_logging_check(app_engine, caplog) == (invalid(3, 4), "seq")


def test_relinked_first_row_is_caught_only_by_first_must_be_one(
    app_engine: Engine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build_flagged_chain(app_engine, 4)
    row2 = stored_row(owner_engine, 2)
    row2["prev_hash"] = GENESIS_PREV_HASH
    forged_hash = recomputed_hash(row2)
    owner_edit(
        owner_engine,
        "DELETE FROM ledger_entry WHERE seq = 1",
        f"UPDATE ledger_entry SET prev_hash = repeat('0', 64), hash = '{forged_hash}'"
        " WHERE seq = 2",
    )
    # Row 2 is now a self-consistent genesis entry; only "the first entry must be seq 1" fails.
    assert verify_logging_check(app_engine, caplog) == (invalid(1, 2), "seq")


def test_plain_payload_edit_is_caught_only_by_the_hash_recompute(
    app_engine: Engine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    build_flagged_chain(app_engine, 4)
    owner_edit(
        owner_engine,
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{threshold_value}', '\"0.35\"')"
        " WHERE seq = 2",
    )
    # Row 2's seq and prev_hash are intact and row 3 still links to row 2's stored hash, so only
    # the recompute of row 2 can fail.
    assert verify_logging_check(app_engine, caplog) == (invalid(2, 2), "hash")


# 8. Known limits (addendum rule 12): these document undetectable cases, they are not bugs


def test_known_limit_deleting_the_last_row_is_not_detected(
    app_engine: Engine, owner_engine: Engine
) -> None:
    build_flagged_chain(app_engine, 5)
    owner_edit(owner_engine, "DELETE FROM ledger_entry WHERE seq = 5")
    assert verify(app_engine) == valid(4)


def test_known_limit_forged_last_row_with_consistent_hash_is_not_detected(
    app_engine: Engine, owner_engine: Engine
) -> None:
    build_flagged_chain(app_engine, 5)
    row = stored_row(owner_engine, 5)
    row["payload"]["observed_value"] = "0.29"
    owner_edit(
        owner_engine,
        "UPDATE ledger_entry SET payload = jsonb_set(payload, '{observed_value}', '\"0.29\"'),"
        f" hash = '{recomputed_hash(row)}' WHERE seq = 5",
    )
    assert verify(app_engine) == valid(5)


# 9. Uncanonicalizable stored rows (decision 2)


@pytest.mark.parametrize(
    ("number", "expected_check"),
    [
        pytest.param("1.5", "uncanonicalizable", id="float-1.5"),
        pytest.param("1.0", "uncanonicalizable", id="float-1.0"),
        # jsonb normalizes 1e3 to the integer 1000, which canonicalizes; the hash then differs.
        pytest.param("1e3", "hash", id="exponent-1e3"),
    ],
)
def test_uncanonicalizable_or_numeric_payload_is_a_mismatch_not_an_error(
    app_engine: Engine,
    owner_engine: Engine,
    caplog: pytest.LogCaptureFixture,
    number: str,
    expected_check: str,
) -> None:
    build_flagged_chain(app_engine, 5)
    owner_edit(
        owner_engine,
        f"UPDATE ledger_entry SET payload = jsonb_set(payload, '{{observed_value}}', '{number}')"
        " WHERE seq = 3",
    )
    caplog.set_level(logging.INFO, logger="ledger")
    assert verify(app_engine) == invalid(3, 3)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [f"ledger verify mismatch: seq=3 check={expected_check}"]


def test_pure_verify_never_raises_on_malformed_rows() -> None:
    rows: list[dict[str, Any]] = [
        {"seq": 1, "prev_hash": GENESIS_PREV_HASH, "payload": {"v": 1.5}},
    ]
    assert verify_entries(rows) == invalid(1, 1)
    # Degenerate non-integer seq values (impossible from the bigint column) are reported as
    # given, still as a string, and never raise.
    assert verify_entries([{"seq": True}])["first_mismatch"] == "True"
    assert verify_entries([{}]) == {"valid": False, "entries_checked": 1, "first_mismatch": "None"}


# 10. Verify writes nothing and never blocks


def test_verify_writes_nothing(app_engine: Engine, owner_engine: Engine) -> None:
    build_flagged_chain(app_engine, 3)
    owner_edit(owner_engine, "DELETE FROM ledger_entry WHERE seq = 2")
    with owner_engine.connect() as conn:
        before = conn.execute(text("SELECT * FROM ledger_entry ORDER BY seq")).all()
    verify(app_engine)
    verify(app_engine)
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT * FROM ledger_entry ORDER BY seq")).all() == before


def test_verify_does_not_wait_for_the_append_lock(app_engine: Engine) -> None:
    build_flagged_chain(app_engine, 3)
    with Session(app_engine) as holder:
        ledger.append(holder, "EVENT_VIEWED", "viewer-1", "evt-1", {})  # holds the lock
        result: dict[str, Any] = {}

        def run_verify() -> None:
            with Session(app_engine) as session:
                session.execute(text("SET LOCAL statement_timeout = '2s'"))
                result["value"] = verify_ledger(session)
                session.rollback()

        thread = threading.Thread(target=run_verify)
        thread.start()
        thread.join(5)
        assert not thread.is_alive(), "verify waited for the append lock"
        holder.rollback()
    # The uncommitted fourth entry is invisible to verify.
    assert result["value"] == valid(3)


# 11. Result shape


def test_result_has_exactly_three_fields_and_a_string_mismatch(
    app_engine: Engine, owner_engine: Engine
) -> None:
    build_flagged_chain(app_engine, 3)
    good = verify(app_engine)
    assert set(good) == {"valid", "entries_checked", "first_mismatch"}
    assert good["first_mismatch"] is None
    owner_edit(owner_engine, "UPDATE ledger_entry SET actor_id = 'rev-1' WHERE seq = 2")
    bad = verify(app_engine)
    assert set(bad) == {"valid", "entries_checked", "first_mismatch"}
    assert isinstance(bad["first_mismatch"], str)
    assert bad["first_mismatch"] == "2"
    assert isinstance(bad["entries_checked"], int)
    assert bad["valid"] is False


# 12. Logging


def test_logging_reports_results_without_payload_values_or_hashes(
    app_engine: Engine, owner_engine: Engine, caplog: pytest.LogCaptureFixture
) -> None:
    entries = build_flagged_chain(app_engine, 3)
    caplog.set_level(logging.INFO, logger="ledger")

    verify(app_engine)
    assert [(r.name, r.levelno, r.getMessage()) for r in caplog.records] == [
        ("ledger", logging.INFO, "ledger verify: valid=True entries_checked=3")
    ]
    caplog.clear()

    owner_edit(
        owner_engine,
        f"UPDATE ledger_entry SET payload = jsonb_set(payload, '{{note}}', '\"{SECRET}-x\"')"
        " WHERE seq = 2",
    )
    verify(app_engine)
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.WARNING, "ledger verify mismatch: seq=2 check=hash"),
        (logging.INFO, "ledger verify: valid=False entries_checked=2"),
    ]
    for record in caplog.records:
        message = record.getMessage()
        assert SECRET not in message
        assert "0.34" not in message
        assert all(entry.hash not in message for entry in entries)


# 13. Pure-function check on the fixture (M5 owns the cross-implementation test)


def test_pure_verify_on_fixture() -> None:
    rows = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert verify_entries(rows) == valid(3)
    rows[1]["payload"]["observed_value"] = "0.35"  # one character changed in entry 2
    assert verify_entries(rows) == invalid(2, 2)


# 15. Guard: runs last in this module (and this module sorts last)


def test_zz_guard_trigger_enabled_and_ledger_empty(owner_engine: Engine) -> None:
    """Checks what the previous test left behind: no pre-test truncate runs in this module."""
    assert trigger_enabled(owner_engine)
    with owner_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM ledger_entry")).scalar_one() == 0
