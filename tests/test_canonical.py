"""M1: canonical form, hashing, recorded_at formatting and input validation (pure functions)."""

import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from app.ledger.canonical import (
    ENTRY_TYPES,
    GENESIS_PREV_HASH,
    LedgerError,
    Payload,
    canonical_json,
    compute_hash,
    format_recorded_at,
    validate_entry_type,
    validate_payload,
)

FIXTURE = Path(__file__).parent / "fixtures" / "fixture_export.json"

# Addendum rule 7, pasted verbatim. Not derived from this code or from the fixture file.
KNOWN_ANSWERS = [
    (
        "0000000000000000000000000000000000000000000000000000000000000000",
        '{"actor_id":"system","entry_type":"SEED","payload":{"systems":["sys-42"],'
        '"threshold_count":"2"},"recorded_at":"2026-09-27T10:00:00.000Z","seq":1,'
        '"subject_id":null}',
        "3e17607bd7884dfbaff84c7a4a5a4762d56f329c1f63a91420831a1078db080c",
    ),
    (
        "3e17607bd7884dfbaff84c7a4a5a4762d56f329c1f63a91420831a1078db080c",
        '{"actor_id":"sys-42","entry_type":"EVENT_FLAGGED","payload":{"event_id":"evt-7f3a",'
        '"metric_name":"accuracy_drift","observed_value":"0.34","system_id":"sys-42",'
        '"threshold_value":"0.30","threshold_version_id":"thr-1"},'
        '"recorded_at":"2026-09-27T10:05:00.250Z","seq":2,"subject_id":"evt-7f3a"}',
        "e6ba0c725b25a5aa1c3f40d9c26e1823c01798d77f7ad5ed63be90894d0ad92c",
    ),
    (
        "e6ba0c725b25a5aa1c3f40d9c26e1823c01798d77f7ad5ed63be90894d0ad92c",
        '{"actor_id":"viewer-1","entry_type":"EVENT_VIEWED","payload":{},'
        '"recorded_at":"2026-09-27T10:06:00.000Z","seq":3,"subject_id":"evt-7f3a"}',
        "d557f21d771adea81d93c2aab306f00d61898207c26d1f8bc767a828efa6a5b2",
    ),
]

# The same three entries as Python values, typed in by hand (fields in non-sorted order).
KNOWN_ENTRIES: list[dict[str, Any]] = [
    {
        "seq": 1,
        "entry_type": "SEED",
        "actor_id": "system",
        "subject_id": None,
        "payload": {"threshold_count": "2", "systems": ["sys-42"]},
        "recorded_at": "2026-09-27T10:00:00.000Z",
    },
    {
        "seq": 2,
        "entry_type": "EVENT_FLAGGED",
        "actor_id": "sys-42",
        "subject_id": "evt-7f3a",
        "payload": {
            "threshold_version_id": "thr-1",
            "event_id": "evt-7f3a",
            "system_id": "sys-42",
            "metric_name": "accuracy_drift",
            "observed_value": "0.34",
            "threshold_value": "0.30",
        },
        "recorded_at": "2026-09-27T10:05:00.250Z",
    },
    {
        "seq": 3,
        "entry_type": "EVENT_VIEWED",
        "actor_id": "viewer-1",
        "subject_id": "evt-7f3a",
        "payload": {},
        "recorded_at": "2026-09-27T10:06:00.000Z",
    },
]

CONTENT_FIELDS = ("seq", "entry_type", "actor_id", "subject_id", "payload", "recorded_at")


def entry_with(payload: Payload, subject_id: str | None = "evt-1") -> dict[str, Any]:
    return {
        "seq": 1,
        "entry_type": "EVENT_FLAGGED",
        "actor_id": "sys-42",
        "subject_id": subject_id,
        "payload": payload,
        "recorded_at": "2026-09-27T10:05:00.250Z",
    }


def hash_of(entry: dict[str, Any], prev_hash: str = GENESIS_PREV_HASH) -> str:
    return compute_hash(prev_hash, canonical_json(**entry))


# 1. Known answers from addendum rule 7


def test_genesis_prev_hash_is_64_zeros() -> None:
    assert GENESIS_PREV_HASH == "0" * 64


@pytest.mark.parametrize("index", [0, 1, 2])
def test_known_answers_reproduce_exactly(index: int) -> None:
    prev_hash, expected_canonical, expected_hash = KNOWN_ANSWERS[index]
    canonical = canonical_json(**KNOWN_ENTRIES[index])
    assert canonical == expected_canonical
    assert compute_hash(prev_hash, canonical) == expected_hash
    assert compute_hash(prev_hash, expected_canonical) == expected_hash


def test_known_answers_chain() -> None:
    assert KNOWN_ANSWERS[0][0] == GENESIS_PREV_HASH
    for previous, current in zip(KNOWN_ANSWERS, KNOWN_ANSWERS[1:], strict=False):
        assert current[0] == previous[2]


# 2. Fixture (unit check of the hash function, not verify)


def load_fixture() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return entries


