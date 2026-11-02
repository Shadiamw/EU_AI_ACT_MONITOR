"""The compose stack and the image job leave no sink test silently skipped (decisions 143, 145).

The API process runs the in-service HTTP runner and hands it its own environment, so the
compose `api` service needs WEBHOOK_SINK_URL. In CI the image job's runner step fails when a
sink is configured and any acceptance test still skipped. The checks read the files as text
(PyYAML is not a dependency) and run the step's own skip check against sample JUnit files.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
CONDITION_95 = (
    "github.ref_name == 'main' || github.ref_name == 'phase4-integration' "
    "|| github.base_ref == 'main'"
)


def _service(name: str) -> str:
    """The text of one top-level compose service, up to the next service or section."""
    text = COMPOSE.read_text(encoding="utf-8")
    match = re.search(rf"^  {name}:\n(.*?)(?=^  \S|^\S)", text, re.MULTILINE | re.DOTALL)
    assert match, f"no service {name!r} in docker-compose.yml"
    return match.group(1)


def _step(name: str) -> str:
    """The text of one CI step, from its `- name:` line to the next step or job."""
    text = CI.read_text(encoding="utf-8")
    match = re.search(
        rf"^      - name: {re.escape(name)}\n(.*?)(?=^      - |^  \S|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    assert match, f"no step {name!r} in ci.yml"
    return match.group(1)


def test_the_api_service_has_a_sink_channel_of_its_own() -> None:
    api = _service("api")
    assert re.search(r"^      <<: \*app_env$", api, re.MULTILINE), api
    sink = r"^      WEBHOOK_SINK_URL: http://sink:8765/hook/suite$"
    assert re.search(sink, api, re.MULTILINE), api
    # Only the API runs the in-service runner; the worker keeps the shared environment.
    assert "environment: *app_env" in _service("worker")


def test_the_95_conditions_are_unchanged() -> None:
    text = CI.read_text(encoding="utf-8")
    guarded = f"if: ${{{{ !cancelled() && ({CONDITION_95}) }}}}"
    assert text.count(guarded) == 3, text
    assert text.count(f"if: {CONDITION_95}\n") == 2, text
    for name in ("HTTP acceptance runner", "Model 5 examples", "Phase 4 demo"):
        assert guarded in _step(name), name


def _skip_check() -> str:
    run = _step("HTTP acceptance runner")
    match = re.search(r"python - <<'PY' \|\| status=1\n(.*?)\n\s*PY\n", run, re.DOTALL)
    assert match, run
    return textwrap.dedent(match.group(1))


def _junit(tmp_path: Path, skipped: int, nested: bool) -> None:
    suite = f'<testsuite name="pytest" tests="5" failures="0" errors="0" skipped="{skipped}"/>'
    body = f"<testsuites>{suite}</testsuites>" if nested else suite
    (tmp_path / "acceptance.xml").write_text(body, encoding="utf-8")


@pytest.mark.parametrize("nested", [True, False], ids=["testsuites", "testsuite"])
@pytest.mark.parametrize(
    ("sink", "skipped", "code"),
    [("http://sink:8765/hook/ci", 0, 0), ("http://sink:8765/hook/ci", 5, 1), (None, 5, 0)],
    ids=["sink-no-skips", "sink-with-skips", "no-sink"],
)
def test_the_runner_step_fails_on_skips_only_when_a_sink_is_set(
    tmp_path: Path, sink: str | None, skipped: int, code: int, nested: bool
) -> None:
    _junit(tmp_path, skipped, nested)
    env = {k: v for k, v in os.environ.items() if k != "WEBHOOK_SINK_URL"}
    if sink:
        env["WEBHOOK_SINK_URL"] = sink
    result = subprocess.run(
        [sys.executable, "-"],
        input=_skip_check(),
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == code, result.stdout + result.stderr


def test_the_runner_step_keeps_the_pytest_status() -> None:
    run = _step("HTTP acceptance runner")
    assert "--junitxml=acceptance.xml || status=$?" in run, run
    assert run.rstrip().endswith("exit $status"), run
