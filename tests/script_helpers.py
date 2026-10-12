"""Helpers for running tools/verify_ledger.py in tests (M5)."""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from tools import verify_ledger as script

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "verify_ledger.py"
FIXTURE = ROOT / "tests" / "fixtures" / "fixture_export.json"
RESULT_KEYS = {"valid", "entries_checked", "first_mismatch"}


@dataclass
class Run:
    code: int
    result: dict[str, Any] | None  # the parsed stdout object, None if stdout is empty
    stdout: str
    stderr: str

    @property
    def check(self) -> str | None:
        """The check name from the script's mismatch line on stderr, if any."""
        names = [
            line.rsplit("check=", 1)[1]
            for line in self.stderr.splitlines()
            if line.startswith("ledger verify mismatch: ")
        ]
        assert len(names) <= 1, self.stderr
        return names[0] if names else None


def parse_stdout(stdout: str) -> dict[str, Any] | None:
    """stdout is empty or exactly one JSON object with exactly the three result keys."""
    if stdout == "":
        return None
    assert stdout.endswith("\n") and stdout.count("\n") == 1, repr(stdout)
    result = json.loads(stdout)
    assert isinstance(result, dict)
    assert set(result) == RESULT_KEYS, result
    assert type(result["valid"]) is bool
    assert type(result["entries_checked"]) is int
    assert result["first_mismatch"] is None or isinstance(result["first_mismatch"], str)
    return result


def run_main(args: list[str], capsys: pytest.CaptureFixture[str]) -> Run:
    """Call the script's main() in-process."""
    capsys.readouterr()
    code = script.main(args)
    captured = capsys.readouterr()
    return Run(code, parse_stdout(captured.out), captured.out, captured.err)


def run_subprocess(args: list[str], *, script_path: Path = SCRIPT, cwd: Path | None = None) -> Run:
    """Run the script as a real process: isolated mode, no site packages, empty PYTHONPATH."""
    completed = subprocess.run(
        [sys.executable, "-I", "-S", str(script_path), *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=cwd,
        env={"PYTHONPATH": "", "PATH": "/usr/bin:/bin"},
        timeout=120,
    )
    assert "Traceback" not in completed.stderr, completed.stderr
    return Run(
        completed.returncode,
        parse_stdout(completed.stdout),
        completed.stdout,
        completed.stderr,
    )


def write_rows(path: Path, rows: list[dict[str, Any]]) -> Path:
    path.write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
    return path


def fixture_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return rows
