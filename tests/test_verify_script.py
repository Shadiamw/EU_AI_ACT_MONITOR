"""M5: tools/verify_ledger.py on its own (known answers, imports, file-only cases, scale).

Expected values are reasoned out from the spec, the addendum and docs/decisions.md in
comments next to each assertion; they are not copied from output.
"""

import ast
import copy
import hashlib
import inspect
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from app.ledger.canonical import GENESIS_PREV_HASH, canonical_json, compute_hash
from tests.script_helpers import (
    FIXTURE,
    SCRIPT,
    fixture_rows,
    run_main,
    run_subprocess,
    write_rows,
)
from tools import verify_ledger as script

# Addendum rule 7, pasted as literals.
CANONICAL_1 = (
    '{"actor_id":"system","entry_type":"SEED","payload":{"systems":["sys-42"],'
    '"threshold_count":"2"},"recorded_at":"2026-09-27T10:00:00.000Z","seq":1,"subject_id":null}'
)
HASH_1 = "3e17607bd7884dfbaff84c7a4a5a4762d56f329c1f63a91420831a1078db080c"
CANONICAL_2 = (
    '{"actor_id":"sys-42","entry_type":"EVENT_FLAGGED","payload":{"event_id":"evt-7f3a",'
    '"metric_name":"accuracy_drift","observed_value":"0.34","system_id":"sys-42",'
    '"threshold_value":"0.30","threshold_version_id":"thr-1"},'
    '"recorded_at":"2026-09-27T10:05:00.250Z","seq":2,"subject_id":"evt-7f3a"}'
)
HASH_2 = "e6ba0c725b25a5aa1c3f40d9c26e1823c01798d77f7ad5ed63be90894d0ad92c"
CANONICAL_3 = (
    '{"actor_id":"viewer-1","entry_type":"EVENT_VIEWED","payload":{},'
    '"recorded_at":"2026-09-27T10:06:00.000Z","seq":3,"subject_id":"evt-7f3a"}'
)
HASH_3 = "d557f21d771adea81d93c2aab306f00d61898207c26d1f8bc767a828efa6a5b2"

VALID_3 = {"valid": True, "entries_checked": 3, "first_mismatch": None}
UNUSABLE = {"valid": False, "entries_checked": 0, "first_mismatch": None}
CONTENT_FIELDS = ["seq", "entry_type", "actor_id", "subject_id", "payload", "recorded_at"]
SECRET = "SECRET-payload-value-6021"
HEX64 = re.compile(r"[0-9a-f]{64}")


def tampered_fixture(tmp_path: Path, old: str, new: str, name: str = "export.json") -> Path:
    text = FIXTURE.read_text(encoding="utf-8")
    assert text.count(old) == 1, old
    path = tmp_path / name
    path.write_text(text.replace(old, new), encoding="utf-8")
    return path


def build_chain(specs: list[tuple[str, str, str | None, dict[str, Any]]]) -> list[dict[str, Any]]:
    """A valid chain built with the server's M1 functions (an independent implementation)."""
    rows: list[dict[str, Any]] = []
    prev_hash = GENESIS_PREV_HASH
    for seq, (entry_type, actor_id, subject_id, payload) in enumerate(specs, start=1):
        recorded_at = f"2026-09-27T10:{seq // 60 % 60:02d}:{seq % 60:02d}.{seq % 1000:03d}Z"
        canonical = canonical_json(
            seq=seq,
            entry_type=entry_type,
            actor_id=actor_id,
            subject_id=subject_id,
            payload=payload,
            recorded_at=recorded_at,
        )
        entry_hash = compute_hash(prev_hash, canonical)
        rows.append(
            {
                "seq": seq,
                "entry_type": entry_type,
                "actor_id": actor_id,
                "subject_id": subject_id,
                "payload": payload,
                "recorded_at": recorded_at,
                "prev_hash": prev_hash,
                "hash": entry_hash,
            }
        )
        prev_hash = entry_hash
    return rows


# A. Known answers


def test_script_reproduces_the_addendum_hashes() -> None:
    rows = fixture_rows()
    # The script builds exactly the canonical strings of addendum rule 7 ...
    assert [script.canonical_content(row) for row in rows] == [
        CANONICAL_1,
        CANONICAL_2,
        CANONICAL_3,
    ]
    # ... and hashes them, chained, to exactly the three published hashes.
    assert script.entry_hash(GENESIS_PREV_HASH, CANONICAL_1) == HASH_1
    assert script.entry_hash(HASH_1, CANONICAL_2) == HASH_2
    assert script.entry_hash(HASH_2, CANONICAL_3) == HASH_3


