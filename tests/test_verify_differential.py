"""M5 E: differential test of the script's pure function against the server's.

Both implementations get the same mutated rows. The server's verify_entries gets them in
stored-seq order, as verify_ledger does (ORDER BY seq, decision 23); the script gets them
shuffled and sorts them itself (decision 58). They must agree on valid, entries_checked and
first_mismatch, except for degenerate-seq rows, where only valid must agree (decision 59).
"""

import copy
import logging
import random
from collections import Counter
from typing import Any

import pytest

from app.ledger import verify_entries as server_verify
from app.ledger.canonical import ENTRY_TYPES, GENESIS_PREV_HASH, canonical_json, compute_hash
from tools import verify_ledger as script

SEED = 20261012
CASES = 300
FIELDS = ["seq", "entry_type", "actor_id", "subject_id", "payload", "recorded_at", "prev_hash"]
FIELDS.append("hash")
WORDS = ["evt", "sys-42", "é", "日本語", "ключ", "naïve", "0.34", "", "a b", "☃"]
MUTATIONS = [
    "change_field",
    "remove_field",
    "delete_row",
    "swap_rows",
    "inject_float",
    "extra_key",
    "forge_own_hash",
]


def random_value(rng: random.Random, depth: int) -> Any:
    kind = rng.choice(["str", "int", "bool", "null", "list", "dict"] if depth < 3 else ["str"])
    if kind == "str":
        return rng.choice(WORDS) + str(rng.randint(0, 99))
    if kind == "int":
        return rng.choice([0, -7, 42, 2**53 + 1, -(2**63), rng.randint(-1000, 1000)])
    if kind == "bool":
        return rng.choice([True, False])
    if kind == "null":
        return None
    if kind == "list":
        return [random_value(rng, depth + 1) for _ in range(rng.randint(0, 3))]
    return random_payload(rng, depth + 1)


def random_payload(rng: random.Random, depth: int = 0) -> dict[str, Any]:
    return {rng.choice(WORDS) + str(i): random_value(rng, depth) for i in range(rng.randint(0, 4))}


def random_chain(rng: random.Random) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    prev_hash = GENESIS_PREV_HASH
    for seq in range(1, rng.randint(1, 12) + 1):
        row: dict[str, Any] = {
            "seq": seq,
            "entry_type": rng.choice(sorted(ENTRY_TYPES)),
            "actor_id": rng.choice(["system", "sys-42", "viewer-1", "rev-1"]),
            "subject_id": rng.choice([None, f"evt-{rng.randint(1, 5)}", "wh-ü"]),
            "payload": random_payload(rng),
            "recorded_at": f"2026-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}T"
            f"{rng.randint(0, 23):02d}:{rng.randint(0, 59):02d}:{rng.randint(0, 59):02d}."
            f"{rng.randint(0, 999):03d}Z",
            "prev_hash": prev_hash,
        }
        row["hash"] = compute_hash(prev_hash, canonical_json(**{f: row[f] for f in FIELDS[:6]}))
        prev_hash = row["hash"]
        rows.append(row)
    return rows


def changed_value(rng: random.Random, field: str, row: dict[str, Any]) -> Any:
    """Another value for the field, in a valid format."""
    if field == "seq":
        return row["seq"] + 100
    if field == "entry_type":
        return rng.choice(sorted(ENTRY_TYPES - {row["entry_type"]}))
    if field == "actor_id":
        return row["actor_id"] + "-x"
    if field == "subject_id":
        return "evt-other" if row["subject_id"] is None else None
    if field == "payload":
        return {**row["payload"], "tampered": "1"}
    if field == "recorded_at":
        return "2027-01-01T00:00:00.000Z"
    return format(int(row[field], 16) ^ 1, "064x")  # prev_hash or hash: flip the last bit


def mutate(rng: random.Random, rows: list[dict[str, Any]], kind: str) -> str:
    """Apply one mutation in place; return a description."""
    k = rng.randrange(len(rows))
    if kind == "swap_rows" and len(rows) < 2:
        kind = "delete_row"
    if kind == "change_field":
        field = rng.choice(FIELDS)
        rows[k][field] = changed_value(rng, field, rows[k])
        return f"change {field} of row {k + 1}"
    if kind == "remove_field":
        field = rng.choice(FIELDS)
        del rows[k][field]
        return f"remove {field} of row {k + 1}"
    if kind == "delete_row":
        del rows[k]
        return f"delete row {k + 1}"
    if kind == "swap_rows":
        i, j = rng.sample(range(len(rows)), 2)
        rows[i]["seq"], rows[j]["seq"] = rows[j]["seq"], rows[i]["seq"]
        return f"swap seqs of rows {i + 1} and {j + 1}"
    if kind == "inject_float":
        value = rng.choice([1.5, 1.0, 1000.0, float("nan"), float("inf"), float("-inf")])
        target: Any = rows[k]["payload"]
        target[rng.choice(WORDS)] = value if rng.random() < 0.5 else [value]
        return f"inject {value!r} into the payload of row {k + 1}"
    if kind == "extra_key":
        rows[k]["unexpected"] = random_value(rng, 0)
        return f"extra key on row {k + 1}"
    rows[k]["payload"] = {**rows[k]["payload"], "forged": "1"}
    rows[k]["hash"] = compute_hash(
        rows[k]["prev_hash"], canonical_json(**{f: rows[k][f] for f in FIELDS[:6]})
    )
    return f"forge row {k + 1} with its own hash recomputed"


def is_integer(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def server_check(caplog: pytest.LogCaptureFixture) -> str | None:
    names = [
        r.getMessage().rsplit("check=", 1)[1]
        for r in caplog.records
        if r.levelno == logging.WARNING
    ]
    return names[-1] if names else None


def test_script_and_server_agree_on_random_single_mutations(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="ledger")
    rng = random.Random(SEED)
    kinds: Counter[str] = Counter()
    degenerate = 0
    disagreements: list[str] = []
    for case in range(CASES):
        rows = random_chain(rng)
        assert server_verify(rows) == {
            "valid": True,
            "entries_checked": len(rows),
            "first_mismatch": None,
        }
        kind = rng.choice(MUTATIONS)
        description = mutate(rng, rows, kind)
        kinds[kind] += 1
        seq_ok = all(is_integer(row.get("seq")) for row in rows)
        server_rows = sorted(rows, key=lambda r: r["seq"]) if seq_ok else rows
        script_rows = copy.deepcopy(rows)
        rng.shuffle(script_rows)
        caplog.clear()
        expected = server_verify(copy.deepcopy(server_rows))
        expected_check = server_check(caplog)
        actual, actual_check = script.check_entries(script_rows)
        if seq_ok:
            # Same result and the same failed check name (decision 60).
            agree = (actual, actual_check) == (expected, expected_check)
        else:
            degenerate += 1
            agree = actual["valid"] == expected["valid"]
        if not agree:
            disagreements.append(
                f"case {case}: {description}: server {expected} {expected_check},"
                f" script {actual} {actual_check}"
            )
    print(
        f"\ndifferential: seed {SEED}, {CASES} cases, {degenerate} degenerate-seq cases, "
        f"mutations {dict(sorted(kinds.items()))}, disagreements {len(disagreements)}"
    )
    assert disagreements == []
