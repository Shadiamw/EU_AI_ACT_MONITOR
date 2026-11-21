#!/usr/bin/env python3
"""The Phase 5 AC-16 scripted trace: spec 10.3 paths S and N over HTTP only.

Usage: python tools/demo_phase5.py [--path S|N]

Environment: BASE_URL, INGEST_KEY (or INGEST_KEY_SYS_42), REVIEWER_KEY, VIEWER_KEY, optional
REVIEWER_2_KEY (rev-2, D17: when set, step S6's revision is made by rev-2), WEBHOOK_SINK_URL
(where the service posts) and WEBHOOK_SINK_READ_URL (where the trace reads the same channel;
defaults to WEBHOOK_SINK_URL).

Each step prints "S<n> ok: ..." or "S<n> FAILED: ..." (N<n> on path N), and the trace stops
at the first failed expectation and exits 1. Exit 0 means both paths passed. The event id of
step S2 (N1 on path N) is carried through every later step, and each step checks it. The
printed trace is the AC-16 artifact the other team reviews (manual check M6).

Expected values come from Phase 5 spec 10.3 and the sections it rests on (3.3 report object,
3.7 submission, D2 closure report, D17 second reviewer) and Phase 4 spec 2.5 (windows), never
from the service's code. tests/platform5/test_ac16_in_process.py runs the same steps through
the ASGI app with an in-process receiver.
"""

from __future__ import annotations

import argparse
import hashlib
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
from typing import Any, Protocol

import httpx

ROOT = Path(__file__).resolve().parent.parent
VERIFIER = ROOT / "tools" / "verify_ledger.py"
REPORT_SCHEMA = ROOT / "docs" / "contract" / "phase5" / "report.schema.json"

DEFAULT_KEYS = {
    "system": "test-ingest-key-sys-42-0d6f1c2e9b8a4f37",
    "reviewer": "test-reviewer-key-rev-1-5a7e3b9c1d2f4086",
    "viewer": "test-viewer-key-viewer-1-8c4d2e6f0a1b3957",
}
DEFAULT_SINK = "http://127.0.0.1:8765/hook/ac16"