def test_script_verifies_the_fixture(capsys: pytest.CaptureFixture[str]) -> None:
    run = run_main([str(FIXTURE)], capsys)
    # Three correct entries: valid, 3 checked, no mismatch, exit 0, nothing on stderr.
    assert (run.code, run.result, run.stderr) == (0, VALID_3, "")


def test_one_changed_character_in_entry_2_payload(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tampered_fixture(tmp_path, '"0.34"', '"0.35"')
    run = run_main([str(path)], capsys)
    # Entry 1 is untouched. Entry 2's seq and prev_hash are unchanged, but its recomputed
    # hash no longer equals the stored one: invalid at 2 after 2 entries (addendum rule 8).
    assert (run.code, run.result) == (
        1,
        {"valid": False, "entries_checked": 2, "first_mismatch": "2"},
    )
    assert "ledger verify mismatch: seq=2 check=hash" in run.stderr.splitlines()


def test_script_runs_isolated_from_another_directory(tmp_path: Path) -> None:
    script_dir, data_dir, work_dir = (tmp_path / d for d in ("script", "data", "work"))
    for directory in (script_dir, data_dir, work_dir):
        directory.mkdir()
    script_copy = Path(shutil.copy(SCRIPT, script_dir / "verify_ledger.py"))
    fixture_copy = Path(shutil.copy(FIXTURE, data_dir / "export.json"))
    tampered = tampered_fixture(data_dir, '"0.34"', '"0.35"', "tampered.json")
    # python -I -S, empty PYTHONPATH, a different working directory: only the standard
    # library is reachable, and the results are the same as in-process.
    run = run_subprocess([str(fixture_copy)], script_path=script_copy, cwd=work_dir)
    assert (run.code, run.result, run.stderr) == (0, VALID_3, "")
    run = run_subprocess([str(tampered)], script_path=script_copy, cwd=work_dir)
    assert (run.code, run.result, run.check) == (
        1,
        {"valid": False, "entries_checked": 2, "first_mismatch": "2"},
        "hash",
    )


# A2. The hash check on its own (decision 70)

OTHER_PREV_HASH = "ab" * 32  # well-formed, and not the hash of any fixture entry


def own_hash(prev_hash: str, canonical: str) -> str:
    """The addendum hash, computed here with hashlib, so these tests do not trust the script."""
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def entry_2_with_prev_hash(prev_hash: str) -> dict[str, Any]:
    """Fixture entry 2 with this stored prev_hash, and a stored hash computed from it."""
    row = fixture_rows()[1]
    row["prev_hash"] = prev_hash
    row["hash"] = own_hash(prev_hash, CANONICAL_2)
    return row


def test_hash_check_takes_only_the_row() -> None:
    # One parameter, the row: the function cannot see the previous row or its hash.
    assert list(inspect.signature(script.hash_check).parameters) == ["row"]


@pytest.mark.parametrize(
    "prev_hash",
    [OTHER_PREV_HASH, GENESIS_PREV_HASH, HASH_2, HASH_3],
    ids=["other", "genesis", "own-hash", "successor-hash"],
)
def test_a_row_consistent_with_its_own_prev_hash_passes(prev_hash: str) -> None:
    # (a) Entry 2's real predecessor hash is HASH_1, and none of these is HASH_1. The stored
    # hash was computed from the row's own stored prev_hash, so the row is consistent with
    # itself and the hash check passes (decision 18).
    assert prev_hash != HASH_1
    assert script.hash_check(entry_2_with_prev_hash(prev_hash)) is None


def test_each_fixture_row_passes_on_its_own() -> None:
    # (a) Each published entry, checked alone with no predecessor in sight, is consistent.
    assert [script.hash_check(row) for row in fixture_rows()] == [None, None, None]


def test_changing_only_the_stored_prev_hash_flips_the_result() -> None:
    # (b) Consistent with OTHER_PREV_HASH; the same row with only its stored prev_hash
    # changed (to the real predecessor's hash, HASH_1) no longer matches its stored hash.
    row = entry_2_with_prev_hash(OTHER_PREV_HASH)
    assert script.hash_check(row) is None
    row["prev_hash"] = HASH_1
    assert script.hash_check(row) == "hash"
    # The other direction: the real entry 2 is consistent with HASH_1 and fails once only
    # its stored prev_hash changes.
    real = fixture_rows()[1]
    assert script.hash_check(real) is None
    real["prev_hash"] = OTHER_PREV_HASH
    assert script.hash_check(real) == "hash"


def uncanonicalizable_cases() -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for field in CONTENT_FIELDS:
        row = fixture_rows()[1]
        del row[field]
        cases[f"missing-{field}"] = row
    float_text = FIXTURE.read_text(encoding="utf-8").replace('"0.34"', "0.34")
    cases["float-in-payload"] = script.load_export(float_text.encode("utf-8"))[1]
    nan_text = FIXTURE.read_text(encoding="utf-8").replace('"0.34"', "NaN")
    cases["nan-in-payload"] = script.load_export(nan_text.encode("utf-8"))[1]
    nul = fixture_rows()[1]
    nul["payload"]["observed_value"] = "0.3\x004"
    cases["nul-in-payload"] = nul
    wrong_type = fixture_rows()[1]
    wrong_type["subject_id"] = ""
    cases["empty-subject_id"] = wrong_type
    no_prev = fixture_rows()[1]
    del no_prev["prev_hash"]
    cases["missing-prev_hash"] = no_prev
    upper = fixture_rows()[1]
    upper["prev_hash"] = HASH_1.upper()
    cases["uppercase-prev_hash"] = upper
    return cases


@pytest.mark.parametrize("case", list(uncanonicalizable_cases()))
def test_missing_or_uncanonicalizable_content_is_a_mismatch(case: str) -> None:
    # (c) A missing content field or a value with no canonical form is a mismatch, reported
    # as uncanonicalizable (decisions 24, 56 and 57). So is a prev_hash that is missing or not
    # 64 lowercase hex characters, because no hash can be recomputed from it.
    assert script.hash_check(uncanonicalizable_cases()[case]) == "uncanonicalizable"


def test_a_missing_or_wrong_stored_hash_is_a_hash_mismatch() -> None:
    # (c) The content canonicalizes, but the stored hash is missing or different.
    no_hash = fixture_rows()[1]
    del no_hash["hash"]
    wrong = fixture_rows()[1]
    wrong["hash"] = HASH_1
    assert [script.hash_check(no_hash), script.hash_check(wrong)] == ["hash", "hash"]


# B. Import check


def test_script_imports_only_the_standard_library() -> None:
    tree = ast.parse(SCRIPT.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):  # every import, including any inside a function
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "relative import"
            assert node.module is not None
            imported.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            assert node.func.id != "__import__", "dynamic import"
    assert imported, "no imports found"
    assert imported <= set(sys.stdlib_module_names), imported - set(sys.stdlib_module_names)
    assert not imported & {"app", "tests", "tools", "sqlalchemy", "psycopg", "importlib"}


# D. File-only cases


@pytest.mark.parametrize("field", CONTENT_FIELDS)
@pytest.mark.parametrize("k", [1, 2, 3])
def test_missing_content_field(k: int, field: str) -> None:
    rows = fixture_rows()
    del rows[k - 1][field]
    result, check = script.check_entries(rows)
    if field == "seq":
        # A missing seq fails the seq check first (decision 24). The row sorts first and its
        # first_mismatch string is not part of the cross-check (decision 59).
        assert (result["valid"], check) == (False, "seq")
    else:
        # Rows before k pass. Row k passes seq and prev_hash, then cannot be canonicalized
        # because a content field is missing: invalid at k (decision 58). For k = 1 this
        # includes removing subject_id from the null-subject SEED row (decision 24).
        assert result == {"valid": False, "entries_checked": k, "first_mismatch": str(k)}
        assert check == "uncanonicalizable"


def test_explicit_null_subject_and_unknown_extra_keys_are_valid(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = fixture_rows()
    assert "subject_id" in rows[0] and rows[0]["subject_id"] is None  # explicit null
    for row in rows:
        row["unexpected"] = {"anything": [1.5, None]}  # ignored, even with a float inside
    run = run_main([str(write_rows(tmp_path / "x.json", rows))], capsys)
    # The hash covers only the six content fields, so extra keys change nothing.
    assert (run.code, run.result) == (0, VALID_3)


@pytest.mark.parametrize("number", ["1.5", "1.0", "1e3", "NaN", "Infinity", "-Infinity"])
def test_non_integer_numbers_are_uncanonicalizable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], number: str
) -> None:
    path = tampered_fixture(tmp_path, '"observed_value": "0.34"', f'"observed_value": {number}')
    run = run_main([str(path)], capsys)
    # Entry 2's payload now holds a non-integer number (decision 56): uncanonicalizable at 2.
    assert (run.code, run.result, run.check) == (
        1,
        {"valid": False, "entries_checked": 2, "first_mismatch": "2"},
        "uncanonicalizable",
    )


