"""Phase 5 Part 3, M1: tools/verify_ledger.py --anchor (spec 6.2, Delta 11; decisions 297 to 299).

The script is run as a real process in isolated mode (-I -S), as the anchor workflow and the
other team run it. Exports are built in memory from tests/fixtures/fixture_export.json, with
hashes recomputed by the server's own functions (an independent implementation of the hash).
Expected values are reasoned out from spec 6.2 in comments; they are not copied from output.
"""

import copy
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.ledger.canonical import GENESIS_PREV_HASH, canonical_json, compute_hash
from tests.script_helpers import FIXTURE, SCRIPT, fixture_rows

# The fixture's published hashes (Phase 4 addendum rule 7).
HASH_1 = "3e17607bd7884dfbaff84c7a4a5a4762d56f329c1f63a91420831a1078db080c"
HASH_3 = "d557f21d771adea81d93c2aab306f00d61898207c26d1f8bc767a828efa6a5b2"

# The script's output on the fixture before Part 3 (main a9bf283), pinned byte for byte.
FIXTURE_STDOUT = '{"valid": true, "entries_checked": 3, "first_mismatch": null}\n'
TAMPERED_STDOUT = '{"valid": false, "entries_checked": 2, "first_mismatch": "2"}\n'
TAMPERED_STDERR = "ledger verify mismatch: seq=2 check=hash\n"
USAGE_STDERR = "usage: verify_ledger.py EXPORT_FILE\n"


@dataclass
class Run:
    code: int
    stdout: str
    stderr: str


def run_script(args: list[str]) -> Run:
    """Run the script as a real process: isolated mode, no site packages, empty PYTHONPATH."""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={"PYTHONPATH": "", "PATH": "/usr/bin:/bin"},
        timeout=120,
    )
    assert "Traceback" not in completed.stderr, completed.stderr
    return Run(completed.returncode, completed.stdout, completed.stderr)


def rehash(rows: list[dict[str, Any]], start: int) -> list[dict[str, Any]]:
    """Recompute prev_hash and hash from row index `start` on, so the chain is valid again."""
    rows = copy.deepcopy(rows)
    prev_hash = GENESIS_PREV_HASH if start == 0 else rows[start - 1]["hash"]
    for row in rows[start:]:
        canonical = canonical_json(
            seq=row["seq"],
            entry_type=row["entry_type"],
            actor_id=row["actor_id"],
            subject_id=row["subject_id"],
            payload=row["payload"],
            recorded_at=row["recorded_at"],
        )
        row["prev_hash"] = prev_hash
        row["hash"] = compute_hash(prev_hash, canonical)
        prev_hash = row["hash"]
    return rows


def extended(length: int) -> list[dict[str, Any]]:
    """The fixture's three entries followed by EVENT_VIEWED entries up to `length`, valid."""
    rows = fixture_rows()
    for seq in range(4, length + 1):
        rows.append(
            {
                "seq": seq,
                "entry_type": "EVENT_VIEWED",
                "actor_id": "viewer-1",
                "subject_id": "evt-7f3a",
                "payload": {},
                "recorded_at": f"2026-09-27T10:{seq:02d}:00.000Z",
                "prev_hash": "",
                "hash": "",
            }
        )
    return rehash(rows, 3)


