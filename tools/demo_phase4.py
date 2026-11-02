#!/usr/bin/env python3
"""The Phase 4 demo: spec section 12's ten steps over HTTP only (decision 146).

Usage: python tools/demo_phase4.py [--keep-going]

Environment: BASE_URL, INGEST_KEY (or INGEST_KEY_SYS_42), REVIEWER_KEY, VIEWER_KEY,
WEBHOOK_SINK_URL (where the service posts) and WEBHOOK_SINK_READ_URL (where the demo reads the
same channel; defaults to WEBHOOK_SINK_URL). A second channel, the same URLs with "-reminder"
appended, receives a reminder-only webhook.

Each step prints "step N ok: ..." or "step N FAILED: ...". The demo stops at the first mismatch
(status, body field, state, ledger count or sink content) and exits 1; with --keep-going it
runs the remaining steps for the record and still exits 1. Exit 0 means all ten steps passed.
Expected values come from spec section 12 and the sections it rests on (the Ex-1 values of 4.7,
the template of 1.3, the windows and instants of section 6, the role matrix of section 7, the
reminder body of 3.5), never from the service's code.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
VERIFIER = ROOT / "tools" / "verify_ledger.py"

DEFAULT_KEYS = {
    "system": "test-ingest-key-sys-42-0d6f1c2e9b8a4f37",
    "reviewer": "test-reviewer-key-rev-1-5a7e3b9c1d2f4086",
    "viewer": "test-viewer-key-viewer-1-8c4d2e6f0a1b3957",
}
DEFAULT_SINK = "http://127.0.0.1:8765/hook/demo"

HARM_KEYS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)
RIGHTS = "fundamental_rights_infringement"
# Spec 1.3 template with the seeded harm_context row (spec 4.4, README 10.3).
CONTEXT = (
    "sys-42 makes automated decisions about natural persons, so degraded performance can "
    "infringe their fundamental rights (Art. 3(49)(c))."
)
FORBIDDEN = {"error_code": "FORBIDDEN_ROLE", "message": "this role may not call this endpoint"}
DAY = 86400
FOUR_HOURS = 4 * 3600
SINK_WAIT_SECONDS = 60.0
# Section 12 step 10: what the trail shows, in order of first appearance.
TRAIL = (
    "EVENT_FLAGGED",
    "NOTIFICATION_REQUESTED",
    "NOTIFICATION_DELIVERED",
    "EVENT_VIEWED",
    "REVIEW_STARTED",
    "DISPOSITION_RECORDED",
    "DISPOSITION_REVISED",
    "REMINDER_TRIGGERED",
)

_API_TIME = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(\.\d{1,6})?Z$")


class Failed(Exception):
    pass


def check(condition: bool, message: str) -> None:
    if not condition:
        raise Failed(message)


def parse_time(text: Any) -> datetime:
    match = _API_TIME.match(str(text))
    check(match is not None, f"not an API timestamp: {text!r}")
    assert match is not None
    fraction = (match.group(2) or ".0")[1:].ljust(6, "0")
    return datetime.fromisoformat(match.group(1)).replace(tzinfo=UTC) + timedelta(
        microseconds=int(fraction)
    )


def api_time(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def instant(anchor: datetime, deadline: datetime, pct: int) -> datetime:
    """Spec section 6: anchor + ceil((deadline - anchor) * pct / 100), whole microseconds."""
    window = (deadline - anchor) // timedelta(microseconds=1)
    return anchor + timedelta(microseconds=-(-window * pct // 100))


@dataclass(frozen=True)
class Channel:
    url: str
    read_url: str


@dataclass
class Demo:
    client: httpx.Client
    keys: dict[str, str]
    flags: Channel  # flag_notification and reminder (section 12 step 2)
    reminders: Channel  # reminder only (Addendum rule 7)
    state: dict[str, Any] = field(default_factory=dict)

    # HTTP --------------------------------------------------------------------------------
    def headers(self, role: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.keys[role]}"}

    def call(
        self, method: str, path: str, role: str, status: int, body: Any = None
    ) -> httpx.Response:
        response = self.client.request(method, path, headers=self.headers(role), json=body)
        check(
            response.status_code == status,
            f"{method} {path} as {role}: expected {status}, got {response.status_code} "
            f"{response.text[:300]}",
        )
        return response

    def advance(self, seconds: int) -> datetime:
        body = self.call(
            "POST", "/v1/test/clock/advance", "reviewer", 200, {"seconds": seconds}
        ).json()
        return parse_time(body["simulated_time"])

    def advance_to(self, target: datetime) -> datetime:
        now = self.advance(0)
        if now < target:
            micros = (target - now) // timedelta(microseconds=1)
            now = self.advance(-(-micros // 1_000_000))
        check(now >= target, f"clock at {now}, wanted {target}")
        return now

    def event(self, role: str = "viewer") -> dict[str, Any]:
        detail: dict[str, Any] = self.call(
            "GET", f"/v1/events/{self.state['event']}", role, 200
        ).json()
        return detail

    def entries(self) -> int:
        count: int = self.call("GET", "/v1/ledger/verify", "viewer", 200).json()["entries_checked"]
        return count

    def dispose(self, tier: str, role: str = "reviewer", status: int = 200) -> httpx.Response:
        fields = {key: {"status": "NOT_APPLICABLE", "evidence": None} for key in HARM_KEYS}
        fields[RIGHTS] = {"status": "CONFIRMED", "evidence": None}
        return self.call(
            "POST",
            f"/v1/events/{self.state['event']}/disposition",
            role,
            status,
            {"tier": tier, "rationale": None, "harm_fields": fields},
        )

    # Sink --------------------------------------------------------------------------------
    def received(self, channel: Channel, notification_type: str) -> list[dict[str, Any]]:
        response = self.client.get(channel.read_url)
        check(response.status_code == 200, f"sink {channel.read_url}: {response.status_code}")
        # Delivery is at least once (spec 3.4): a redelivered notification_id counts once.
        found = []
        seen: set[str] = set()
        for item in response.json():
            body = item.get("body")
            if isinstance(body, dict) and body.get("notification_type") == notification_type:
                notification_id = body.get("notification_id")
                if notification_id is not None:
                    if notification_id in seen:
                        continue
                    seen.add(notification_id)
                found.append(body)
        return found

    def wait_for(
        self, channel: Channel, notification_type: str, count: int
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + SINK_WAIT_SECONDS
        found = self.received(channel, notification_type)
        while len(found) < count and time.monotonic() < deadline:
            time.sleep(0.5)
            found = self.received(channel, notification_type)
        return found

    def event_reminders(self, channel: Channel) -> list[dict[str, Any]]:
        return [
            body
            for body in self.received(channel, "reminder")
            if body.get("event_id") == self.state["event"]
        ]


# The ten steps of spec section 12 -------------------------------------------------------------


def step1_reset(demo: Demo) -> str:
    response = demo.call("POST", "/v1/test/reset", "reviewer", 204)
    check(response.content == b"", f"reset body not empty: {response.text!r}")
    verify = demo.call("GET", "/v1/ledger/verify", "viewer", 200).json()
    check(verify["valid"] is True, f"verify after reset: {verify}")
    check(
        verify["entries_checked"] == 1, f"ledger after reset: {verify['entries_checked']} entries"
    )
    for channel in (demo.flags, demo.reminders):
        demo.client.delete(channel.read_url).raise_for_status()
    return "204, no body; ledger verifies with 1 entry"


def step2_webhooks(demo: Demo) -> str:
    both = demo.call(
        "POST",
        "/v1/webhooks",
        "reviewer",
        201,
        {"url": demo.flags.url, "event_types": ["flag_notification", "reminder"]},
    ).json()
    only = demo.call(
        "POST",
        "/v1/webhooks",
        "reviewer",
        201,
        {"url": demo.reminders.url, "event_types": ["reminder"]},
    ).json()
    check(str(both.get("webhook_id", "")).startswith("wh-"), f"webhook body {both}")
    check(str(only.get("webhook_id", "")).startswith("wh-"), f"webhook body {only}")
    return f"201 {both['webhook_id']} (flag + reminder), 201 {only['webhook_id']} (reminder)"


def step3_flag(demo: Demo) -> str:
    detected = demo.advance(0).replace(microsecond=0)
    body = demo.call(
        "POST",
        "/v1/metrics",
        "system",
        201,
        {
            "system_id": "sys-42",
            "metric_name": "accuracy",
            "metric_type": "performance",
            "value": 0.61,
            "timestamp": api_time(detected),
        },
    ).json()
    check(set(body) == {"sample_id", "flagged_event_id"}, f"metrics body {body}")
    demo.state.update(event=body["flagged_event_id"], detected=detected)
    flags = demo.wait_for(demo.flags, "flag_notification", 1)
    mine = [b for b in flags if b.get("event_id") == demo.state["event"]]
    check(len(flags) == 1 and len(mine) == 1, f"flag channel holds {flags}")
    check(mine[0].get("type") == "threshold_breach", f"flag body {mine[0]}")
    check(
        demo.received(demo.reminders, "flag_notification") == [], "reminder-only channel got a flag"
    )
    # The delivery is recorded just after the receiver answers; wait for it (the export is not
    # logged), so the trail's order in step 10 does not depend on the worker's timing.
    deadline = time.monotonic() + SINK_WAIT_SECONDS
    while not any(
        entry["entry_type"] == "NOTIFICATION_DELIVERED"
        and entry["subject_id"] == demo.state["event"]
        for entry in demo.call("GET", "/v1/ledger/export", "viewer", 200).json()
    ):
        check(time.monotonic() < deadline, "no NOTIFICATION_DELIVERED for the flag")
        time.sleep(0.5)
    return f"201 {demo.state['event']}; one flag_notification received and recorded"


def step4_viewer(demo: Demo) -> str:
    event = demo.event("viewer")
    check(event["state"] == "FLAGGED", f"state {event['state']}")
    check(
        event["detected_at"] == api_time(demo.state["detected"]),
        f"detected_at {event['detected_at']}",
    )
    expected = (
        f"accuracy = 0.61 is below the limit 0.70 (threshold thr-2) at {event['detected_at']}. "
        f"{CONTEXT}"
    )
    fields = event["harm_fields"]
    check(fields[RIGHTS]["evidence"] == expected, f"rights evidence {fields[RIGHTS]['evidence']!r}")
    for key in HARM_KEYS:
        check(
            fields[key]["status"] == "PREFILLED_PENDING_CONFIRMATION",
            f"{key} status {fields[key]['status']}",
        )
        check(
            fields[key]["confirmed_by"] is None, f"{key} confirmed_by {fields[key]['confirmed_by']}"
        )
        if key != RIGHTS:
            check(fields[key]["evidence"] is None, f"{key} evidence {fields[key]['evidence']!r}")
    check(event["disposition"] is None, f"disposition {event['disposition']}")
    return (
        "FLAGGED; rights pre-filled, three null, all PREFILLED_PENDING_CONFIRMATION; no disposition"
    )


def step5_reviewer(demo: Demo) -> str:
    state = demo.event("reviewer")["state"]
    check(state == "UNDER_REVIEW", f"state {state}")
    return "UNDER_REVIEW"


def step6_denied(demo: Demo) -> str:
    before = demo.entries()
    response = demo.dispose("OTHER_SERIOUS", role="viewer", status=403)
    check(response.json() == FORBIDDEN, f"403 body {response.json()}")
    after = demo.entries()
    check(after == before, f"ledger went from {before} to {after} entries")
    state = demo.event("viewer")["state"]
    check(state == "UNDER_REVIEW", f"state {state}")
    return "403 FORBIDDEN_ROLE; still UNDER_REVIEW; no new ledger entry"


def step7_assign(demo: Demo) -> str:
    demo.advance(FOUR_HOURS)
    event = demo.dispose("OTHER_SERIOUS").json()
    check(event["state"] == "DISPOSITIONED", f"state {event['state']}")
    check(event["disposition"]["tier"] == "OTHER_SERIOUS", f"tier {event['disposition']}")
    anchor = parse_time(event["disposition"]["assigned_at"])
    deadline = parse_time(event["deadline_utc"])
    check(deadline - anchor == timedelta(days=15), f"deadline {event['deadline_utc']}")
    gap = anchor - parse_time(event["detected_at"])
    check(
        timedelta(seconds=FOUR_HOURS) <= gap < timedelta(seconds=FOUR_HOURS + 60),
        f"assigned_at - detected_at = {gap}",
    )
    demo.state.update(anchor=anchor, assigned_at=event["disposition"]["assigned_at"])
    return f"200 DISPOSITIONED; deadline = assigned_at + 15 d; risk gap {gap}"


def step8_revise(demo: Demo) -> str:
    demo.advance(3 * DAY)
    event = demo.dispose("DEATH").json()
    anchor = demo.state["anchor"]
    check(event["disposition"]["tier"] == "DEATH", f"tier {event['disposition']}")
    check(
        event["disposition"]["assigned_at"] == demo.state["assigned_at"],
        f"assigned_at moved to {event['disposition']['assigned_at']}",
    )
    deadline = parse_time(event["deadline_utc"])
    check(deadline == anchor + timedelta(days=10), f"deadline {event['deadline_utc']}")
    revisions = event["revisions"]
    check(len(revisions) == 1, f"revisions {revisions}")
    check(
        (revisions[0]["prior_tier"], revisions[0]["new_tier"], revisions[0]["reviewer_id"])
        == ("OTHER_SERIOUS", "DEATH", "rev-1"),
        f"revision {revisions[0]}",
    )
    demo.state.update(deadline=deadline, deadline_utc=event["deadline_utc"])
    return (
        "200; deadline = original assigned_at + 10 d; one revision OTHER_SERIOUS -> DEATH by rev-1"
    )


def step9_reminders(demo: Demo) -> str:
    anchor, deadline = demo.state["anchor"], demo.state["deadline"]
    expected: list[int] = []
    for pct in (49, 50, 79, 80, 99, 100):
        demo.advance_to(instant(anchor, deadline, pct))
        if pct in (50, 80, 100):
            expected.append(pct)
            demo.wait_for(demo.flags, "reminder", len(expected))
        got = [b.get("escalation_pct") for b in demo.event_reminders(demo.flags)]
        check(got == expected, f"at {pct}% the receiver holds reminders {got}, expected {expected}")
    demo.advance(DAY)
    bodies = demo.event_reminders(demo.flags)
    got = [b.get("escalation_pct") for b in bodies]
    check(got == [50, 80, 100], f"a day after 100% the receiver holds reminders {got}")
    check(len({b.get("notification_id") for b in bodies}) == 3, "reminder notification ids repeat")
    for body in bodies:
        due = instant(anchor, deadline, int(body["escalation_pct"]))
        check(body.get("tier") == "DEATH", f"reminder tier {body.get('tier')}")
        check(body.get("deadline_utc") == demo.state["deadline_utc"], f"reminder deadline {body}")
        check(body.get("assigned_at") == demo.state["assigned_at"], f"reminder assigned_at {body}")
        check(parse_time(body.get("due_at")) == due, f"reminder due_at {body.get('due_at')}")
        check(parse_time(body.get("sent_at")) >= due, f"reminder sent before due: {body}")
    only = [b.get("escalation_pct") for b in demo.event_reminders(demo.reminders)]
    check(only == [50, 80, 100], f"reminder-only channel holds {only}")
    check(
        demo.received(demo.reminders, "flag_notification") == [], "reminder-only channel got a flag"
    )
    return "exactly 50, 80, 100 at their steps; none at 49, 79, 99 or after"


def step10_audit_and_verify(demo: Demo) -> str:
    trail = demo.call("GET", f"/v1/events/{demo.state['event']}/audit", "viewer", 200).json()
    actions = [row["action"] for row in trail]
    first_seen = list(dict.fromkeys(a for a in actions if a in TRAIL))
    check(first_seen == list(TRAIL), f"trail order {first_seen}")
    check(
        actions.count("REMINDER_TRIGGERED") == 3,
        f"REMINDER_TRIGGERED x{actions.count('REMINDER_TRIGGERED')}",
    )
    check(actions.count("DISPOSITION_REVISED") == 1, f"trail {actions}")
    verify = demo.call("GET", "/v1/ledger/verify", "viewer", 200).json()
    check(verify["valid"] is True, f"server verify {verify}")
    export = demo.call("GET", "/v1/ledger/export", "viewer", 200).json()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "export.json"
        path.write_text(json.dumps(export), encoding="utf-8")
        run = subprocess.run(
            [sys.executable, str(VERIFIER), str(path)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
    check(run.returncode == 0, f"verify_ledger.py exit {run.returncode}: {run.stdout} {run.stderr}")
    offline = json.loads(run.stdout)
    check(
        offline == {"valid": True, "entries_checked": len(export), "first_mismatch": None},
        f"verify_ledger.py {offline}",
    )
    return f"trail complete; server and offline verifiers valid ({len(export)} entries)"


STEPS: list[tuple[str, Callable[[Demo], str]]] = [
    ("reset", step1_reset),
    ("webhooks", step2_webhooks),
    ("flag", step3_flag),
    ("viewer detail", step4_viewer),
    ("reviewer opens", step5_reviewer),
    ("viewer denied", step6_denied),
    ("assign OTHER_SERIOUS after 4 h", step7_assign),
    ("revise to DEATH after 3 days", step8_revise),
    ("reminders", step9_reminders),
    ("audit and verify", step10_audit_and_verify),
]


def run(demo: Demo, keep_going: bool = False) -> int:
    failed = 0
    for number, (title, step) in enumerate(STEPS, start=1):
        try:
            detail = step(demo)
        except (Failed, KeyError, TypeError, ValueError, httpx.HTTPError) as error:
            failed += 1
            print(f"step {number} FAILED: {title}: {type(error).__name__}: {error}")
            if not keep_going:
                print("demo FAILED")
                return 1
            continue
        print(f"step {number} ok: {title}: {detail}")
    if failed:
        print(f"demo FAILED ({failed} steps)")
        return 1
    print("demo ok")
    return 0


def _channel(url: str, read_url: str, suffix: str = "") -> Channel:
    return Channel(url=url + suffix, read_url=read_url + suffix)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep-going", action="store_true")
    args = parser.parse_args(argv)
    base = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    keys = {
        "system": os.environ.get("INGEST_KEY")
        or os.environ.get("INGEST_KEY_SYS_42", DEFAULT_KEYS["system"]),
        "reviewer": os.environ.get("REVIEWER_KEY", DEFAULT_KEYS["reviewer"]),
        "viewer": os.environ.get("VIEWER_KEY", DEFAULT_KEYS["viewer"]),
    }
    url = os.environ.get("WEBHOOK_SINK_URL", DEFAULT_SINK).rstrip("/")
    read_url = (os.environ.get("WEBHOOK_SINK_READ_URL") or url).rstrip("/")
    with httpx.Client(base_url=base, timeout=60.0) as client:
        demo = Demo(
            client=client,
            keys=keys,
            flags=_channel(url, read_url),
            reminders=_channel(url, read_url, "-reminder"),
        )
        return run(demo, keep_going=args.keep_going)


if __name__ == "__main__":
    sys.exit(main())
