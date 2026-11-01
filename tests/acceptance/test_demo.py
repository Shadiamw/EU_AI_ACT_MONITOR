"""The Phase 4 demo (spec section 12) as the runner sees it: tools/demo_phase4.py is run as a
subprocess against BASE_URL and must exit 0 with all ten "step N ok" lines.

The demo itself fails at the first mismatch (status, body field, state, ledger count or sink
content). Its step 9 (reminders) and step 10 (REMINDER_TRIGGERED) need Part 3.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.acceptance.api import Sink

pytestmark = pytest.mark.integration

DEMO = Path(__file__).resolve().parents[2] / "tools" / "demo_phase4.py"


def test_the_demo_walks_the_ten_steps(base_url: str, keys: dict[str, str], sink: Sink) -> None:
    env = {
        **os.environ,
        "BASE_URL": base_url,
        "INGEST_KEY": keys["system"],
        "REVIEWER_KEY": keys["reviewer"],
        "VIEWER_KEY": keys["viewer"],
        "WEBHOOK_SINK_URL": sink.url,
        "WEBHOOK_SINK_READ_URL": sink.read_url,
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    run = subprocess.run(
        [sys.executable, str(DEMO)],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    output = run.stdout + run.stderr
    assert run.returncode == 0, output
    for step in range(1, 11):
        assert f"step {step} ok" in run.stdout, output
