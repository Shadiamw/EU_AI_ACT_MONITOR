"""ledger.append and ledger.ensure_seed (spec: ledger.append; addendum rules 4 and 5).

Both run inside the caller's open transaction and never commit, roll back or begin one.
The transaction-scoped advisory lock serializes every append until the caller's transaction
ends, so seq is assigned under the lock, a rollback leaves no gap, and two concurrent
appends cannot fork the chain. The advisory lock is the only lock: there is no
SELECT ... FOR UPDATE, which the application role could not run anyway.
"""

import copy
import json
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock
from app.ledger.canonical import (
    GENESIS_PREV_HASH,
    LedgerError,
    Payload,
    canonical_json,
    compute_hash,
    format_recorded_at,
    validate_actor_id,
    validate_entry_type,
    validate_payload,
    validate_subject_id,
)

# ASCII "LEDG". No other part may use this advisory lock key.
ADVISORY_LOCK_KEY = 1279607879


@dataclass(frozen=True)
class LedgerEntry:
    seq: int
    entry_type: str
    actor_id: str
    subject_id: str | None
    payload: Payload
    recorded_at: str
    prev_hash: str
    hash: str


def append(
    session: Session,
    entry_type: str,
    actor_id: str,
    subject_id: str | None,
    payload: Payload,
) -> LedgerEntry:
    """Append one entry as the last write of the caller's transaction and return it."""
    # Validate before locking, so invalid input never holds the lock.
    validate_entry_type(entry_type)
    validate_actor_id(actor_id)
    validate_subject_id(subject_id)
    validate_payload(payload)
    _lock(session)

    # A separate statement after the lock: at READ COMMITTED it takes a fresh snapshot and so
    # sees the head committed by whoever held the lock before us.
    head = session.execute(
        text("SELECT seq, hash FROM ledger_entry ORDER BY seq DESC LIMIT 1")
    ).one_or_none()
    seq, prev_hash = (1, GENESIS_PREV_HASH) if head is None else (head.seq + 1, head.hash)
    recorded_at = format_recorded_at(clock.now())

    stored_payload = copy.deepcopy(payload)
    entry_hash = compute_hash(
        prev_hash,
        canonical_json(
            seq=seq,
            entry_type=entry_type,
            actor_id=actor_id,
            subject_id=subject_id,
            payload=stored_payload,
            recorded_at=recorded_at,
        ),
    )
    session.execute(
        text(
            "INSERT INTO ledger_entry"
            " (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash)"
            " VALUES (:seq, :entry_type, :actor_id, :subject_id, CAST(:payload AS jsonb),"
            " :recorded_at, :prev_hash, :hash)"
        ),
        {
            "seq": seq,
            "entry_type": entry_type,
            "actor_id": actor_id,
            "subject_id": subject_id,
            "payload": json.dumps(stored_payload, ensure_ascii=False),
            "recorded_at": recorded_at,
            "prev_hash": prev_hash,
            "hash": entry_hash,
        },
    )
    return LedgerEntry(
        seq=seq,
        entry_type=entry_type,
        actor_id=actor_id,
        subject_id=subject_id,
        payload=stored_payload,
        recorded_at=recorded_at,
        prev_hash=prev_hash,
        hash=entry_hash,
    )


def ensure_seed(session: Session, payload: Payload) -> None:
    """Append SEED (actor system, no subject) only if the ledger is empty (addendum rule 4).

    The emptiness check runs after the lock, so concurrent startups produce one SEED.
    """
    validate_payload(payload)
    _lock(session)
    if session.execute(text("SELECT EXISTS (SELECT 1 FROM ledger_entry)")).scalar_one():
        return
    append(session, "SEED", "system", None, payload)


def _lock(session: Session) -> None:
    """Check READ COMMITTED (addendum rule 5), then take the transaction-scoped append lock."""
    isolation = session.execute(text("SHOW transaction_isolation")).scalar_one()
    if isolation != "read committed":
        raise LedgerError("ledger append requires READ COMMITTED isolation")
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": ADVISORY_LOCK_KEY})
