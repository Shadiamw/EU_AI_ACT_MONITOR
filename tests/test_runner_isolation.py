"""The HTTP acceptance runner never loads the in-process suite's database fixtures (spec 4.8).

tests/acceptance runs against any BASE_URL, so it must not load tests/conftest.py, whose
session fixture downgrades and upgrades the database named by DATABASE_ADMIN_URL. It is run
with `--confcutdir=tests/acceptance` (README 13, Part 4 decision 145). This file sits outside
tests/acceptance so that the in-process suite, which ignores that folder, still runs it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RUNNER = [
    sys.executable,
    "-m",
    "pytest",
    "tests/acceptance",
    "--confcutdir=tests/acceptance",
    "-p",
    "no:cacheprovider",
    "-q",
    "-rs",
]
# Everything that could point the runner at a database or a server.
_STRIPPED = (
    "DATABASE_URL",
    "DATABASE_ADMIN_URL",
    "BASE_URL",
    "WEBHOOK_SINK_URL",
    "WEBHOOK_SINK_READ_URL",
    "PYTEST_ADDOPTS",
)


def _run(*extra: str) -> subprocess.CompletedProcess[str]:
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return subprocess.run(
        [*RUNNER, *extra],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_without_base_url_the_runner_skips_every_test_and_needs_no_database() -> None:
    run = _run()
    output = run.stdout + run.stderr
    assert run.returncode == 0, output
    summary = re.search(r"^(\d+) skipped in ", output, re.MULTILINE)
    assert summary and int(summary.group(1)) > 0, output
    assert "passed" not in output and "failed" not in output and "error" not in output, output
    assert "BASE_URL is not set" in output, output


def test_the_runner_does_not_load_the_root_conftest() -> None:
    """--run-integration is defined only by tests/conftest.py. The runner rejecting it shows that
    file, and with it the migrated_db fixture, is not loaded."""
    run = _run("--run-integration")
    assert run.returncode == 4, run.stdout + run.stderr
    assert "unrecognized arguments: --run-integration" in run.stderr, run.stderr
