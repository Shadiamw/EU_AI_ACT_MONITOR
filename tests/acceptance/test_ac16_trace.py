"""AC-16 over HTTP: the scripted trace of tools/demo_phase5.py (Phase 5 spec 10.3), one test per
path. The steps are the demo's own; the sink is the runner's webhook channel. When
REVIEWER_2_KEY is set (and the stack seeded rev-2 from it, D17), S6's revision is rev-2's.
"""

from __future__ import annotations

import os

import httpx
import pytest

from tests.acceptance.api import Sink
from tools import demo_phase5

pytestmark = pytest.mark.integration


def _run(http: httpx.Client, keys: dict[str, str], sink: Sink, path: str) -> None:
    lines: list[str] = []
    all_keys = dict(keys)
    if os.environ.get("REVIEWER_2_KEY"):
        all_keys["reviewer2"] = os.environ["REVIEWER_2_KEY"]
    trace = demo_phase5.Trace(
        client=http,
        keys=all_keys,
        receiver=demo_phase5.http_receiver(http, sink.url, sink.read_url),
        out=lines.append,
    )
    passed = demo_phase5.run_path(trace, demo_phase5.PATHS[path])
    assert passed, "\n".join(lines)
    assert len(lines) == len(demo_phase5.PATHS[path])


def test_ac16_breach_to_submission_trace(
    http: httpx.Client, keys: dict[str, str], sink: Sink
) -> None:
    _run(http, keys, sink, "S")


def test_ac16_not_serious_trace_has_no_deadline(
    http: httpx.Client, keys: dict[str, str], sink: Sink
) -> None:
    _run(http, keys, sink, "N")
