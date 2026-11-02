"""The root conftest refuses to run the HTTP runner against a stack (integration decision 219).

tests/conftest.py's session fixture migrated_db downgrades and upgrades the database named by
DATABASE_ADMIN_URL. The HTTP runner in tests/acceptance must therefore run with
`--confcutdir=tests/acceptance` (Part 4 decision 145). If it is started against a stack
(BASE_URL set) without that option, the root conftest stops the session at collection, before
any fixture runs. Both subprocesses here point the database URLs at a closed port, so that a
missing guard fails without touching any real database.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
NOWHERE_DB = "postgresql+psycopg://nobody:nobody@127.0.0.1:9/nowhere"
TARGET = "tests/acceptance/test_platform_contract.py"


def _run(*args: str, base_url: str | None) -> subprocess.CompletedProcess[str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if k not in ("BASE_URL", "WEBHOOK_SINK_URL", "WEBHOOK_SINK_READ_URL", "PYTEST_ADDOPTS")
    }
    env.update(
        {
            "DATABASE_URL": NOWHERE_DB,
            "DATABASE_ADMIN_URL": NOWHERE_DB,
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    if base_url is not None:
        env["BASE_URL"] = base_url
    return subprocess.run(
        [sys.executable, "-m", "pytest", TARGET, "-p", "no:cacheprovider", "-q", *args],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_runner_against_a_stack_without_confcutdir_is_refused() -> None:
    run = _run(base_url="http://127.0.0.1:9")
    output = run.stdout + run.stderr
    assert run.returncode == 4, output
    assert "add --confcutdir=tests/acceptance" in output, output
    assert "passed" not in output and "error at setup" not in output, output


def test_collecting_the_acceptance_folder_without_base_url_is_still_allowed() -> None:
    """A plain in-process run that happens to collect tests/acceptance (no BASE_URL, so its tests
    skip) is not affected."""
    run = _run("--collect-only", base_url=None)
    assert run.returncode == 0, run.stdout + run.stderr