def test_fixture_hashes_and_links_recompute() -> None:
    entries = load_fixture()
    assert len(entries) == 3
    prev_hash = GENESIS_PREV_HASH
    for entry in entries:
        assert entry["prev_hash"] == prev_hash
        content = {field: entry[field] for field in CONTENT_FIELDS}
        assert compute_hash(entry["prev_hash"], canonical_json(**content)) == entry["hash"]
        prev_hash = entry["hash"]


def test_fixture_one_character_change_in_entry_2_payload_changes_hash() -> None:
    entry = load_fixture()[1]
    entry["payload"]["observed_value"] = "0.35"  # was "0.34"
    content = {field: entry[field] for field in CONTENT_FIELDS}
    assert compute_hash(entry["prev_hash"], canonical_json(**content)) != entry["hash"]


# 3. Canonical form cases


def test_nested_key_order_does_not_matter() -> None:
    first: Payload = {"b": {"y": 1, "x": [{"d": 1, "c": 2}]}, "a": "1"}
    second: Payload = {"a": "1", "b": {"x": [{"c": 2, "d": 1}], "y": 1}}
    assert canonical_json(**entry_with(first)) == canonical_json(**entry_with(second))
    assert hash_of(entry_with(first)) == hash_of(entry_with(second))
    assert '"payload":{"a":"1","b":{"x":[{"c":2,"d":1}],"y":1}}' in canonical_json(
        **entry_with(first)
    )


def test_list_order_matters() -> None:
    assert hash_of(entry_with({"systems": ["a", "b"]})) != hash_of(
        entry_with({"systems": ["b", "a"]})
    )


NON_ASCII: Payload = {"accented": "café Zürich", "cjk": "日本語の文字", "emoji": "😀🚀"}


def test_non_ascii_is_not_escaped() -> None:
    canonical = canonical_json(**entry_with(NON_ASCII))
    assert "\\u" not in canonical
    assert "café Zürich" in canonical
    assert "日本語の文字" in canonical
    assert "😀🚀" in canonical


def test_non_ascii_hashes_identically_after_round_trip() -> None:
    round_tripped = json.loads(json.dumps(NON_ASCII))  # ASCII-escaped text form and back
    assert hash_of(entry_with(round_tripped)) == hash_of(entry_with(NON_ASCII))


def test_numeric_strings_are_distinct_strings() -> None:
    assert hash_of(entry_with({"value": "0.34"})) != hash_of(entry_with({"value": "0.340"}))
    assert '"value":"0.34"' in canonical_json(**entry_with({"value": "0.34"}))


def test_true_is_serialized_as_true_not_one() -> None:
    canonical = canonical_json(**entry_with({"flag": True, "off": False}))
    assert '"flag":true' in canonical
    assert '"off":false' in canonical
    assert hash_of(entry_with({"flag": True})) != hash_of(entry_with({"flag": 1}))


def test_edge_values() -> None:
    big = 2**53 + 1
    payload: Payload = {"empty_object": {}, "empty_string": "", "none": None, "big": big}
    payload |= {"negative": -42, "zero": 0, "empty_list": []}
    canonical = canonical_json(**entry_with(payload, subject_id=None))
    assert (
        '"payload":{"big":9007199254740993,"empty_list":[],"empty_object":{},'
        '"empty_string":"","negative":-42,"none":null,"zero":0}' in canonical
    )
    assert '"subject_id":null' in canonical
    assert hash_of(entry_with(json.loads(json.dumps(payload)), subject_id=None)) == hash_of(
        entry_with(payload, subject_id=None)
    )


def test_empty_payload_object() -> None:
    assert '"payload":{}' in canonical_json(**entry_with({}))


def test_special_characters() -> None:
    payload: Payload = {"s": 'quote" backslash\\ newline\n tab\t ctrl\x01 del\x7f ls '}
    canonical = canonical_json(**entry_with(payload))
    # json.dumps escapes quote, backslash and C0 controls; U+007F and U+2028 stay raw.
    assert '"s":"quote\\" backslash\\\\ newline\\n tab\\t ctrl\\u0001 del\x7f ls "' in canonical
    assert hash_of(entry_with(json.loads(json.dumps(payload)))) == hash_of(entry_with(payload))


def test_hash_is_lowercase_hex_64() -> None:
    digest = hash_of(entry_with({}))
    assert len(digest) == 64
    assert digest == digest.lower()
    int(digest, 16)


def test_prev_hash_is_part_of_the_hash() -> None:
    assert hash_of(entry_with({}), GENESIS_PREV_HASH) != hash_of(entry_with({}), "f" * 64)


# 5. recorded_at formatting


@pytest.mark.parametrize(
    ("microsecond", "expected"),
    [
        (0, "2026-09-27T10:05:00.000Z"),
        (250000, "2026-09-27T10:05:00.250Z"),
        (999999, "2026-09-27T10:05:00.999Z"),
        (1500, "2026-09-27T10:05:00.001Z"),
        (999, "2026-09-27T10:05:00.000Z"),
    ],
)
def test_recorded_at_truncates_to_milliseconds(microsecond: int, expected: str) -> None:
    moment = datetime(2026, 9, 27, 10, 5, 0, microsecond, tzinfo=UTC)
    formatted = format_recorded_at(moment)
    assert formatted == expected
    assert "+00:00" not in formatted
    assert formatted.endswith("Z")


