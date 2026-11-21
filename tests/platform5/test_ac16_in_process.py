"""AC-16 in process (Phase 5 spec 10.1, 10.3, 10.4): the scripted trace of tools/demo_phase5.py,
both paths, through the ASGI app, for the gate.

The steps are the demo's own. Time is the database offset moved by the clock-advance endpoint,
never FakeClock (Phase 4 Addendum rule 10), so reset and every advance behave as over HTTP; each
advance also runs one sweep, fire_due and delivery pass, which stands in for the worker. The
webhook receiver is the real HTTP server of tests/conftest.py. rev-2 is seeded (D17), so S6's
revision is made by rev-2.
"""

from collections.abc import Iterator

import pytest

from app import config, platform
from app.main import create_app
from tests.api.conftest import ApiClient
from tests.platform5.conftest import REVIEWER_2_KEY
from tests.worker.conftest import Receiver
from tools import demo_phase5

pytestmark = pytest.mark.integration


@pytest.fixture
def trace(receiver: Receiver, monkeypatch: pytest.MonkeyPatch) -> Iterator[demo_phase5.Trace]:
    monkeypatch.setenv("REVIEWER_2_KEY", REVIEWER_2_KEY)
    lines: list[str] = []
    with ApiClient(create_app()) as client:
        keys = {
            "system": config.ingest_key_sys_42(),
            "reviewer": config.reviewer_key(),
            "viewer": config.viewer_key(),
            "reviewer2": REVIEWER_2_KEY,
        }

        def deliver() -> None:
            built.advance(0)

        built = demo_phase5.Trace(
            client=client,
            keys=keys,
            receiver=demo_phase5.Receiver(
                url=receiver.url,
                bodies=lambda: [call.body for call in receiver.calls],
                clear=receiver.calls.clear,
                pump=deliver,
            ),
            out=lines.append,
        )
        built.state["lines"] = lines
        yield built
    platform.set_offset_seconds(0)


def test_ac16_in_process_trace_both_paths(trace: demo_phase5.Trace) -> None:
    for name, steps in demo_phase5.PATHS.items():
        passed = demo_phase5.run_path(trace, steps)
        lines = trace.state["lines"]
        assert passed, f"path {name}:\n" + "\n".join(lines)
    print("\n".join(trace.state["lines"]))  # the AC-16 trace, shown with -s
    labels = [line.split(" ", 1)[0] for line in trace.state["lines"] if " ok: " in line]
    assert labels == [label for steps in demo_phase5.PATHS.values() for label, _, _ in steps]