def test_a_float_nested_in_an_array_is_uncanonicalizable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tampered_fixture(tmp_path, '"sys-42"\n   ]', '"sys-42", 2.5\n   ]')
    run = run_main([str(path)], capsys)
    # SEED's systems array holds 2.5: entry 1 cannot be canonicalized.
    assert (run.result, run.check) == (
        {"valid": False, "entries_checked": 1, "first_mismatch": "1"},
        "uncanonicalizable",
    )


def test_an_integer_edit_is_caught_by_the_hash_check(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tampered_fixture(tmp_path, '"observed_value": "0.34"', '"observed_value": 1000')
    run = run_main([str(path)], capsys)
    # 1000 is an allowed integer, so entry 2 canonicalizes, but to a different string than
    # the one hashed: the hash check fails at 2 (the database form of 1e3, decision 22).
    assert (run.result, run.check) == (
        {"valid": False, "entries_checked": 2, "first_mismatch": "2"},
        "hash",
    )


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ('"observed_value": "0.34"', '"observed_value": "0.3\\u00004"'),
        ('"observed_value": "0.34"', '"observed_value": "0.3\\ud8004"'),
        ('"observed_value": "0.34"', '"observ\\udfffed_value": "0.34"'),
        ('"actor_id": "sys-42"', '"actor_id": "sys-\\u000042"'),
    ],
    ids=["nul-value", "lone-surrogate-value", "lone-surrogate-key", "nul-actor"],
)
def test_forbidden_characters_are_uncanonicalizable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], old: str, new: str
) -> None:
    path = tampered_fixture(tmp_path, old, new)
    run = run_main([str(path)], capsys)
    # Entry 2 now has U+0000 or a lone surrogate (written as a JSON escape) in a payload
    # value, a payload key or actor_id: uncanonicalizable at 2 (decision 57), no crash.
    assert (run.code, run.result, run.check) == (
        1,
        {"valid": False, "entries_checked": 2, "first_mismatch": "2"},
        "uncanonicalizable",
    )