HARM_KEYS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)
RIGHTS = "fundamental_rights_infringement"
HOUR = 3600
DAY = 86400
SINK_WAIT_SECONDS = 60.0
RATIONALE = "seasonal shift, no user impact"
RATIONALE_REQUIRED = {
    "error_code": "RATIONALE_REQUIRED",
    "message": "rationale is required when tier is NOT_SERIOUS",
}
NO_DISPOSITION = {
    "error_code": "NO_DISPOSITION",
    "message": "no disposition has been recorded for this event",
}
NOT_SUBMITTABLE = {
    "error_code": "NOT_SUBMITTABLE",
    "message": "a not-serious closure report cannot be submitted",
}
# Spec 10.3 S9: what the trail shows, in order of first appearance.
TRAIL_S = (
    "EVENT_FLAGGED",
    "NOTIFICATION_REQUESTED",
    "NOTIFICATION_DELIVERED",
    "EVENT_VIEWED",
    "REVIEW_STARTED",
    "DISPOSITION_RECORDED",
    "DISPOSITION_REVISED",
    "REPORT_ASSEMBLED",
    "REPORT_SUBMITTED",
)
# N7: as S9, without the revision and the submission.
TRAIL_N = (
    "EVENT_FLAGGED",
    "NOTIFICATION_REQUESTED",
    "NOTIFICATION_DELIVERED",
    "EVENT_VIEWED",
    "REVIEW_STARTED",
    "DISPOSITION_RECORDED",
    "REPORT_ASSEMBLED",
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


class Client(Protocol):
    """What the trace needs from a client: httpx.Client, or the in-process ApiClient."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = ...,
        json: Any = ...,
    ) -> httpx.Response: ...


@dataclass
class Receiver:
    """Where the service's webhook calls are read. `url` is registered with the service;
    `bodies` returns every JSON body received so far, in arrival order; `clear` empties it;
    `pump` is called between reads while waiting (a sleep against a running worker, or a
    zero-second clock advance, which runs one delivery pass, in process)."""

    url: str
    bodies: Callable[[], list[Any]]
    clear: Callable[[], None]
    pump: Callable[[], None]


@dataclass
class Trace:
    client: Client
    keys: dict[str, str]
    receiver: Receiver
    out: Callable[[str], None] = print
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

    def event(self, event_id: str, role: str = "viewer") -> dict[str, Any]:
        detail: dict[str, Any] = self.call("GET", f"/v1/events/{event_id}", role, 200).json()
        check(detail["event_id"] == event_id, f"detail is for {detail['event_id']}")
        return detail

    def audit(self, event_id: str) -> list[str]:
        trail = self.call("GET", f"/v1/events/{event_id}/audit", "viewer", 200).json()
        return [row["action"] for row in trail]

    def dispose(
        self,
        event_id: str,
        tier: str,
        rationale: str | None = None,
        role: str = "reviewer",
        status: int = 200,
    ) -> httpx.Response:
        fields = {key: {"status": "NOT_APPLICABLE", "evidence": None} for key in HARM_KEYS}
        if tier != "NOT_SERIOUS":
            fields[RIGHTS] = {"status": "CONFIRMED", "evidence": None}
        return self.call(
            "POST",
            f"/v1/events/{event_id}/disposition",
            role,
            status,
            {"tier": tier, "rationale": rationale, "harm_fields": fields},
        )

    # Receiver ----------------------------------------------------------------------------
    def received(self, notification_type: str, event_id: str) -> list[dict[str, Any]]:
        """One notification_type for one event. Delivery is at least once (Phase 3 spec 3.4):
        a redelivered notification_id counts once."""
        found = []
        seen: set[str] = set()
        for body in self.receiver.bodies():
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

    def wait_for_flag(self, event_id: str) -> None:
        """One flag_notification for the event at the receiver, and its NOTIFICATION_DELIVERED
        recorded (so the trail's order does not depend on the worker's timing)."""
        deadline = time.monotonic() + SINK_WAIT_SECONDS
        while True:
            flags = self.received("flag_notification", event_id)
            delivered = "NOTIFICATION_DELIVERED" in self.audit(event_id)
            if flags and delivered:
                break
            check(
                time.monotonic() < deadline,
                f"after {SINK_WAIT_SECONDS:.0f} s: {len(flags)} flag(s), delivered={delivered}",
            )
            self.receiver.pump()
        check(len(flags) == 1, f"{len(flags)} flag notifications for {event_id}")
        check(flags[0].get("type") == "threshold_breach", f"flag body {flags[0]}")

    # Shared steps ------------------------------------------------------------------------
    def reset_and_register(self) -> str:
        response = self.call("POST", "/v1/test/reset", "reviewer", 204)
        check(response.content == b"", f"reset body not empty: {response.text!r}")
        self.receiver.clear()
        hook = self.call(
            "POST",
            "/v1/webhooks",
            "reviewer",
            201,
            {"url": self.receiver.url, "event_types": ["flag_notification", "reminder"]},
        ).json()
        check(str(hook.get("webhook_id", "")).startswith("wh-"), f"webhook body {hook}")
        return f"reset 204; webhook {hook['webhook_id']} 201 for flag_notification and reminder"

    def breach(self, key: str, metric: dict[str, Any]) -> str:
        detected = self.advance(0).replace(microsecond=0)
        body = self.call(
            "POST",
            "/v1/metrics",
            "system",
            201,
            {"system_id": "sys-42", **metric, "timestamp": api_time(detected)},
        ).json()
        event_id = body.get("flagged_event_id")
        check(isinstance(event_id, str) and event_id != "", f"metrics body {body}")
        self.state[key] = event_id
        self.state[f"{key}_detected"] = api_time(detected)
        self.wait_for_flag(event_id)
        return f"201 flagged_event_id {event_id}; one flag_notification received and recorded"

    def open_event(self, event_id: str) -> tuple[str, str]:
        viewed = self.event(event_id, "viewer")
        reviewed = self.event(event_id, "reviewer")
        check(reviewed["state"] == "UNDER_REVIEW", f"state after the reviewer {reviewed['state']}")
        return viewed["state"], viewed["harm_fields"][RIGHTS]["status"]

    def fetch_report(self, event_id: str) -> tuple[dict[str, Any], httpx.Response]:
        """The JSON report and its PDF: same id and version; each body's SHA-256 is its own
        X-Report-Sha256 and ETag (spec 3.6)."""
        as_json = self.call("GET", f"/v1/events/{event_id}/report", "reviewer", 200)
        as_pdf = self.call("GET", f"/v1/events/{event_id}/report?format=pdf", "reviewer", 200)
        check(
            as_pdf.headers.get("content-type", "").startswith("application/pdf"),
            f"pdf content-type {as_pdf.headers.get('content-type')}",
        )
        check(as_pdf.content.startswith(b"%PDF"), "the PDF body does not start with %PDF")
        for name, response in (("json", as_json), ("pdf", as_pdf)):
            digest = hashlib.sha256(response.content).hexdigest()
            check(
                response.headers.get("X-Report-Sha256") == digest,
                f"{name}: X-Report-Sha256 {response.headers.get('X-Report-Sha256')} != {digest}",
            )
        report: dict[str, Any] = as_json.json()
        for name, response in (("json", as_json), ("pdf", as_pdf)):
            check(
                response.headers.get("X-Report-Id") == report["report_id"],
                f"{name}: X-Report-Id {response.headers.get('X-Report-Id')}",
            )
            check(
                response.headers.get("X-Report-Version") == str(report["report_version"]),
                f"{name}: X-Report-Version {response.headers.get('X-Report-Version')}",
            )
        check(report["event"]["event_id"] == event_id, f"report event {report['event']}")
        validate_report(report)
        return report, as_json

    def verify_both(self) -> int:
        verify = self.call("GET", "/v1/ledger/verify", "viewer", 200).json()
        check(verify["valid"] is True, f"server verify {verify}")
        export = self.call("GET", "/v1/ledger/export", "viewer", 200).json()
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
        check(run.returncode == 0, f"verify_ledger.py exit {run.returncode}: {run.stdout}")
        offline = json.loads(run.stdout)
        check(
            offline == {"valid": True, "entries_checked": len(export), "first_mismatch": None},
            f"verify_ledger.py {offline}",
        )
        return len(export)


def validate_report(report: dict[str, Any]) -> None:
    """Against docs/contract/phase5/report.schema.json (Draft 2020-12; spec 3.3, AC-13)."""
    from jsonschema import Draft202012Validator

    schema = json.loads(REPORT_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(report), key=str)
    check(not errors, f"report is not schema-valid: {errors[0].message if errors else ''}")


def first_seen(actions: list[str], expected: tuple[str, ...]) -> list[str]:
    return list(dict.fromkeys(action for action in actions if action in expected))


# Path S: breach to submission ---------------------------------------------------------------


def s1(trace: Trace) -> str:
    return trace.reset_and_register()


def s2(trace: Trace) -> str:
    return trace.breach(
        "E", {"metric_name": "accuracy", "metric_type": "performance", "value": 0.61}
    )


def s3(trace: Trace) -> str:
    event_id = trace.state["E"]
    before = trace.event(event_id, "viewer")["state"]
    response = trace.call("GET", f"/v1/events/{event_id}/report", "reviewer", 409)
    check(response.json() == NO_DISPOSITION, f"409 body {response.json()}")
    after = trace.event(event_id, "viewer")["state"]
    check(after == before == "FLAGGED", f"state {before} -> {after}")
    return f"{event_id}: 409 NO_DISPOSITION; state unchanged (FLAGGED)"


def s4(trace: Trace) -> str:
    event_id = trace.state["E"]
    state, rights = trace.open_event(event_id)
    check(state == "FLAGGED", f"viewer saw {state}")
    check(rights == "PREFILLED_PENDING_CONFIRMATION", f"rights status {rights}")
    return (
        f"{event_id}: viewer sees FLAGGED with the rights field pre-filled;"
        " reviewer opens it: UNDER_REVIEW"
    )


def s5(trace: Trace) -> str:
    event_id = trace.state["E"]
    trace.advance(4 * HOUR)
    event = trace.dispose(event_id, "OTHER_SERIOUS").json()
    check(event["event_id"] == event_id, f"disposed {event['event_id']}")
    check(event["state"] == "DISPOSITIONED", f"state {event['state']}")
    assigned = parse_time(event["disposition"]["assigned_at"])
    check(
        parse_time(event["deadline_utc"]) - assigned == timedelta(days=15),
        f"deadline {event['deadline_utc']} for assigned_at {event['disposition']['assigned_at']}",
    )
    trace.state.update(E_assigned=event["disposition"]["assigned_at"])
    return f"{event_id}: 200 DISPOSITIONED OTHER_SERIOUS; deadline_utc = assigned_at + 15 d"


def s6(trace: Trace) -> str:
    event_id = trace.state["E"]
    revised_from = trace.advance(DAY)
    reviser = "reviewer2" if "reviewer2" in trace.keys else "reviewer"
    reviser_id = "rev-2" if reviser == "reviewer2" else "rev-1"
    event = trace.dispose(event_id, "DEATH", role=reviser).json()
    check(event["disposition"]["tier"] == "DEATH", f"tier {event['disposition']}")
    check(
        event["disposition"]["assigned_at"] == trace.state["E_assigned"],
        f"assigned_at moved to {event['disposition']['assigned_at']}",
    )
    first = parse_time(trace.state["E_assigned"])
    check(
        parse_time(event["deadline_utc"]) == first + timedelta(days=10),
        f"deadline {event['deadline_utc']}",
    )
    [revision] = event["revisions"]
    check(
        (revision["prior_tier"], revision["new_tier"], revision["reviewer_id"])
        == ("OTHER_SERIOUS", "DEATH", reviser_id),
        f"revision {revision}",
    )
    trace.state.update(E_deadline=event["deadline_utc"], E_revised_from=revised_from)
    return f"{event_id}: 200; DEATH by {reviser_id}; deadline = first assigned_at + 10 d"


def s7(trace: Trace) -> str:
    event_id = trace.state["E"]
    trace.advance(HOUR)
    report, as_json = trace.fetch_report(event_id)
    check(report["report_kind"] == "SERIOUS_INCIDENT", f"kind {report['report_kind']}")
    check(report["report_version"] == 1, f"version {report['report_version']}")
    check(report["submission_marking"] is None, f"marking {report['submission_marking']}")
    determination = report["determination"]
    check(determination["tier"] == "DEATH", f"tier {determination['tier']}")
    check(determination["deadline_utc"] == trace.state["E_deadline"], f"deadline {determination}")
    stamps = report["timestamps"]
    check(stamps["detected_at"] == trace.state["E_detected"], f"detected_at {stamps}")
    check(stamps["determined_at"] == trace.state["E_assigned"], f"determined_at {stamps}")
    revised = parse_time(stamps["last_revised_at"])
    check(
        trace.state["E_revised_from"]
        <= revised
        < trace.state["E_revised_from"] + timedelta(minutes=1),
        f"last_revised_at {stamps['last_revised_at']}",
    )
    check(
        parse_time(stamps["assembled_at"]) - revised >= timedelta(hours=1),
        f"assembled_at {stamps['assembled_at']}",
    )
    check(report["regulation"]["reporting_window_days"] == 10, f"regulation {report['regulation']}")
    trace.state.update(E_report=report["report_id"], E_sha=as_json.headers["X-Report-Sha256"])
    return (
        f"{event_id}: 200 report {report['report_id']} v1 (schema-valid, DEATH, "
        "detected < determined < revised < assembled); PDF SHA-256 = X-Report-Sha256"
    )


def s8(trace: Trace) -> str:
    event_id = trace.state["E"]
    body = trace.call(
        "POST",
        f"/v1/events/{event_id}/report/submission",
        "reviewer",
        201,
        {
            "report_id": trace.state["E_report"],
            "json_sha256": trace.state["E_sha"],
            "report_type": "Initial",
        },
    ).json()
    check(body["event_id"] == event_id, f"submission event {body['event_id']}")
    check(body["report_id"] == trace.state["E_report"], f"submission report {body['report_id']}")
    check(body["submission_kind"] == "INITIAL", f"kind {body['submission_kind']}")
    check(body["submitted_by"] == "rev-1", f"submitted_by {body['submitted_by']}")
    return f"{event_id}: 201 {body['submission_id']} INITIAL by rev-1"


def s9(trace: Trace) -> str:
    event_id = trace.state["E"]
    reports = trace.call("GET", f"/v1/events/{event_id}/reports", "viewer", 200).json()
    check(
        [(r["report_id"], r["status"]) for r in reports]
        == [(trace.state["E_report"], "SUBMITTED")],
        f"reports {reports}",
    )
    actions = trace.audit(event_id)
    check(first_seen(actions, TRAIL_S) == list(TRAIL_S), f"trail order {actions}")
    for once in ("DISPOSITION_REVISED", "REPORT_ASSEMBLED", "REPORT_SUBMITTED"):
        check(actions.count(once) == 1, f"{once} x{actions.count(once)} in {actions}")
    entries = trace.verify_both()
    return (
        f"{event_id}: SUBMITTED; trail complete;"
        f" server and offline verifiers valid ({entries} entries)"
    )


# Path N: not serious ------------------------------------------------------------------------


def n0(trace: Trace) -> str:
    return trace.reset_and_register()


def n1(trace: Trace) -> str:
    return trace.breach(
        "F", {"metric_name": "accuracy_drift", "metric_type": "drift", "value": 0.34}
    )


def n2(trace: Trace) -> str:
    event_id = trace.state["F"]
    state, _ = trace.open_event(event_id)
    check(state == "FLAGGED", f"viewer saw {state}")
    return f"{event_id}: reviewer opens it: UNDER_REVIEW"


def n3(trace: Trace) -> str:
    event_id = trace.state["F"]
    response = trace.dispose(event_id, "NOT_SERIOUS", rationale="", status=400)
    check(response.json() == RATIONALE_REQUIRED, f"400 body {response.json()}")
    event = trace.event(event_id)
    check(
        event["state"] == "UNDER_REVIEW" and event["disposition"] is None,
        f"after the 400: {event['state']} {event['disposition']}",
    )
    return f"{event_id}: 400 RATIONALE_REQUIRED; still UNDER_REVIEW, no disposition"


def n4(trace: Trace) -> str:
    event_id = trace.state["F"]
    event = trace.dispose(event_id, "NOT_SERIOUS", rationale=RATIONALE).json()
    check(event["state"] == "CLOSED", f"state {event['state']}")
    check(event["deadline_utc"] is None, f"deadline {event['deadline_utc']}")
    trace.advance(16 * DAY)
    trace.receiver.pump()
    reminders = trace.received("reminder", event_id)
    check(reminders == [], f"reminders for {event_id}: {reminders}")
    check("REMINDER_TRIGGERED" not in trace.audit(event_id), "a reminder was triggered")
    return f"{event_id}: 200 CLOSED, deadline_utc null; 16 days later no reminder"


def n5(trace: Trace) -> str:
    event_id = trace.state["F"]
    report, as_json = trace.fetch_report(event_id)
    check(report["report_kind"] == "NOT_SERIOUS_CLOSURE", f"kind {report['report_kind']}")
    check(report["determination"]["tier"] == "NOT_SERIOUS", f"tier {report['determination']}")
    check(report["determination"]["deadline_utc"] is None, f"deadline {report['determination']}")
    check(report["determination"]["rationale"] == RATIONALE, f"rationale {report['determination']}")
    check(report["submission_marking"] == "NOT FOR SUBMISSION", f"marking {report}")
    trace.state.update(F_report=report["report_id"], F_sha=as_json.headers["X-Report-Sha256"])
    return (
        f"{event_id}: 200 closure report {report['report_id']}"
        " marked NOT FOR SUBMISSION, and its PDF"
    )


def n6(trace: Trace) -> str:
    event_id = trace.state["F"]
    response = trace.call(
        "POST",
        f"/v1/events/{event_id}/report/submission",
        "reviewer",
        409,
        {
            "report_id": trace.state["F_report"],
            "json_sha256": trace.state["F_sha"],
            "report_type": "Initial",
        },
    )
    check(response.json() == NOT_SUBMITTABLE, f"409 body {response.json()}")
    return f"{event_id}: 409 NOT_SUBMITTABLE"


def n7(trace: Trace) -> str:
    event_id = trace.state["F"]
    actions = trace.audit(event_id)
    check(first_seen(actions, TRAIL_N) == list(TRAIL_N), f"trail order {actions}")
    for absent in ("DISPOSITION_REVISED", "REPORT_SUBMITTED", "REMINDER_TRIGGERED"):
        check(absent not in actions, f"{absent} in {actions}")
    entries = trace.verify_both()
    return (
        f"{event_id}: trail complete, no submission;"
        f" server and offline verifiers valid ({entries} entries)"
    )


Step = tuple[str, str, Callable[[Trace], str]]

PATH_S: list[Step] = [
    ("S1", "reset and webhooks", s1),
    ("S2", "breach on sys-42", s2),
    ("S3", "report before any disposition", s3),
    ("S4", "viewer, then reviewer, opens the event", s4),
    ("S5", "OTHER_SERIOUS after 4 h", s5),
    ("S6", "revise to DEATH after 1 day", s6),
    ("S7", "report and PDF after 1 hour", s7),
    ("S8", "submission", s8),
    ("S9", "listing, trail and verify", s9),
]
PATH_N: list[Step] = [
    ("N0", "reset and webhooks", n0),
    ("N1", "breach on sys-42", n1),
    ("N2", "reviewer opens the event", n2),
    ("N3", "NOT_SERIOUS without a rationale", n3),
    ("N4", "NOT_SERIOUS with a rationale, then 16 days", n4),
    ("N5", "closure report and PDF", n5),
    ("N6", "closure report submission", n6),
    ("N7", "trail and verify", n7),
]
PATHS = {"S": PATH_S, "N": PATH_N}


def run_path(trace: Trace, steps: list[Step]) -> bool:
    """Run the steps in order; print each; stop at the first failure. True if all passed."""
    for label, title, step in steps:
        try:
            detail = step(trace)
        except (Failed, KeyError, TypeError, ValueError, httpx.HTTPError) as error:
            trace.out(f"{label} FAILED: {title}: {type(error).__name__}: {error}")
            return False
        trace.out(f"{label} ok: {title}: {detail}")
    return True


def http_receiver(client: httpx.Client, url: str, read_url: str) -> Receiver:
    def bodies() -> list[Any]:
        response = client.get(read_url)
        response.raise_for_status()
        return [item.get("body") for item in response.json()]

    def clear() -> None:
        client.delete(read_url).raise_for_status()

    return Receiver(url=url, bodies=bodies, clear=clear, pump=lambda: time.sleep(0.5))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--path", choices=sorted(PATHS), action="append")
    args = parser.parse_args(argv)
    base = os.environ.get("BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    keys = {
        "system": os.environ.get("INGEST_KEY")
        or os.environ.get("INGEST_KEY_SYS_42", DEFAULT_KEYS["system"]),
        "reviewer": os.environ.get("REVIEWER_KEY", DEFAULT_KEYS["reviewer"]),
        "viewer": os.environ.get("VIEWER_KEY", DEFAULT_KEYS["viewer"]),
    }
    if os.environ.get("REVIEWER_2_KEY"):
        keys["reviewer2"] = os.environ["REVIEWER_2_KEY"]
    url = os.environ.get("WEBHOOK_SINK_URL", DEFAULT_SINK).rstrip("/")
    read_url = (os.environ.get("WEBHOOK_SINK_READ_URL") or url).rstrip("/")
    with httpx.Client(base_url=base, timeout=60.0) as client:
        trace = Trace(client=client, keys=keys, receiver=http_receiver(client, url, read_url))
        for name in args.path or ["S", "N"]:
            print(f"path {name}")
            if not run_path(trace, PATHS[name]):
                print("demo FAILED")
                return 1
    print("demo ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
