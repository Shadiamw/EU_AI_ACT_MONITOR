#!/usr/bin/env python3
"""Standalone ledger verifier: re-checks a ledger export without the server.

Usage:
    python verify_ledger.py EXPORT_FILE

EXPORT_FILE is the JSON array returned by GET /v1/ledger/export. The script prints one JSON
object on stdout, {"valid": ..., "entries_checked": ..., "first_mismatch": ...}, and exits 0
if the export is valid, 1 if it is not (or cannot be read), and 2 on a usage error. Reasons
and the failed check go to stderr; no payload value or stored hash is ever printed.

It uses only the Python standard library and imports nothing from the server's code. It is
written for Python 3.9 or newer and tested on Python 3.12 (docs/decisions.md 52 to 61).

Hash definition (pinned by the spec and addendum rule 7):

    canonical = JSON of {seq, entry_type, actor_id, subject_id, payload, recorded_at}
                with keys sorted at every level, separators "," and ":", and non-ASCII
                characters written as themselves (ensure_ascii False)
    hash      = lowercase hex SHA-256 of the UTF-8 bytes of (prev_hash + canonical)

Entry 1's prev_hash is 64 zeros.

Checks, for each entry in ascending stored seq; verification stops at the first failure:

1. seq: an integer (not a boolean); the first entry is 1 and each later one is previous + 1.
2. prev_hash: equals the previous entry's stored hash (64 zeros for the first entry).
3. hash: the hash recomputed from the stored content fields and the stored prev_hash equals
   the stored hash. An entry whose content cannot be canonicalized (a missing content field,
   a non-integer number, a string with U+0000 or a lone surrogate, a value of the wrong type
   or format) fails this check as "uncanonicalizable".

Unknown extra keys in an entry are ignored: the hash covers only the six content fields.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from collections.abc import Iterable, Mapping
from typing import Any

GENESIS_PREV_HASH = "0" * 64

ENTRY_TYPES = frozenset(
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

CONTENT_FIELDS = ("seq", "entry_type", "actor_id", "subject_id", "payload", "recorded_at")

_RECORDED_AT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")
_LOWER_HEX_64 = re.compile(r"[0-9a-f]{64}")
_FORBIDDEN_CHARS = re.compile("[\x00\ud800-\udfff]")

USAGE = "usage: verify_ledger.py EXPORT_FILE"


class NonInteger:
    """A JSON number that is not an integer (1.5, 1.0, 1e3) or NaN, Infinity, -Infinity.

    Parsing keeps it as its source text instead of a float, so it can never be mistaken for
    an integer and never canonicalizes.
    """

    __slots__ = ("text",)

    def __init__(self, text: str) -> None:
        self.text = text

    def __repr__(self) -> str:
        return self.text


class UnusableInput(Exception):
    """The file cannot be read as a ledger export at all."""


def load_export(data: bytes) -> list[dict[str, Any]]:
    """Parse export bytes into a list of entry objects, or raise UnusableInput."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnusableInput(f"the file is not valid UTF-8 (byte offset {exc.start})") from None
    try:
        parsed = json.loads(text, parse_float=NonInteger, parse_constant=NonInteger)
    except RecursionError:
        raise UnusableInput("the file is nested too deeply to parse") from None
    except ValueError as exc:
        reason = exc.msg if isinstance(exc, json.JSONDecodeError) else "unparseable value"
        raise UnusableInput(f"the file is not valid JSON ({reason})") from None
    if not isinstance(parsed, list):
        raise UnusableInput("the top level of the file is not a JSON array")
    for index, element in enumerate(parsed):
        if not isinstance(element, dict):
            raise UnusableInput(f"element {index} of the array is not a JSON object")
    return parsed