def test_shuffled_rows_verify_after_sorting(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = fixture_rows()
    shuffled = [rows[2], rows[0], rows[1]]
    run = run_main([str(write_rows(tmp_path / "x.json", shuffled))], capsys)
    # Sorted by stored seq they are entries 1, 2, 3 again (decision 58).
    assert (run.code, run.result) == (0, VALID_3)


@pytest.mark.parametrize("seq_value", ["missing", True, "2", 2.0, None, "duplicate"], ids=str)
def test_degenerate_seq_is_invalid_and_never_crashes(tmp_path: Path, seq_value: object) -> None:
    rows = fixture_rows()
    if seq_value == "missing":
        del rows[1]["seq"]
    elif seq_value == "duplicate":
        rows[2]["seq"] = 2  # two rows with seq 2
    else:
        rows[1]["seq"] = seq_value
    run = run_subprocess([str(write_rows(tmp_path / "x.json", rows))])
    # Invalid, exit 1, and no traceback (run_subprocess checks stderr). The exact
    # first_mismatch string is not asserted (decision 59).
    assert run.code == 1
    assert run.result is not None and run.result["valid"] is False
    assert run.check == "seq"


UNUSABLE_INPUTS: dict[str, bytes | None] = {
    "missing-file": None,
    "empty-file": b"",
    "invalid-utf8": b"[\xff]",
    "invalid-json": b"[{",
    "object-not-array": b'{"seq": 1}',
    "non-object-element": b'[{"seq": 1}, 2]',
    "utf8-bom": b"\xef\xbb\xbf" + FIXTURE.read_bytes(),
    "huge-integer-digits": b"[" + b"9" * 5000 + b"]",
}


@pytest.mark.parametrize("case", list(UNUSABLE_INPUTS))
def test_unusable_input(tmp_path: Path, capsys: pytest.CaptureFixture[str], case: str) -> None:
    path = tmp_path / "export.json"
    content = UNUSABLE_INPUTS[case]
    if content is not None:
        path.write_bytes(content)
    run = run_main([str(path)], capsys)
    # Decision 54: invalid with nothing checked, exit 1, one reason line on stderr.
    assert (run.code, run.result) == (1, UNUSABLE)
    assert len(run.stderr.splitlines()) == 1
    assert run.stderr.startswith("unusable input: ")


def test_unusable_input_through_a_subprocess(tmp_path: Path) -> None:
    path = tmp_path / "export.json"
    path.write_bytes(b'{"not": "an array"}')
    run = run_subprocess([str(path)])
    assert (run.code, run.result) == (1, UNUSABLE)


def test_an_empty_array_is_a_valid_empty_ledger(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "export.json"
    path.write_text("[]", encoding="utf-8")
    run = run_main([str(path)], capsys)
    # An empty ledger is valid with nothing checked (addendum rule 11; decision 54).
    assert (run.code, run.result, run.stderr) == (
        0,
        {"valid": True, "entries_checked": 0, "first_mismatch": None},
        "",
    )


@pytest.mark.parametrize("args", [[], ["a.json", "b.json"]], ids=["none", "two"])
def test_usage_error_exits_2(capsys: pytest.CaptureFixture[str], args: list[str]) -> None:
    run = run_main(args, capsys)
    # Decision 53: exit 2; nothing on stdout, because nothing was verified.
    assert (run.code, run.stdout) == (2, "")
    assert run.stderr.startswith("usage: ")


def test_no_secret_or_hash_on_stdout_or_stderr(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rows = build_chain(
        [
            ("SEED", "system", None, {"systems": ["sys-42"], "threshold_count": "2"}),
            ("EVENT_FLAGGED", "sys-42", "evt-1", {"event_id": "evt-1", "note": SECRET}),
            ("EVENT_VIEWED", "viewer-1", "evt-1", {"note": SECRET}),
        ]
    )
    valid = run_main([str(write_rows(tmp_path / "valid.json", rows))], capsys)
    tampered_rows = copy.deepcopy(rows)
    tampered_rows[1]["payload"]["note"] = SECRET + "-changed"
    tampered = run_main([str(write_rows(tmp_path / "tampered.json", tampered_rows))], capsys)
    assert valid.result == {"valid": True, "entries_checked": 3, "first_mismatch": None}
    # The payload changed: entry 2 fails the hash check.
    assert tampered.result == {"valid": False, "entries_checked": 2, "first_mismatch": "2"}
    for run in (valid, tampered):
        for stream in (run.stdout, run.stderr):
            assert SECRET not in stream
            assert HEX64.search(stream) is None


def test_integer_above_2_pow_53_is_accepted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    big = 2**53 + 1  # 9007199254740993: not representable as a JavaScript number
    rows = build_chain(
        [
            ("SEED", "system", None, {"systems": ["sys-42"], "threshold_count": "2"}),
            ("NOTIFICATION_DELIVERED", "system", "evt-1", {"latency_seconds": big}),
        ]
    )
    canonical = script.canonical_content(rows[1])
    assert canonical is not None and '"latency_seconds":9007199254740993' in canonical
    # The script's hash of that canonical string is the hash the server computed.
    assert script.entry_hash(rows[1]["prev_hash"], canonical) == rows[1]["hash"]
    run = run_main([str(write_rows(tmp_path / "x.json", rows))], capsys)
    assert (run.code, run.result) == (
        0,
        {"valid": True, "entries_checked": 2, "first_mismatch": None},
    )


# F. Scale (sanity only)


def test_twenty_thousand_entries(tmp_path: Path) -> None:
    count = 20_000
    rows = build_chain(
        [
            ("EVENT_VIEWED", "viewer-1", f"evt-{n % 1000}", {"n": n, "note": "é"})
            for n in range(1, count + 1)
        ]
    )
    path = write_rows(tmp_path / "big.json", rows)
    started = time.perf_counter()
    run = run_subprocess([str(path)])
    elapsed = time.perf_counter() - started
    print(f"\n20,000-entry export ({path.stat().st_size} bytes) verified in {elapsed:.2f} s")
    assert (run.code, run.result) == (
        0,
        {"valid": True, "entries_checked": count, "first_mismatch": None},
    )
    assert elapsed < 60  # a generous sanity bound, not a performance promise
