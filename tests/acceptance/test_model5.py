"""Model 5 shared fixtures Ex-1, Ex-2 and Ex-4 over HTTP (Phase 4 spec 4.7; decision 144).

Each example in tests/fixtures/phase4/model5_examples.json is run by tools/run_model5.py against
BASE_URL, with the sink for the webhook expectations. Ex-1's reminder steps need Part 3.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from tests.acceptance.api import Sink
from tools import run_model5

pytestmark = pytest.mark.integration

EXAMPLES: list[dict[str, Any]] = json.loads(run_model5.FIXTURE.read_text(encoding="utf-8"))[
    "examples"
]


@pytest.mark.parametrize("example", EXAMPLES, ids=[example["id"] for example in EXAMPLES])
def test_model5_example(
    http: httpx.Client, keys: dict[str, str], sink: Sink, example: dict[str, Any]
) -> None:
    channel = run_model5.SinkChannel(url=sink.url, read_url=sink.read_url)
    try:
        run_model5.run_example(http, example, keys, channel)
    except run_model5.StepFailure as failure:
        pytest.fail(str(failure), pytrace=False)
