#!/usr/bin/env python3
"""HTTP runner for tests/fixtures/phase4/model5_examples.json (Phase 4 spec 4.7; decision 144).

Usage: python tools/run_model5.py [--base-url URL] [--example ex1 ...]

Environment: BASE_URL, INGEST_KEY (or INGEST_KEY_SYS_42), REVIEWER_KEY, VIEWER_KEY,
WEBHOOK_SINK_URL (where the service posts) and WEBHOOK_SINK_READ_URL (where this runner reads
the same channel; defaults to WEBHOOK_SINK_URL).

Each example runs in order. A step's "at" is an offset from the example's start (d0); the runner
advances the test clock to it, then makes the call (if any) and checks the expectations. The
first mismatch fails the example; the run exits 1 if any example failed, 0 if all passed. The
fixture README describes the step format.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "phase4" / "model5_examples.json"

DEFAULT_KEYS = {
    "system": "test-ingest-key-sys-42-0d6f1c2e9b8a4f37",
    "reviewer": "test-reviewer-key-rev-1-5a7e3b9c1d2f4086",
    "viewer": "test-viewer-key-viewer-1-8c4d2e6f0a1b3957",
}
DEFAULT_SINK = "http://127.0.0.1:8765/hook/model5"
EXPECTATIONS = frozenset(
    {"status", "body", "eq", "len", "relative", "save", "audit_delta", "subject"}
)
SINK_WAIT_SECONDS = 15.0

_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}
_DURATION = re.compile(r"^(?:\d+[dhms])+$")
_PART = re.compile(r"(\d+)([dhms])")
_API_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,6})?Z$")
_VARIABLE = re.compile(r"\$\{(\w+)\}")
_RELATIVE = re.compile(
    r"^(?P<base>\$(?:\.[\w.]+)?|\$\{\w+\})\s*\+\s*(?P<dur>(?:\d+[dhms])+)"
    r"(?:\s*~\s*(?P<tol>(?:\d+[dhms])+))?$"
)


class Mismatch(AssertionError):
    """An expectation that did not hold."""


class StepFailure(Exception):
    def __init__(self, example_id: str, index: int, call: str, message: str) -> None:
        super().__init__(f"{example_id} step {index} ({call}): {message}")
        self.example_id = example_id
        self.index = index
        self.message = message


@dataclass(frozen=True)
class SinkChannel:
    """One tools/webhook_sink.py channel: url is what the service posts to, read_url where the
    runner reads it."""

    url: str
    read_url: str

    def clear(self, client: httpx.Client) -> None:
        client.delete(self.read_url).raise_for_status()

    def received(self, client: httpx.Client) -> list[dict[str, Any]]:
        response = client.get(self.read_url)
        response.raise_for_status()
        items: list[dict[str, Any]] = response.json()
        return items


# Parsing ------------------------------------------------------------------------------------


def parse_duration(text: str) -> timedelta:
    if not _DURATION.match(text):
        raise ValueError(f"bad duration: {text!r}")
    return timedelta(seconds=sum(int(n) * _UNIT_SECONDS[u] for n, u in _PART.findall(text)))


def parse_offset(token: str) -> int:
    """'+5d4h' -> seconds from the example's start."""
    if not token.startswith("+"):
        raise ValueError(f"bad at offset: {token!r}")
    return int(parse_duration(token[1:]).total_seconds())


def parse_time(text: Any) -> datetime:
    match = _API_TIME.match(str(text))
    if not match:
        raise Mismatch(f"not an API timestamp: {text!r}")
    fraction = (match.group(2) or ".0")[1:].ljust(6, "0")
    whole = datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC)
    return whole + timedelta(microseconds=int(fraction))


def api_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def json_path(data: Any, path: str) -> Any:
    """'$', '$.a.b' or '$.list.0.key'. KeyError when the path is not there."""
    if path == "$":
        return data
    if not path.startswith("$."):
        raise KeyError(path)
    current = data
    for part in path[2:].split("."):
        if isinstance(current, dict) and part in current:
            current = current[part]
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            raise KeyError(path)
    return current


