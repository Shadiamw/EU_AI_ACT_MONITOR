"""Canonical form and hashing for ledger entries. Pure functions, no database access.

Pinned by the spec (Ledger entry format, Hash definition) and addendum rules 2, 3, 7 and 10:

    canonical = json.dumps({seq, entry_type, actor_id, subject_id, payload, recorded_at},
                           sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    hash      = lowercase hex SHA-256 of utf8(prev_hash + canonical)

Payload values may be str, int, bool, None, and nested objects (str keys) or arrays of these.
Anything else, including any float (1.0, inf, NaN), Decimal, datetime, bytes, set and tuple,
raises LedgerError. Messages name the type and its location in the payload, never the value.

Integers of any size are accepted, including above 2**53; a JavaScript re-verifier would lose
precision on those (docs/decisions.md, decision 6).

Settled decisions (docs/decisions.md):

- recorded_at truncates microseconds to milliseconds; it never rounds, so .999999 gives .999
  and the second never rolls over.
- An aware datetime in another timezone is converted to UTC (same instant). A naive datetime
  is rejected, because its instant is unknown.
- Strings containing U+0000 or a lone surrogate are rejected. U+0000 cannot be stored in a
  PostgreSQL jsonb or text column, and a lone surrogate has no UTF-8 encoding, so neither
  could be stored and hashed as defined.
- actor_id and subject_id must be non-empty strings; subject_id is None for "no subject",
  never "".
- canonical_json validates its input, so a canonical form never exists for an invalid entry.
  Verify (M3) treats a stored row that raises LedgerError here as a mismatch at that seq.
"""

import hashlib
import json
import re
from datetime import UTC, datetime

type JsonValue = str | int | bool | None | dict[str, JsonValue] | list[JsonValue]
type Payload = dict[str, JsonValue]

GENESIS_PREV_HASH = "0" * 64

ENTRY_TYPES: frozenset[str] = frozenset(
    {
        "SEED",
        "EVENT_FLAGGED",
        "GAP_OPENED",
        "GAP_CLOSED",
        "NOTIFICATION_REQUESTED",
        "NOTIFICATION_DELIVERED",
        "NOTIFICATION_FAILED",
        "WEBHOOK_REGISTERED",
        "EVENT_VIEWED",
        "AUDIT_VIEWED",
    }
)

_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_RECORDED_AT_PATTERN = re.compile(
    r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z"
)
_FORBIDDEN_CHARS = re.compile("[\x00\ud800-\udfff]")


class LedgerError(Exception):
    """Internal error for invalid ledger input. Maps to 500 INTERNAL_ERROR at the API edge."""


def validate_entry_type(entry_type: object) -> str:
    if not isinstance(entry_type, str):
        raise LedgerError(f"entry_type must be a str, got {type(entry_type).__name__}")
    if entry_type not in ENTRY_TYPES:
        raise LedgerError("entry_type is not one of the allowed ledger entry types")
    return entry_type


def validate_payload(payload: object) -> Payload:
    """Check that payload is a JSON object made only of the allowed value types."""
    if not isinstance(payload, dict):
        raise LedgerError(f"payload must be a JSON object (dict), got {type(payload).__name__}")
    _validate_value(payload, "$")
    return payload


def validate_actor_id(actor_id: object) -> str:
    """actor_id must be a non-empty str (decision 5)."""
    value = _validate_text(actor_id, "actor_id", allow_none=False)
    assert value is not None
    return value


def validate_subject_id(subject_id: object) -> str | None:
    """subject_id is a non-empty str, or None for "no subject" (decision 5)."""
    return _validate_text(subject_id, "subject_id", allow_none=True)


def format_recorded_at(moment: datetime) -> str:
    """Format an aware datetime as YYYY-MM-DDTHH:MM:SS.mmmZ in UTC, truncating to milliseconds."""
    if not isinstance(moment, datetime):
        raise LedgerError(f"recorded_at must be a datetime, got {type(moment).__name__}")
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise LedgerError("recorded_at must be a timezone-aware datetime, got a naive one")
    utc = moment.astimezone(UTC)
    return (
        f"{utc.year:04d}-{utc.month:02d}-{utc.day:02d}"
        f"T{utc.hour:02d}:{utc.minute:02d}:{utc.second:02d}"
        f".{utc.microsecond // 1000:03d}Z"
    )


def canonical_json(
    *,
    seq: int,
    entry_type: str,
    actor_id: str,
    subject_id: str | None,
    payload: Payload,
    recorded_at: str,
) -> str:
    """The canonical JSON of an entry's six content fields."""
    if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
        raise LedgerError("seq must be an integer >= 1")
    content = {
        "seq": seq,
        "entry_type": validate_entry_type(entry_type),
        "actor_id": validate_actor_id(actor_id),
        "subject_id": validate_subject_id(subject_id),
        "payload": validate_payload(payload),
        "recorded_at": _validate_recorded_at(recorded_at),
    }
    return json.dumps(
        content, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def compute_hash(prev_hash: str, canonical: str) -> str:
    """Lowercase hex SHA-256 of the UTF-8 bytes of prev_hash + canonical."""
    if not isinstance(prev_hash, str) or not _HASH_PATTERN.fullmatch(prev_hash):
        raise LedgerError("prev_hash must be 64 lowercase hex characters")
    if not isinstance(canonical, str):
        raise LedgerError(f"canonical must be a str, got {type(canonical).__name__}")
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def _validate_value(value: object, path: str) -> None:
    # float is not an int subclass, so every float (1.0, inf, NaN) reaches the final raise.
    if value is None or isinstance(value, bool | int):
        return
    if isinstance(value, str):
        _check_string(value, f"payload value at {path}")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise LedgerError(
                    f"payload object key at {path} must be a str, got {type(key).__name__}"
                )
            _check_string(key, f"payload object key at {path}")
            _validate_value(item, f"{path}.{key}")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_value(item, f"{path}[{index}]")
        return
    raise LedgerError(f"payload value at {path} has unsupported type {type(value).__name__}")


def _check_string(value: str, where: str) -> None:
    if _FORBIDDEN_CHARS.search(value):
        raise LedgerError(f"{where} contains U+0000 or a lone surrogate")


def _validate_text(value: object, name: str, *, allow_none: bool) -> str | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not value:
        raise LedgerError(f"{name} must be a non-empty str")
    _check_string(value, name)
    return value


def _validate_recorded_at(value: object) -> str:
    if not isinstance(value, str) or not _RECORDED_AT_PATTERN.fullmatch(value):
        raise LedgerError("recorded_at must be a string in YYYY-MM-DDTHH:MM:SS.mmmZ form")
    return value
