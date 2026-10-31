#!/usr/bin/env python3
"""HTTP runner for tests/fixtures/phase4/model5_examples.json (Phase 4 spec 4.7)."""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "phase4" / "model5_examples.json"

_OFFSET = re.compile(r"^\+(\d+)(s|m|h|d)$")
_RELATIVE = re.compile(r"^\$\.(?P<path>[\w.]+)\s*\+\s*(?P<n>\d+)(?P<unit>d|h|m|s)$")


def _parse_offset(token: str) -> int:
    match = _OFFSET.match(token)
    if not match:
        raise ValueError(f"bad at offset: {token}")
    amount = int(match.group(1))
    unit = match.group(2)
    return amount * {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]


def _json_path(data: Any, path: str) -> Any:
    cur: Any = data
    for part in path.lstrip("$.").split("."):
        if part == "":
            continue
        if not isinstance(cur, dict) or part not in cur:
            raise KeyError(path)
        cur = cur[part]
    return cur


def _substitute(value: Any, saved: dict[str, str], simulated_now: str, webhook: str) -> Any:
    if isinstance(value, str):
        out = value
        out = out.replace("${SIMULATED_NOW}", simulated_now)
        out = out.replace("${WEBHOOK_SINK_URL}", webhook)
        for key, stored in saved.items():
            out = out.replace(f"${{{key}}}", stored)
        return out
    if isinstance(value, list):
        return [_substitute(item, saved, simulated_now, webhook) for item in value]
    if isinstance(value, dict):
        return {k: _substitute(v, saved, simulated_now, webhook) for k, v in value.items()}
    return value


def _advance_to(
    client: httpx.Client, headers: dict[str, str], target_offset: int, current: int
) -> int:
    delta = target_offset - current
    if delta < 0:
        raise RuntimeError("fixture steps must be non-decreasing in `at`")
    if delta == 0:
        response = client.post(
            "/v1/test/clock/advance",
            headers=headers,
            json={"seconds": 0},
        )
    else:
        response = client.post(
            "/v1/test/clock/advance",
            headers=headers,
            json={"seconds": delta},
        )
    if response.status_code != 200:
        raise RuntimeError(f"clock advance failed: {response.status_code} {response.text}")
    return target_offset


def _check_relative(body: Any, path_expr: str, expr: str) -> None:
    match = _RELATIVE.match(expr.strip())
    if not match:
        raise AssertionError(f"unsupported relative expect: {expr}")
    anchor = _json_path(body, "$." + match.group("path"))
    amount = int(match.group("n"))
    unit = match.group("unit")
    start = datetime.fromisoformat(str(anchor).replace("Z", "+00:00")).astimezone(UTC)
    delta = {
        "s": timedelta(seconds=amount),
        "m": timedelta(minutes=amount),
        "h": timedelta(hours=amount),
        "d": timedelta(days=amount),
    }[unit]
    expected = start + delta
    got = datetime.fromisoformat(
        str(_json_path(body, path_expr)).replace("Z", "+00:00")
    ).astimezone(UTC)
    if got != expected:
        raise AssertionError(f"{path_expr}: expected {expected.isoformat()}, got {got.isoformat()}")


def run_example(
    client: httpx.Client,
    example: dict[str, Any],
    keys: dict[str, str],
    webhook: str,
) -> None:
    saved: dict[str, str] = {}
    current_offset = 0
    reviewer = {"Authorization": f"Bearer {keys['reviewer']}"}
    for index, step in enumerate(example["steps"]):
        target = _parse_offset(step["at"])
        current_offset = _advance_to(client, reviewer, target, current_offset)
        simulated = client.post(
            "/v1/test/clock/advance",
            headers=reviewer,
            json={"seconds": 0},
        ).json()["simulated_time"]
        method, _, path = step["call"].partition(" ")
        path = _substitute(path, saved, simulated, webhook)
        body = _substitute(step.get("body"), saved, simulated, webhook)
        role = step["as"]
        headers = {"Authorization": f"Bearer {keys[role]}"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        response = client.request(method, path, headers=headers, json=body)
        expect = step.get("expect") or {}
        status = expect.get("status")
        if status is not None and response.status_code != status:
            raise AssertionError(
                f"{example['id']} step {index}: expected {status}, got "
                f"{response.status_code}: {response.text}"
            )
        payload: Any
        if response.content:
            try:
                payload = response.json()
            except json.JSONDecodeError:
                payload = response.text
        else:
            payload = None
        for name, path_expr in (expect.get("save") or {}).items():
            saved[name] = str(_json_path(payload, path_expr))
        for path_expr, wanted in (expect.get("eq") or {}).items():
            got = _json_path(payload, path_expr)
            if got != wanted:
                raise AssertionError(
                    f"{example['id']} step {index}: {path_expr} expected {wanted!r}, got {got!r}"
                )
        for path_expr, expr in (expect.get("relative") or {}).items():
            _check_relative(payload, path_expr, expr)
        print(f"  ok {example['id']} step {index}: {step['call']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--example", action="append", default=[])
    args = parser.parse_args(argv)
    keys = {
        "system": os.environ.get("INGEST_KEY")
        or os.environ.get("INGEST_KEY_SYS_42", "test-ingest-key-sys-42-0d6f1c2e9b8a4f37"),
        "reviewer": os.environ.get("REVIEWER_KEY", "test-reviewer-key-rev-1-5a7e3b9c1d2f4086"),
        "viewer": os.environ.get("VIEWER_KEY", "test-viewer-key-viewer-1-8c4d2e6f0a1b3957"),
    }
    webhook = os.environ.get("WEBHOOK_SINK_URL", "http://127.0.0.1:8765/hook/model5")
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    selected = set(args.example)
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60.0) as client:
        for example in data["examples"]:
            if selected and example["id"] not in selected:
                continue
            print(f"running {example['id']}: {example['title']}")
            run_example(client, example, keys, webhook)
    print("model5 fixtures ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