def test_recorded_at_converts_other_timezones_to_utc() -> None:
    plus_two = timezone(timedelta(hours=2))
    moment = datetime(2026, 9, 27, 1, 5, 0, 250000, tzinfo=plus_two)
    assert format_recorded_at(moment) == "2026-09-26T23:05:00.250Z"


def test_recorded_at_rejects_naive_datetime() -> None:
    with pytest.raises(LedgerError, match="naive"):
        format_recorded_at(datetime(2026, 9, 27, 10, 5, 0))  # noqa: DTZ001


def test_recorded_at_rejects_non_datetime() -> None:
    with pytest.raises(LedgerError):
        format_recorded_at(date(2026, 9, 27))  # type: ignore[arg-type]


# 6. Rejections


SECRET = "987.654"  # appears in rejected values; must never appear in error messages


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"v": 1.0}, id="float-1.0"),
        pytest.param({"v": 0.34}, id="float"),
        pytest.param({"v": float("inf")}, id="float-inf"),
        pytest.param({"v": float("-inf")}, id="float-minus-inf"),
        pytest.param({"v": float("nan")}, id="nan"),
        pytest.param({"v": Decimal(SECRET)}, id="decimal"),
        pytest.param({"v": datetime(2026, 9, 27, tzinfo=UTC)}, id="datetime"),
        pytest.param({"v": SECRET.encode()}, id="bytes"),
        pytest.param({"v": {SECRET}}, id="set"),
        pytest.param({"v": (SECRET,)}, id="tuple"),
        pytest.param({1: SECRET}, id="int-key"),
        pytest.param({"outer": {"inner": [{2: SECRET}]}}, id="nested-int-key"),
        pytest.param({"a": {"b": [1, "x", {"c": [float(SECRET)]}]}}, id="deep-float"),
        pytest.param({"a": {"b": [1, "x", {"c": [Decimal(SECRET)]}]}}, id="deep-decimal"),
        pytest.param({"v": "nul\x00" + SECRET}, id="nul-char"),
        pytest.param({"v": "\ud800" + SECRET}, id="lone-surrogate"),
    ],
)
def test_payload_rejections(payload: Any) -> None:
    with pytest.raises(LedgerError) as excinfo:
        validate_payload(payload)
    assert SECRET not in str(excinfo.value)
    with pytest.raises(LedgerError):
        canonical_json(**entry_with(payload))


def test_rejection_message_names_location_and_type() -> None:
    with pytest.raises(LedgerError) as excinfo:
        validate_payload({"a": {"b": [1, {"c": [Decimal(SECRET)]}]}})
    assert str(excinfo.value) == "payload value at $.a.b[1].c[0] has unsupported type Decimal"


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param([SECRET], id="list"),
        pytest.param(SECRET, id="str"),
        pytest.param(None, id="none"),
        pytest.param(1, id="int"),
    ],
)
def test_payload_must_be_an_object(payload: Any) -> None:
    with pytest.raises(LedgerError, match="payload must be a JSON object") as excinfo:
        validate_payload(payload)
    assert SECRET not in str(excinfo.value)


@pytest.mark.parametrize(
    "entry_type",
    [
        pytest.param("EVENT_FLAGED", id="typo"),
        pytest.param("event_flagged", id="lowercase"),
        pytest.param("", id="empty"),
        pytest.param(1, id="int"),
        pytest.param(None, id="none"),
        pytest.param(b"SEED", id="bytes"),
    ],
)
def test_entry_type_rejections(entry_type: Any) -> None:
    with pytest.raises(LedgerError):
        validate_entry_type(entry_type)
    with pytest.raises(LedgerError):
        canonical_json(**(entry_with({}) | {"entry_type": entry_type}))


def test_all_ten_entry_types_are_accepted() -> None:
    assert len(ENTRY_TYPES) == 10
    for entry_type in ENTRY_TYPES:
        assert validate_entry_type(entry_type) == entry_type


def test_allowed_values_are_accepted() -> None:
    payload: Payload = {"s": "x", "i": 3, "b": False, "n": None, "o": {"l": [1, "2", True, None]}}
    assert validate_payload(payload) is payload


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("seq", 0),
        ("seq", True),
        ("seq", "1"),
        ("actor_id", ""),
        ("actor_id", None),
        ("subject_id", 5),
        ("recorded_at", "2026-09-27T10:05:00Z"),
        ("recorded_at", "2026-09-27T10:05:00.250+00:00"),
        ("recorded_at", "٢٠٢٦-09-27T10:05:00.250Z"),
    ],
)
def test_other_field_rejections(field: str, value: Any) -> None:
    with pytest.raises(LedgerError):
        canonical_json(**(entry_with({}) | {field: value}))


@pytest.mark.parametrize("prev_hash", ["", "0" * 63, "A" * 64, "g" * 64])
def test_compute_hash_rejects_malformed_prev_hash(prev_hash: str) -> None:
    with pytest.raises(LedgerError):
        compute_hash(prev_hash, "{}")
