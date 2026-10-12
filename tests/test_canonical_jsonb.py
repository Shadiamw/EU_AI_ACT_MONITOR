"""M1: payloads survive a real PostgreSQL jsonb round trip with an unchanged hash.

Rows are inserted with plain test SQL as the application role, not through any append
function, and the ledger is truncated as the owner afterwards.
"""

import json
from collections.abc import Iterator
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app.ledger.canonical import GENESIS_PREV_HASH, Payload, canonical_json, compute_hash

CASES: dict[str, Payload] = {
    "nested-key-order": {"b": {"y": 1, "x": [{"d": 1, "c": 2}]}, "a": "1"},
    "non-ascii": {"accented": "café Zürich", "cjk": "日本語の文字", "emoji": "😀🚀"},
    "numeric-strings": {"observed_value": "0.34", "threshold_value": "0.340"},
    "booleans": {"flag": True, "off": False},
    "empty-object": {},
    "edge-values": {
        "empty_object": {},
        "empty_list": [],
        "empty_string": "",
        "none": None,
        "negative": -42,
        "zero": 0,
        "big": 2**53 + 1,
    },
    "special-characters": {"s": 'quote" backslash\\ newline\n tab\t ctrl\x01 del\x7f ls '},
    "fixture-seed": {"systems": ["sys-42"], "threshold_count": "2"},
}

RECORDED_AT = "2026-09-27T10:05:00.250Z"


@pytest.fixture
def cleanup(owner_engine: Engine) -> Iterator[None]:
    yield
    with owner_engine.begin() as conn:
        conn.execute(text("TRUNCATE ledger_entry"))


def test_column_types_match_addendum_rule_3(owner_engine: Engine) -> None:
    with owner_engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT column_name, data_type FROM information_schema.columns"
                " WHERE table_name = 'ledger_entry'"
            )
        ).all()
    types: dict[str, str] = {row.column_name: row.data_type for row in rows}
    assert types["recorded_at"] == "text"
    assert types["payload"] == "jsonb"


@pytest.mark.parametrize("subject_id", ["evt-1", None])
@pytest.mark.parametrize("case", sorted(CASES))
def test_jsonb_round_trip_keeps_hash(
    app_engine: Engine, cleanup: None, case: str, subject_id: str | None
) -> None:
    payload = CASES[case]
    fields: dict[str, Any] = {
        "seq": 1,
        "entry_type": "EVENT_FLAGGED",
        "actor_id": "sys-42",
        "subject_id": subject_id,
        "recorded_at": RECORDED_AT,
    }
    stored_hash = compute_hash(GENESIS_PREV_HASH, canonical_json(**fields, payload=payload))

    with app_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ledger_entry"
                " (seq, entry_type, actor_id, subject_id, payload, recorded_at, prev_hash, hash)"
                " VALUES (:seq, :entry_type, :actor_id, :subject_id, CAST(:payload AS jsonb),"
                " :recorded_at, :prev_hash, :hash)"
            ),
            # The payload goes in as ASCII-escaped, spaced JSON text, not the canonical form.
            fields
            | {"payload": json.dumps(payload), "prev_hash": GENESIS_PREV_HASH, "hash": stored_hash},
        )
    with app_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT seq, entry_type, actor_id, subject_id, payload, recorded_at, hash"
                " FROM ledger_entry"
            )
        ).one()

    assert row.recorded_at == RECORDED_AT
    assert row.payload == payload
    recomputed = compute_hash(
        GENESIS_PREV_HASH,
        canonical_json(
            seq=row.seq,
            entry_type=row.entry_type,
            actor_id=row.actor_id,
            subject_id=row.subject_id,
            payload=row.payload,
            recorded_at=row.recorded_at,
        ),
    )
    assert recomputed == row.hash == stored_hash