def substitute(value: Any, variables: dict[str, str]) -> Any:
    if isinstance(value, str):

        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in variables:
                raise KeyError(f"unknown variable ${{{name}}}")
            return variables[name]

        return _VARIABLE.sub(replace, value)
    if isinstance(value, list):
        return [substitute(item, variables) for item in value]
    if isinstance(value, dict):
        return {key: substitute(item, variables) for key, item in value.items()}
    return value


# Checks -------------------------------------------------------------------------------------


def check_relative(body: Any, path: str, expr: str, variables: dict[str, str]) -> None:
    """'<base> + <duration> [~<tolerance>]': exact, or within [expected, expected + tolerance]."""
    match = _RELATIVE.match(expr.strip())
    if not match:
        raise ValueError(f"bad relative expectation: {expr!r}")
    base_ref = match.group("base")
    try:
        base_text = (
            substitute(base_ref, variables)
            if base_ref.startswith("${")
            else json_path(body, base_ref)
        )
        got_text = json_path(body, path)
    except KeyError as missing:
        raise Mismatch(f"{path}: missing {missing}") from None
    expected = parse_time(base_text) + parse_duration(match.group("dur"))
    got = parse_time(got_text)
    tolerance = match.group("tol")
    if tolerance is None:
        if got != expected:
            raise Mismatch(f"{path}: expected {expected.isoformat()}, got {got_text}")
    elif not expected <= got <= expected + parse_duration(tolerance):
        raise Mismatch(
            f"{path}: expected {expected.isoformat()} + up to {tolerance}, got {got_text}"
        )


def sink_bodies(
    items: list[dict[str, Any]], notification_type: str, event_id: str
) -> list[dict[str, Any]]:
    """Bodies of one notification_type for one event, in arrival order (Addendum rule 7).
    Delivery is at least once (spec 3.4), so a redelivered notification_id counts once."""
    found = []
    seen: set[str] = set()
    for item in items:
        body = item.get("body")
        if (
            isinstance(body, dict)
            and body.get("notification_type") == notification_type
            and body.get("event_id") == event_id
        ):
            notification_id = body.get("notification_id")
            if notification_id is not None:
                if notification_id in seen:
                    continue
                seen.add(notification_id)
            found.append(body)
    return found


def _subset(expected: dict[str, Any], got: Any, where: str) -> None:
    if not isinstance(got, dict):
        raise Mismatch(f"{where}: expected an object, got {got!r}")
    for key, value in expected.items():
        if key not in got:
            raise Mismatch(f"{where}: no {key}")
        if isinstance(value, dict):
            _subset(value, got[key], f"{where}.{key}")
        elif got[key] != value:
            raise Mismatch(f"{where}.{key}: expected {value!r}, got {got[key]!r}")


def match_delta(
    new_entries: list[dict[str, Any]], subject: str, expected: list[dict[str, Any]]
) -> dict[str, str]:
    """The ledger entries a call appended about `subject` must be exactly `expected`, in order
    (entry_type, optional actor_id, optional payload subset). Returns the values to save."""
    mine = [entry for entry in new_entries if entry.get("subject_id") == subject]
    got_types = [(entry["entry_type"], entry["actor_id"]) for entry in mine]
    if len(mine) != len(expected):
        raise Mismatch(f"appended for {subject}: expected {len(expected)} entries, got {got_types}")
    saved: dict[str, str] = {}
    for index, (entry, want) in enumerate(zip(mine, expected, strict=True)):
        where = f"appended[{index}]"
        if entry["entry_type"] != want["entry_type"]:
            raise Mismatch(f"{where}: expected {want['entry_type']}, got {got_types}")
        if "actor_id" in want and entry["actor_id"] != want["actor_id"]:
            raise Mismatch(f"{where}: actor expected {want['actor_id']}, got {entry['actor_id']}")
        if "payload" in want:
            _subset(want["payload"], entry["payload"], f"{where}.payload")
        for name, path in (want.get("save") or {}).items():
            saved[name] = str(json_path(entry, "$." + path))
    return saved