def write_export(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def head_line(seq: int, entry_hash: str) -> dict[str, Any]:
    """One line of heads.jsonl as the workflow writes it (spec 6.2)."""
    return {
        "taken_at": "2026-11-12T03:17:00Z",
        "base_url": "https://monitor.example",
        "app_version": "aa89f5e",
        "seq": seq,
        "hash": entry_hash,
        "recorded_at": "2026-11-12T02:00:00.000Z",
        "verify": {"valid": True, "entries_checked": seq, "first_mismatch": None},
    }


def write_heads(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def test_anchor_detects_truncation_below_the_anchor(tmp_path: Path) -> None:
    chain = extended(5)
    heads = write_heads(tmp_path / "heads.jsonl", [head_line(5, chain[4]["hash"])])
    # The export lost entries 4 and 5. What is left is a valid chain of three ...
    export = write_export(tmp_path / "export.json", chain[:3])
    run = run_script([str(export), "--anchor", str(heads)])
    # ... so the verify line is the ordinary valid result, but the anchored entry 5 is gone:
    # the chain is shorter than the last committed head (spec 6.2 case 2). Exit 1.
    assert run.code == 1
    assert run.stdout == FIXTURE_STDOUT + "anchor: MISMATCH seq=5\n"
    assert run.stderr == ""


def test_anchor_detects_a_forged_anchored_entry(tmp_path: Path) -> None:
    chain = extended(5)
    heads = write_heads(tmp_path / "heads.jsonl", [head_line(3, chain[2]["hash"])])
    assert chain[2]["hash"] == HASH_3
    # Entry 2 is rewritten and every later hash recomputed: the forged chain is internally
    # consistent, so the verifier alone finds nothing ...
    forged = copy.deepcopy(chain)
    forged[1]["payload"]["observed_value"] = "0.29"
    forged = rehash(forged, 1)
    export = write_export(tmp_path / "export.json", forged)
    run = run_script([str(export), "--anchor", str(heads)])
    # ... but entry 3 no longer has the anchored hash (spec 6.2 case 3). Exit 1.
    assert run.code == 1
    assert run.stdout == (
        '{"valid": true, "entries_checked": 5, "first_mismatch": null}\nanchor: MISMATCH seq=3\n'
    )
    assert run.stderr == ""


def test_anchor_accepts_a_longer_valid_chain(tmp_path: Path) -> None:
    chain = extended(6)
    export = write_export(tmp_path / "export.json", chain)
    expected_verify = '{"valid": true, "entries_checked": 6, "first_mismatch": null}\n'

    # The last head is seq 3 with the fixture's published hash; the export has grown to 6.
    heads = write_heads(tmp_path / "heads.jsonl", [head_line(3, HASH_3)])
    run = run_script([str(export), "--anchor", str(heads)])
    assert (run.code, run.stdout, run.stderr) == (0, expected_verify + "anchor: OK seq=3\n", "")

    # The head is the last line with seq and hash and no new_series (decision 298): an older
    # head and a new_series line before it are skipped. Blank lines are ignored.
    series = {
        "new_series": True,
        "reason": "test reset before the demo",
        "reset_count": 4,
        "previous_seq": 9,
        "previous_hash": "f" * 64,
        "actor": "someone",
    }
    heads.write_text(
        json.dumps(head_line(9, "f" * 64))
        + "\n\n"
        + json.dumps(series)
        + "\n"
        + json.dumps(head_line(1, HASH_1))
        + "\n",
        encoding="utf-8",
    )
    run = run_script([str(export), "--anchor", str(heads)])
    assert (run.code, run.stdout, run.stderr) == (0, expected_verify + "anchor: OK seq=1\n", "")

    # A heads file with no head line cannot anchor anything (decision 299): exit 2, a reason
    # on stderr, nothing on stdout.
    write_heads(heads, [series])
    run = run_script([str(export), "--anchor", str(heads)])
    assert (run.code, run.stdout) == (2, "")
    assert run.stderr.startswith("anchor: ")


def test_verifier_output_without_anchor_is_unchanged(tmp_path: Path) -> None:
    # Without --anchor, stdout, stderr and the exit code are byte-identical to the script on
    # main a9bf283 (spec 6.2: the other team's copy keeps working).
    run = run_script([str(FIXTURE)])
    assert (run.code, run.stdout, run.stderr) == (0, FIXTURE_STDOUT, "")

    text = FIXTURE.read_text(encoding="utf-8")
    assert text.count('"0.34"') == 1
    tampered = tmp_path / "tampered.json"
    tampered.write_text(text.replace('"0.34"', '"0.35"'), encoding="utf-8")
    run = run_script([str(tampered)])
    assert (run.code, run.stdout, run.stderr) == (1, TAMPERED_STDOUT, TAMPERED_STDERR)

    for args in ([], ["a.json", "b.json"], [str(FIXTURE), "--anchor"]):
        run = run_script(args)
        assert (run.code, run.stdout, run.stderr) == (2, "", USAGE_STDERR)

    # With --anchor, the same verify output comes first, unchanged, and exactly one line is
    # added after it.
    heads = write_heads(tmp_path / "heads.jsonl", [head_line(3, HASH_3)])
    run = run_script([str(FIXTURE), "--anchor", str(heads)])
    assert (run.code, run.stdout, run.stderr) == (0, FIXTURE_STDOUT + "anchor: OK seq=3\n", "")
    run = run_script([str(tampered), "--anchor", str(heads)])
    assert (run.code, run.stdout, run.stderr) == (
        1,
        TAMPERED_STDOUT + "anchor: MISMATCH seq=3\n",
        TAMPERED_STDERR,
    )
