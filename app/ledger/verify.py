"""Ledger verification (spec: GET /v1/ledger/verify checks; addendum rules 6, 11 and 12).

The result has exactly three fields: valid, entries_checked and first_mismatch, where
first_mismatch is the failing entry's seq as a string, or None when valid.

Entries are read in ascending seq. For each entry, the checks run in this order:

1. seq: the first entry must be seq 1, and every later entry must be the previous seq + 1.
2. prev_hash: equals the previous entry's hash (64 zeros for the first entry).
3. hash: recomputing the hash from the stored fields (re-canonicalizing the payload) gives the
   stored hash. A stored row that cannot be canonicalized, for example a jsonb float, fails this
   check as a mismatch at that seq; verify never raises for it (docs/decisions.md, decision 2).

On the first failure verify stops: valid is false, first_mismatch is that seq as a string and
entries_checked counts the entries examined including the bad one. If every entry passes:
valid is true, first_mismatch is None and entries_checked is the total. An empty ledger is
valid with 0 entries checked.

verify_entries is pure (no database imports). verify_ledger reads the ledger with one ordinary
SELECT in seq order, as the caller's role (the application role): it writes nothing, takes no
advisory lock and never blocks or waits for appends.

Known limits (addendum rule 12): removing entries from the end of the ledger, or forging the
last entry with a consistent hash, is not detectable by the chain alone until Phase 5.

The result is logged on the "ledger" logger: INFO with valid and entries_checked, and on a
mismatch a WARNING with the seq and the failed check (seq, prev_hash, hash or
uncanonicalizable). Payload values and stored hashes are never logged, and the failed check
is not part of the result.
"""

import logging
from collections.abc import Iterable, Mapping
from typing import Any, TypedDict

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.ledger.canonical import GENESIS_PREV_HASH, LedgerError, canonical_json, compute_hash

logger = logging.getLogger("ledger")

_CONTENT_FIELDS = ("seq", "entry_type", "actor_id", "subject_id", "payload", "recorded_at")


class VerifyResult(TypedDict):
    valid: bool
    entries_checked: int
    first_mismatch: str | None


def verify_entries(rows: Iterable[Mapping[str, Any]]) -> VerifyResult:
    """Verify rows given in ascending seq order. Never raises for malformed row content."""
    previous_seq: int | None = None
    previous_hash = GENESIS_PREV_HASH
    entries_checked = 0
    for row in rows:
        entries_checked += 1
        failed_check = _failed_check(row, previous_seq, previous_hash)
        if failed_check is not None:
            seq = row.get("seq")
            logger.warning("ledger verify mismatch: seq=%s check=%s", seq, failed_check)
            return _result(False, entries_checked, str(seq))
        previous_seq = row["seq"]
        previous_hash = row["hash"]
    return _result(True, entries_checked, None)


def verify_ledger(session: Session) -> VerifyResult:
    """Read the whole ledger in seq order with a plain SELECT and verify it."""
    rows = session.execute(
        text(
            "SELECT seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash"
            " FROM ledger_entry ORDER BY seq"
        )
    )
    return verify_entries(dict(row._mapping) for row in rows)


def _failed_check(
    row: Mapping[str, Any], previous_seq: int | None, previous_hash: str
) -> str | None:
    """Name of the first check this row fails, or None if it passes all three."""
    seq = row.get("seq")
    if isinstance(seq, bool) or not isinstance(seq, int):
        return "seq"
    if previous_seq is None:
        if seq != 1:  # the first entry must be seq 1
            return "seq"
    elif seq != previous_seq + 1:  # seq continuity
        return "seq"

    if row.get("prev_hash") != previous_hash:
        return "prev_hash"

    # Recompute from the stored fields, including the stored prev_hash, so this check is
    # independent of the prev_hash link check above.
    content: dict[str, Any] = {field: row.get(field) for field in _CONTENT_FIELDS}
    try:
        recomputed = compute_hash(row["prev_hash"], canonical_json(**content))
    except LedgerError:
        return "uncanonicalizable"
    if recomputed != row.get("hash"):
        return "hash"
    return None


def _result(valid: bool, entries_checked: int, first_mismatch: str | None) -> VerifyResult:
    logger.info("ledger verify: valid=%s entries_checked=%d", valid, entries_checked)
    return {"valid": valid, "entries_checked": entries_checked, "first_mismatch": first_mismatch}
