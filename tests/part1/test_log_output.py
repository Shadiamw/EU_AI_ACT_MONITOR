"""The spec 1.8 log lines reach the process's output when the app runs as a server, not only
pytest's capture (final spec check F2, F3, F4; decision 132).

Each case starts a fresh Python process that imports the app the way uvicorn does, sends real
requests through it and reads what the process wrote. Nothing in that process configures
logging, so a line appears only if the app itself attaches a handler.
"""

import json
import os
import subprocess
import sys
import textwrap
from typing import Any

import pytest

from app import config

SCRIPT = textwrap.dedent(
    """
    import json, os, sys
    from starlette.testclient import TestClient
    from app.main import app

    keys = {"viewer": os.environ["VIEWER_KEY"], "sys-42": os.environ["INGEST_KEY_SYS_42"]}
    with TestClient(app) as client:
        statuses = [
            client.get("/v1/events").status_code,
            client.get(
                "/v1/events", headers={"Authorization": "Bearer " + keys["viewer"]}
            ).status_code,
            client.post(
                "/v1/metrics",
                headers={"Authorization": "Bearer " + keys["sys-42"]},
                content=json.dumps(
                    {
                        "system_id": "sys-42",
                        "metric_name": "accuracy_drift",
                        "metric_type": "drift",
                        "value": 0.34,
                        "timestamp": "2026-09-27T10:05:00Z",
                    }
                ),
            ).status_code,
        ]
    print("STATUSES " + json.dumps(statuses), file=sys.__stdout__)
    """
)


def run_app(log_level: str) -> tuple[list[int], str]:
    env = {**os.environ, "LOG_LEVEL": log_level}
    done = subprocess.run(
        [sys.executable, "-c", SCRIPT],
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    marker = [line for line in done.stdout.splitlines() if line.startswith("STATUSES ")]
    output = done.stdout + done.stderr
    return json.loads(marker[0].removeprefix("STATUSES ")), output


def json_lines(output: str) -> list[dict[str, Any]]:
    lines = []
    for line in output.splitlines():
        if line.startswith("{"):
            lines.append(json.loads(line))
    return lines


@pytest.mark.parametrize("log_level", ["INFO", "DEBUG"])
def test_request_lines_are_written_for_2xx_and_4xx(seeded: None, log_level: str) -> None:
    statuses, output = run_app(log_level)
    assert statuses == [401, 200, 201]
    requests = [line for line in json_lines(output) if "status" in line]
    # Spec 1.8: one JSON line per request with ts, level, request_id, actor_id, method, path,
    # status, duration_ms.
    assert [(r["method"], r["path"], r["status"], r["actor_id"]) for r in requests] == [
        ("GET", "/v1/events", 401, None),
        ("GET", "/v1/events", 200, "viewer-1"),
        ("POST", "/v1/metrics", 201, "sys-42"),
    ]
    for line in requests:
        assert {"ts", "level", "request_id", "duration_ms"} <= line.keys()
        assert line["level"] == "INFO"


def test_counters_are_logged(seeded: None) -> None:
    _, output = run_app("INFO")
    counters = [line["counters"] for line in json_lines(output) if "counters" in line]
    # Spec 1.8: samples accepted, duplicates, breaches, series joins, 4xx by code. After a 401,
    # a 200 and one breaching sample, the latest totals are these.
    assert counters[-1] == {
        "4xx:MISSING_OR_INVALID_KEY": 1,
        "samples_accepted": 1,
        "breaches": 1,
    }


def test_threshold_evaluation_is_written_at_debug_only(seeded: None) -> None:
    _, debug_output = run_app("DEBUG")
    evaluations = [line for line in debug_output.splitlines() if "threshold evaluation" in line]
    # Spec 1.8: every evaluation at debug level, with the version id (thr-1 for accuracy_drift).
    assert len(evaluations) == 1
    assert "DEBUG" in evaluations[0]
    assert "threshold_version_id=thr-1" in evaluations[0]
    assert "breached=True" in evaluations[0]
    _, info_output = run_app("INFO")
    assert "threshold evaluation" not in info_output


def test_no_key_is_written_to_the_output(seeded: None) -> None:
    _, output = run_app("DEBUG")
    for key in (config.ingest_key_sys_42(), config.reviewer_key(), config.viewer_key()):
        assert key not in output