# Running ------------------------------------------------------------------------------------


def _advance(client: httpx.Client, headers: dict[str, str], seconds: int) -> datetime:
    response = client.post("/v1/test/clock/advance", headers=headers, json={"seconds": seconds})
    if response.status_code != 200:
        raise Mismatch(f"clock advance: {response.status_code} {response.text}")
    return parse_time(response.json()["simulated_time"])


def _export(client: httpx.Client, headers: dict[str, str]) -> list[dict[str, Any]]:
    response = client.get("/v1/ledger/export", headers=headers)
    if response.status_code != 200:
        raise Mismatch(f"ledger export: {response.status_code} {response.text}")
    entries: list[dict[str, Any]] = response.json()
    return entries


def _check_sink(
    client: httpx.Client, sink: SinkChannel, spec: dict[str, Any], variables: dict[str, str]
) -> None:
    """{"notification_type", "event", and "pcts" (reminders) or "count"}: exact, after waiting
    up to SINK_WAIT_SECONDS for the expected number to arrive."""
    spec = substitute(spec, variables)
    want_pcts = spec.get("pcts")
    want = len(want_pcts) if want_pcts is not None else int(spec["count"])
    deadline = time.monotonic() + (SINK_WAIT_SECONDS if want else 0)
    while True:
        bodies = sink_bodies(sink.received(client), spec["notification_type"], spec["event"])
        if len(bodies) >= want or time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if want_pcts is not None:
        got_pcts = [body.get("escalation_pct") for body in bodies]
        if got_pcts != want_pcts:
            raise Mismatch(
                f"sink reminders for {spec['event']}: expected {want_pcts}, got {got_pcts}"
            )
    elif len(bodies) != want:
        kind = spec["notification_type"]
        raise Mismatch(f"sink {kind} for {spec['event']}: expected {want}, got {len(bodies)}")
    for path, value in (spec.get("eq") or {}).items():
        for body in bodies:
            try:
                got = json_path(body, path)
            except KeyError:
                raise Mismatch(f"sink body has no {path}: {body}") from None
            if got != value:
                raise Mismatch(f"sink body {path}: expected {value!r}, got {got!r}")


def _check_response(
    response: httpx.Response,
    expect: dict[str, Any],
    variables: dict[str, str],
) -> None:
    status = expect.get("status")
    if status is not None and response.status_code != status:
        raise Mismatch(f"status expected {status}, got {response.status_code}: {response.text}")
    payload: Any = None
    if response.content:
        try:
            payload = response.json()
        except json.JSONDecodeError:
            payload = response.text
    if "body" in expect and payload != substitute(expect["body"], variables):
        raise Mismatch(f"body expected {expect['body']!r}, got {payload!r}")
    try:
        for path, value in (expect.get("eq") or {}).items():
            got = json_path(payload, path)
            wanted = substitute(value, variables)
            if got != wanted:
                raise Mismatch(f"{path}: expected {wanted!r}, got {got!r}")
        for path, count in (expect.get("len") or {}).items():
            got = json_path(payload, path)
            if not isinstance(got, list) or len(got) != count:
                raise Mismatch(f"{path}: expected {count} items, got {got!r}")
        for path, expr in (expect.get("relative") or {}).items():
            check_relative(payload, path, expr, variables)
        for name, path in (expect.get("save") or {}).items():
            # "@NOW" saves this step's simulated now (whole seconds), e.g. a sample's timestamp.
            variables[name] = variables["NOW"] if path == "@NOW" else str(json_path(payload, path))
    except KeyError as missing:
        raise Mismatch(f"missing {missing} in {payload!r}") from None