def verify_entries(rows: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Verify entries (in any order) and return {valid, entries_checked, first_mismatch}."""
    return check_entries(rows)[0]


def check_entries(rows: Iterable[Mapping[str, Any]]) -> tuple[dict[str, Any], str | None]:
    """Like verify_entries, and also return the name of the failed check (or None)."""
    entries_checked = 0
    previous_seq: int | None = None
    previous_hash = GENESIS_PREV_HASH
    for row in sorted(rows, key=_order_key):
        entries_checked += 1
        failed = _failed_check(row, previous_seq, previous_hash)
        if failed is not None:
            result = _result(False, entries_checked, str(row.get("seq")))
            return result, failed
        previous_seq = row["seq"]
        previous_hash = row["hash"]
    return _result(True, entries_checked, None), None


def entry_hash(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def canonical_content(row: Mapping[str, Any]) -> str | None:
    """The canonical JSON of the six content fields, or None if they cannot be canonicalized."""
    if any(field not in row for field in CONTENT_FIELDS):
        return None
    seq = row["seq"]
    entry_type = row["entry_type"]
    actor_id = row["actor_id"]
    subject_id = row["subject_id"]
    payload = row["payload"]
    recorded_at = row["recorded_at"]
    try:
        content_ok = (
            _is_integer(seq)
            and seq >= 1
            and isinstance(entry_type, str)
            and entry_type in ENTRY_TYPES
            and _is_identifier(actor_id)
            and (subject_id is None or _is_identifier(subject_id))
            and isinstance(payload, dict)
            and _is_json_value(payload)
            and isinstance(recorded_at, str)
            and _RECORDED_AT.fullmatch(recorded_at) is not None
        )
    except RecursionError:
        return None
    if not content_ok:
        return None
    content = {
        "seq": seq,
        "entry_type": entry_type,
        "actor_id": actor_id,
        "subject_id": subject_id,
        "payload": payload,
        "recorded_at": recorded_at,
    }
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print(USAGE, file=sys.stderr)
        return 2
    try:
        with open(argv[0], "rb") as export_file:
            data = export_file.read()
        rows = load_export(data)
    except OSError as exc:
        return _unusable(f"cannot read the file ({exc.strerror or type(exc).__name__})")
    except UnusableInput as exc:
        return _unusable(str(exc))
    result, failed = check_entries(rows)
    if failed is not None:
        print(
            f"ledger verify mismatch: seq={result['first_mismatch']} check={failed}",
            file=sys.stderr,
        )
    print(json.dumps(result))
    return 0 if result["valid"] else 1


def _failed_check(
    row: Mapping[str, Any], previous_seq: int | None, previous_hash: str
) -> str | None:
    seq = row.get("seq")
    if not _is_integer(seq):
        return "seq"
    if previous_seq is None:
        if seq != 1:
            return "seq"
    elif seq != previous_seq + 1:
        return "seq"

    if row.get("prev_hash") != previous_hash:
        return "prev_hash"

    canonical = canonical_content(row)
    stored_prev_hash = row["prev_hash"]
    if (
        canonical is None
        or not isinstance(stored_prev_hash, str)
        or _LOWER_HEX_64.fullmatch(stored_prev_hash) is None
    ):
        return "uncanonicalizable"
    # The stored prev_hash, not the previous entry's hash: this check stands on its own.
    if entry_hash(stored_prev_hash, canonical) != row.get("hash"):
        return "hash"
    return None


def _order_key(row: Mapping[str, Any]) -> tuple[int, int]:
    """Ascending stored seq. Rows without a usable seq go first, so they fail at once."""
    seq = row.get("seq")
    if isinstance(seq, int) and not isinstance(seq, bool):
        return (1, seq)
    return (0, 0)


def _is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and value != "" and _FORBIDDEN_CHARS.search(value) is None


def _is_json_value(value: object) -> bool:
    """Strings, integers, booleans, null, and objects (string keys) or arrays of these."""
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, str):
        return _FORBIDDEN_CHARS.search(value) is None
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _FORBIDDEN_CHARS.search(key) is None and _is_json_value(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return all(_is_json_value(item) for item in value)
    return False  # NonInteger, a Python float, or any other type


def _result(valid: bool, entries_checked: int, first_mismatch: str | None) -> dict[str, Any]:
    return {"valid": valid, "entries_checked": entries_checked, "first_mismatch": first_mismatch}


def _unusable(reason: str) -> int:
    print(f"unusable input: {reason}", file=sys.stderr)
    print(json.dumps(_result(False, 0, None)))
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
