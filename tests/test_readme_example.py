"""M6: README.md's hash definition and worked example must match the code (decision 74).

The README is parsed, not trusted. Every expected value here comes from addendum rule 7 (the
three published hashes), from tests/fixtures/fixture_export.json, or from hashlib. If the
README's structure changes so a value can no longer be found, these tests fail loudly instead
of passing on nothing.
"""

import hashlib
import json
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from tests.script_helpers import FIXTURE, ROOT, fixture_rows, run_subprocess
from tools import verify_ledger as script

README = ROOT / "README.md"
GENESIS = "0" * 64

# Addendum rule 7, pasted as literals: the hashes the README must state.
ADDENDUM_HASHES = [
    "3e17607bd7884dfbaff84c7a4a5a4762d56f329c1f63a91420831a1078db080c",
    "e6ba0c725b25a5aa1c3f40d9c26e1823c01798d77f7ad5ed63be90894d0ad92c",
    "d557f21d771adea81d93c2aab306f00d61898207c26d1f8bc767a828efa6a5b2",
]

ENTRY = re.compile(
    r"^entry (\d+)\n"
    r"prev_hash: ([0-9a-f]{64})\n"
    r"canonical: (\{.*\})\n"
    r"hash: +([0-9a-f]{64})$",
    re.MULTILINE,
)


def section(title: str) -> str:
    """The README text under the level-2 heading `title`, up to the next level-2 heading."""
    text = README.read_text(encoding="utf-8")
    match = re.search(rf"^## {re.escape(title)}\n(.*?)(?=^## |\Z)", text, re.MULTILINE | re.DOTALL)
    assert match is not None, f"README has no '## {title}' section"
    return match.group(1)


def fenced_blocks(text: str, language: str) -> list[str]:
    return re.findall(rf"^```{language}\n(.*?)^```$", text, re.MULTILINE | re.DOTALL)


def worked_example() -> list[tuple[int, str, str, str]]:
    blocks = fenced_blocks(section("3. Worked example"), "text")
    assert len(blocks) == 1, "the worked example must be one ```text block"
    entries = [(int(n), prev, canonical, h) for n, prev, canonical, h in ENTRY.findall(blocks[0])]
    assert [entry[0] for entry in entries] == [1, 2, 3], "expected entries 1, 2 and 3"
    return entries


def sha256_hex(prev_hash: str, canonical: str) -> str:
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


def canonical_of(row: dict[str, Any]) -> str:
    """The spec's canonical form, built here with json.dumps from the fixture row."""
    content = {
        k: row[k] for k in ("seq", "entry_type", "actor_id", "subject_id", "payload", "recorded_at")
    }
    return json.dumps(content, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def test_each_stated_hash_is_sha256_of_prev_hash_and_canonical() -> None:
    for _, prev_hash, canonical, stated in worked_example():
        assert sha256_hex(prev_hash, canonical) == stated


def test_the_stated_chain_links_and_starts_at_genesis() -> None:
    entries = worked_example()
    assert entries[0][1] == GENESIS
    for previous, current in zip(entries, entries[1:], strict=False):
        assert current[1] == previous[3]


def test_stated_hashes_are_the_addendum_hashes() -> None:
    assert [entry[3] for entry in worked_example()] == ADDENDUM_HASHES


def test_stated_entries_match_the_fixture() -> None:
    rows = fixture_rows()
    assert len(rows) == 3
    for (seq, prev_hash, canonical, stated), row in zip(worked_example(), rows, strict=True):
        assert row["seq"] == seq
        assert row["prev_hash"] == prev_hash
        assert row["hash"] == stated
        # The README's canonical string is exactly what the spec's json.dumps gives for the
        # fixture row, and what the standalone verifier builds.
        assert canonical_of(row) == canonical
        assert script.canonical_content(row) == canonical


def test_the_verifier_accepts_the_fixture_the_readme_describes() -> None:
    run = run_subprocess([str(FIXTURE)])
    assert (run.code, run.result, run.stderr) == (
        0,
        {"valid": True, "entries_checked": 3, "first_mismatch": None},
        "",
    )
    # The last stated hash is the chain head the verifier accepted.
    assert worked_example()[-1][3] == fixture_rows()[-1]["hash"]


def test_the_stated_genesis_value_is_64_zeros_and_the_scripts() -> None:
    blocks = fenced_blocks(section("2. Hash definition"), "text")
    assert len(blocks) == 1, "the hash definition must be one ```text block"
    stated = re.findall(r"^genesis .*: ([0-9a-f]+)$", blocks[0], re.MULTILINE)
    assert stated == [GENESIS]
    assert script.GENESIS_PREV_HASH == GENESIS


def test_the_python_definition_in_the_readme_reproduces_the_fixture() -> None:
    blocks = fenced_blocks(section("2. Hash definition"), "python")
    assert len(blocks) == 1, "the hash definition must have one ```python block"
    namespace: dict[str, Any] = {}
    exec(blocks[0], namespace)
    entry_hash: Callable[[str, dict[str, Any]], str] = namespace["entry_hash"]
    prev_hash = GENESIS
    for row, expected in zip(fixture_rows(), ADDENDUM_HASHES, strict=True):
        prev_hash = entry_hash(prev_hash, row)
        assert prev_hash == expected


def test_the_stated_usage_line_is_the_scripts() -> None:
    blocks = fenced_blocks(section("4. Standalone verifier"), "text")
    assert blocks, "the verifier section must start with the usage line in a ```text block"
    assert blocks[0].strip() == script.USAGE


def stated_exit_codes() -> dict[str, int]:
    """The verifier table: first cell (the case) to last cell (the exit code)."""
    rows = re.findall(r"^\| (.+?) \|.*\| (\d) \|$", section("4. Standalone verifier"), re.MULTILINE)
    return {case: int(code) for case, code in rows}


@pytest.mark.parametrize(
    ("case", "make_args"),
    [
        ("Valid", lambda tmp: [str(FIXTURE)]),
        ("Empty array `[]`", lambda tmp: [str(write(tmp, "[]"))]),
        (
            "Mismatch",
            lambda tmp: [str(write(tmp, FIXTURE.read_text().replace('"0.34"', '"0.35"')))],
        ),
        ("Unusable file", lambda tmp: [str(tmp / "missing.json")]),
        ("Not exactly one argument", lambda tmp: []),
    ],
)
def test_the_stated_exit_codes_are_the_scripts(
    tmp_path: Path, case: str, make_args: Callable[[Path], list[str]]
) -> None:
    stated = {name.split(" (")[0]: code for name, code in stated_exit_codes().items()}
    assert case in stated, f"README verifier table has no '{case}' row"
    assert run_subprocess(make_args(tmp_path)).code == stated[case]


def write(directory: Path, text: str) -> Path:
    path = directory / "export.json"
    path.write_text(text, encoding="utf-8")
    return path