def run_example(
    client: httpx.Client,
    example: dict[str, Any],
    keys: dict[str, str],
    sink: SinkChannel,
) -> list[str]:
    """Run one example; return the 'ok' lines, or raise StepFailure at the first mismatch."""
    headers = {role: {"Authorization": f"Bearer {key}"} for role, key in keys.items()}
    variables: dict[str, str] = {"WEBHOOK_SINK_URL": sink.url}
    lines: list[str] = []
    offset = 0
    for index, step in enumerate(example["steps"]):
        call = step.get("call", "check")
        try:
            target = parse_offset(step["at"])
            if target < offset:
                raise ValueError("steps must be in non-decreasing `at` order")
            if index == 0:
                sink.clear(client)
            now = _advance(client, headers["reviewer"], target - offset)
            offset = target
            variables["NOW"] = api_time(now)
            if index == 0:
                variables["T0"] = variables["NOW"]
            if "call" in step:
                expect = step.get("expect") or {}
                method, _, path = step["call"].partition(" ")
                subject = (
                    substitute(expect.get("subject", "${event}"), variables)
                    if ("audit_delta" in expect)
                    else None
                )
                before = _export(client, headers["viewer"]) if subject else []
                response = client.request(
                    method,
                    substitute(path, variables),
                    headers=headers[step["as"]],
                    json=substitute(step["body"], variables) if "body" in step else None,
                )
                _check_response(response, expect, variables)
                if path == "/v1/test/reset" and response.status_code == 204:
                    # A reset puts simulated time back to wall time: d0 starts here.
                    variables["NOW"] = variables["T0"] = api_time(
                        _advance(client, headers["reviewer"], 0)
                    )
                if subject is not None:
                    last = before[-1]["seq"] if before else 0
                    after = _export(client, headers["viewer"])
                    new = [entry for entry in after if entry["seq"] > last]
                    variables.update(
                        match_delta(new, subject, substitute(expect["audit_delta"], variables))
                    )
            if "sink" in step:
                _check_sink(client, sink, step["sink"], variables)
        except (Mismatch, KeyError, ValueError, httpx.HTTPError) as error:
            raise StepFailure(example["id"], index, call, str(error)) from None
        lines.append(f"ok {example['id']} step {index}: {call}")
    return lines


def run_all(
    client: httpx.Client,
    data: dict[str, Any],
    keys: dict[str, str],
    sink: SinkChannel,
    selected: set[str] | None = None,
) -> int:
    failed = []
    for example in data["examples"]:
        if selected and example["id"] not in selected:
            continue
        print(f"running {example['id']}: {example['title']}")
        try:
            for line in run_example(client, example, keys, sink):
                print(f"  {line}")
        except StepFailure as failure:
            failed.append(example["id"])
            print(f"  {failure.example_id} FAILED at step {failure.index}: {failure.message}")
            continue
        print(f"  {example['id']} passed")
    if failed:
        print(f"model5: FAILED {', '.join(failed)}")
        return 1
    print("model5: all examples passed")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=os.environ.get("BASE_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--example", action="append", default=[])
    args = parser.parse_args(argv)
    keys = {
        "system": os.environ.get("INGEST_KEY")
        or os.environ.get("INGEST_KEY_SYS_42", DEFAULT_KEYS["system"]),
        "reviewer": os.environ.get("REVIEWER_KEY", DEFAULT_KEYS["reviewer"]),
        "viewer": os.environ.get("VIEWER_KEY", DEFAULT_KEYS["viewer"]),
    }
    url = os.environ.get("WEBHOOK_SINK_URL", DEFAULT_SINK)
    sink = SinkChannel(url=url, read_url=os.environ.get("WEBHOOK_SINK_READ_URL") or url)
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60.0) as client:
        return run_all(client, data, keys, sink, set(args.example))


if __name__ == "__main__":
    sys.exit(main())
