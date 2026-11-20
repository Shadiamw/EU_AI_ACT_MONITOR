"""Model coverage for the cross-team testing interface (Phase 5 and 6 addition).

When the tester and the target belong to the same team, the suite report carries
coverage.model: covered = the design requirements the suite evaluated, total = the design
requirements the team lists for the milestone. Our list is the scope table of the Phase 5
Implementation Specification, section 1.1 (one row per requirement, DR-01 to DR-26), and the
mapping from each requirement to its tests is docs/models/phase5_design_requirements.md.

The probes M-01 to M-12 run only against our own system: they call Phase 5 endpoints that
another team's system does not have, and they reset and move the test clock.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

DESIGN_VERSION = "Phase 5 Implementation Specification (Pod H), approved 2 Nov 2026, section 1.1"
MAPPING_URL = (
    "https://github.com/Shadiamw/EU_AI_ACT_MONITOR/blob/main/docs/models/"
    "phase5_design_requirements.md"
)

# Spec 1.1, in table order.
DESIGN_REQUIREMENTS: tuple[tuple[str, str], ...] = (
    ("DR-01", "REQ-12 report assembly"),
    ("DR-02", "JSON and PDF, hashed and stored as produced"),
    ("DR-03", "Mark a report as submitted"),
    ("DR-04", "AC-13 schema check"),
    ("DR-05", "AC-13 usability review (manual)"),
    ("DR-06", "AC-14 report half"),
    ("DR-07", "Phase 4 report guard (409 then 501) replaced"),
    ("DR-08", "REQ-05 retention half; AC-05"),
    ("DR-09", "REQ-16; AC-18"),
    ("DR-10", "REQ-06 hardening: nightly verification"),
    ("DR-11", "REQ-06 hardening: chain head outside the database"),
    ("DR-12", "Event-table edits not covered by the chain"),
    ("DR-13", "Health dashboard; operations note"),
    ("DR-14", "Deployed-version traceability in /healthz"),
    ("DR-15", "AC-16 scripted trace, both paths"),
    ("DR-16", "AC-02 real delivery (manual)"),
    ("DR-17", "AC-09 screen review (manual)"),
    ("DR-18", "AC-07 manual review of the confirmation record"),
    ("DR-19", "AC-18 manual chain-integrity review"),
    ("DR-20", "Model 1 REPORT_READY, SUBMITTED"),
    ("DR-21", "Model 2 report, record_key"),
    ("DR-22", "Model 5 Ex-5 (nightly verifier)"),
    ("DR-23", "INV-1, 7, 9, 10, 11"),
    ("DR-24", "LIM-01 to LIM-15"),
    ("DR-25", "Contract delta"),
    ("DR-26", "All earlier regression tests"),
)

# Probe -> the design requirements it evaluates. Plan order is execution order.
MODEL_TESTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("M-01", ("DR-25",)),
    ("M-02", ("DR-07",)),
    ("M-03", ("DR-01", "DR-04")),
    ("M-04", ("DR-02",)),
    ("M-05", ("DR-06",)),
    ("M-06", ("DR-03", "DR-20")),
    ("M-07", ("DR-03",)),
    ("M-08", ("DR-08",)),
    ("M-09", ("DR-09",)),
    ("M-10", ("DR-12",)),
    ("M-11", ("DR-13",)),
    ("M-12", ("DR-14",)),
)

# docs/contract/phase5/report.schema.json, top-level "required" (Δ1).
REPORT_KEYS = (
    "report_id",
    "report_version",
    "report_kind",
    "schema_version",
    "event",
    "harm_fields",
    "determination",
    "timestamps",
    "risk_indicator",
    "regulation",
    "submission_marking",
    "article73_template",
    "ledger_anchor",
)
DELTA_ROUTES = (
    ("GET", "/v1/events/evt-unknown/report"),
    ("POST", "/v1/events/evt-unknown/report/submission"),
    ("GET", "/v1/events/evt-unknown/reports"),
    ("GET", "/v1/reports/rpt-unknown"),
    ("GET", "/v1/ledger/verifications"),
    ("POST", "/v1/ledger/verifications"),
    ("POST", "/v1/events/evt-unknown/erasure"),
    ("GET", "/v1/ops/dashboard"),
)
HARM_KEYS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)
# Past retain_until = max(6 calendar months, 183 days) from detection (at most 184 days).
PAST_RETENTION_SECONDS = 186 * 86400


def model_coverage(results: list[dict[str, Any]]) -> dict[str, Any]:
    """covered = design requirements with at least one probe that passed or failed (an error
    or skip did not evaluate it); total = every listed requirement."""
    probes = dict(MODEL_TESTS)
    evaluated: set[str] = set()
    for row in results:
        if row.get("outcome") in ("pass", "fail"):
            evaluated.update(probes.get(row.get("test_id", ""), ()))
    return {
        "covered": len(evaluated),
        "total": len(DESIGN_REQUIREMENTS),
        "basis": (
            f"Design requirements of the {DESIGN_VERSION} (DR-01 to DR-26) evaluated by "
            f"probes M-01 to M-12; manual, in-process-only and CLI-only requirements are "
            f"listed but not evaluated here. Requirement-to-test mapping: {MAPPING_URL}"
        ),
    }


@dataclass
class Response:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body)
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


def fetch(
    method: str, url: str, payload: Any = None, key: str = "", timeout: float = 20.0
) -> Response:
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    data = None
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return Response(
                resp.status, {k.lower(): v for k, v in resp.headers.items()}, resp.read()
            )
    except urllib.error.HTTPError as exc:
        return Response(exc.code, {k.lower(): v for k, v in exc.headers.items()}, exc.read())
    except Exception as exc:  # noqa: BLE001
        return Response(0, {}, f"{type(exc).__name__}: {exc}".encode())


class ProbeFailed(Exception):
    """The response was evaluated and differs from the expectation."""


class NoResponse(Exception):
    """No response to evaluate (communication)."""


@dataclass
class Probes:
    project: str
    keys: dict[str, str]
    state: dict[str, Any] = field(default_factory=dict)

    def call(
        self, method: str, path: str, role: str = "", payload: Any = None, expect: int = 200
    ) -> Response:
        response = fetch(method, self.project + path, payload, self.keys.get(role, ""))
        if response.status == 0:
            raise NoResponse(f"{method} {path}: {response.body.decode(errors='replace')}")
        if response.status != expect:
            raise ProbeFailed(
                f"{method} {path} as {role or 'no key'}: expected {expect}, got "
                f"{response.status} {response.body[:200].decode(errors='replace')}"
            )
        return response

    def now(self) -> str:
        body = self.call("POST", "/v1/test/clock/advance", "reviewer", {"seconds": 0}).json()
        return str(body["simulated_time"])[:19] + "Z"

    def breach(self, metric: str, metric_type: str, value: float) -> str:
        sample = {
            "system_id": "sys-42",
            "metric_name": metric,
            "metric_type": metric_type,
            "value": value,
            "timestamp": self.now(),
        }
        body = self.call("POST", "/v1/metrics", "ingest", sample, expect=201).json()
        event_id = body.get("flagged_event_id") if isinstance(body, dict) else None
        if not isinstance(event_id, str):
            raise ProbeFailed(f"breach did not flag an event: {body}")
        self.call("GET", f"/v1/events/{event_id}", "reviewer")
        return event_id

    def dispose(self, event_id: str, tier: str, rationale: str | None = None) -> dict[str, Any]:
        fields = {key: {"status": "NOT_APPLICABLE", "evidence": None} for key in HARM_KEYS}
        if tier != "NOT_SERIOUS":
            fields["fundamental_rights_infringement"] = {"status": "CONFIRMED", "evidence": None}
        body: dict[str, Any] = self.call(
            "POST",
            f"/v1/events/{event_id}/disposition",
            "reviewer",
            {"tier": tier, "rationale": rationale, "harm_fields": fields},
        ).json()
        return body

    # M-01 .. M-12 -----------------------------------------------------------------------
    def m01_delta_routes(self) -> str:
        for method, path in DELTA_ROUTES:
            self.call(method, path, expect=401)
        return f"All {len(DELTA_ROUTES)} contract-delta routes answer 401 without a key."

    def m02_report_guard(self) -> str:
        event_id = self.breach("accuracy", "performance", 0.61)
        self.state["serious"] = event_id
        body = self.call("GET", f"/v1/events/{event_id}/report", "reviewer", expect=409).json()
        if not isinstance(body, dict) or body.get("error_code") != "NO_DISPOSITION":
            raise ProbeFailed(f"expected NO_DISPOSITION, got {body}")
        return "Report before a disposition is 409 NO_DISPOSITION."

    def m03_report_assembly(self) -> str:
        event_id = self.state["serious"]
        self.call("POST", "/v1/test/clock/advance", "reviewer", {"seconds": 4 * 3600})
        self.dispose(event_id, "OTHER_SERIOUS")
        self.call("POST", "/v1/test/clock/advance", "reviewer", {"seconds": 3600})
        response = self.call("GET", f"/v1/events/{event_id}/report", "reviewer")
        report = response.json()
        if not isinstance(report, dict):
            raise ProbeFailed("report body is not a JSON object")
        missing = [key for key in REPORT_KEYS if key not in report]
        if missing:
            raise ProbeFailed(f"report lacks required keys {missing}")
        if report["schema_version"] != "pod-h-report-1":
            raise ProbeFailed(f"schema_version {report['schema_version']}")
        stamps = report["timestamps"]
        if not stamps["detected_at"] < stamps["determined_at"] < stamps["assembled_at"]:
            raise ProbeFailed(f"timestamps not ordered: {stamps}")
        self.state.update(report=report["report_id"], sha=response.headers.get("x-report-sha256"))
        return "Report 200 after a disposition, with every required key and ordered timestamps."

    def m04_hashes(self) -> str:
        event_id = self.state["serious"]
        for query in ("", "?format=pdf"):
            first = self.call("GET", f"/v1/events/{event_id}/report{query}", "reviewer")
            digest = hashlib.sha256(first.body).hexdigest()
            if first.headers.get("x-report-sha256") != digest:
                raise ProbeFailed(f"{query or 'json'}: X-Report-Sha256 does not match the body")
            again = self.call("GET", f"/v1/events/{event_id}/report{query}", "reviewer")
            if again.body != first.body:
                raise ProbeFailed(f"{query or 'json'}: a second download differs")
        return "JSON and PDF match X-Report-Sha256 and are served identically twice."

    def m05_report_roles(self) -> str:
        event_id = self.state["serious"]
        for role in ("viewer", "ingest"):
            self.call("GET", f"/v1/events/{event_id}/report", role, expect=403)
        return "Viewer and system keys get 403 on the report."

    def m06_submission(self) -> str:
        event_id = self.state["serious"]
        body = self.call(
            "POST",
            f"/v1/events/{event_id}/report/submission",
            "reviewer",
            {
                "report_id": self.state["report"],
                "json_sha256": self.state["sha"],
                "report_type": "Initial",
            },
            expect=201,
        ).json()
        if body.get("submission_kind") != "INITIAL":
            raise ProbeFailed(f"submission_kind {body.get('submission_kind')}")
        listing = self.call("GET", f"/v1/events/{event_id}/reports", "viewer").json()
        statuses = [row.get("status") for row in listing]
        if statuses != ["SUBMITTED"]:
            raise ProbeFailed(f"report statuses {statuses}")
        return "Submission 201 INITIAL; the report is listed as SUBMITTED."

    def m07_closure(self) -> str:
        event_id = self.breach("accuracy_drift", "drift", 0.34)
        self.dispose(event_id, "NOT_SERIOUS", "seasonal shift, no user impact")
        response = self.call("GET", f"/v1/events/{event_id}/report", "reviewer")
        report = response.json()
        if report.get("report_kind") != "NOT_SERIOUS_CLOSURE":
            raise ProbeFailed(f"report_kind {report.get('report_kind')}")
        body = self.call(
            "POST",
            f"/v1/events/{event_id}/report/submission",
            "reviewer",
            {
                "report_id": report["report_id"],
                "json_sha256": response.headers.get("x-report-sha256"),
                "report_type": "Initial",
            },
            expect=409,
        ).json()
        if body.get("error_code") != "NOT_SUBMITTABLE":
            raise ProbeFailed(f"expected NOT_SUBMITTABLE, got {body}")
        return "Not-serious closure report 200; its submission is 409 NOT_SUBMITTABLE."

    def m08_retention(self) -> str:
        event_id = self.state["serious"]
        body = self.call("POST", f"/v1/events/{event_id}/erasure", "reviewer", expect=409).json()
        if body.get("error_code") != "RETENTION_NOT_EXPIRED":
            raise ProbeFailed(f"expected RETENTION_NOT_EXPIRED, got {body}")
        return "Erasure before retain_until is 409 RETENTION_NOT_EXPIRED."

    def m09_erasure(self) -> str:
        event_id = self.state["serious"]
        self.call("POST", "/v1/test/clock/advance", "reviewer", {"seconds": PAST_RETENTION_SECONDS})
        self.call("POST", f"/v1/events/{event_id}/erasure", "reviewer")
        self.call("GET", f"/v1/events/{event_id}/report", "reviewer", expect=410)
        event = self.call("GET", f"/v1/events/{event_id}", "viewer").json()
        if any(event["harm_fields"][key]["evidence"] is not None for key in HARM_KEYS):
            raise ProbeFailed("evidence still readable after erasure")
        verify = self.call("GET", "/v1/ledger/verify", "viewer").json()
        if verify.get("valid") is not True:
            raise ProbeFailed(f"ledger verify after erasure: {verify}")
        return "After retain_until: erasure 200, report 410, evidence null, ledger still valid."

    def m10_verification_run(self) -> str:
        run = self.call("POST", "/v1/ledger/verifications", "reviewer", expect=201).json()
        if run.get("valid") is not True or run.get("projection_mismatches") != []:
            raise ProbeFailed(f"verification run {run}")
        return "Manual verification run 201: chain valid, no event-table projection mismatch."

    def m11_dashboard(self) -> str:
        body = self.call("GET", "/v1/ops/dashboard", "viewer").json()
        missing = [key for key in ("version", "worker", "ledger") if key not in body]
        if missing:
            raise ProbeFailed(f"dashboard lacks {missing}")
        return "Ops dashboard 200 with version, worker and ledger sections."

    def m12_healthz_version(self) -> str:
        response = fetch("GET", self.project + "/healthz")
        if response.status == 0:
            raise NoResponse(response.body.decode(errors="replace"))
        body = response.json() or {}
        missing = [k for k in ("version", "last_verification", "reset_count") if k not in body]
        if missing:
            raise ProbeFailed(f"/healthz ({response.status}) lacks {missing}")
        return f"/healthz reports version {body['version']!r} and the verification fields."


def run_probes(project: str, keys: dict[str, str]) -> list[dict[str, Any]]:
    """Run M-01..M-12 in order. A probe whose setup depends on an earlier failed probe is an
    error with source tester (it could not be evaluated)."""
    probes = Probes(project=project, keys=keys)
    steps: dict[str, Callable[[], str]] = {
        "M-01": probes.m01_delta_routes,
        "M-02": probes.m02_report_guard,
        "M-03": probes.m03_report_assembly,
        "M-04": probes.m04_hashes,
        "M-05": probes.m05_report_roles,
        "M-06": probes.m06_submission,
        "M-07": probes.m07_closure,
        "M-08": probes.m08_retention,
        "M-09": probes.m09_erasure,
        "M-10": probes.m10_verification_run,
        "M-11": probes.m11_dashboard,
        "M-12": probes.m12_healthz_version,
    }
    results: list[dict[str, Any]] = []
    for test_id, _ in MODEL_TESTS:
        try:
            results.append({"test_id": test_id, "outcome": "pass", "message": steps[test_id]()})
        except ProbeFailed as exc:
            results.append({"test_id": test_id, "outcome": "fail", "message": str(exc)})
        except NoResponse as exc:
            results.append(
                {
                    "test_id": test_id,
                    "outcome": "error",
                    "source": "communication",
                    "message": str(exc),
                }
            )
        except (KeyError, TypeError, AttributeError) as exc:
            results.append(
                {
                    "test_id": test_id,
                    "outcome": "error",
                    "source": "tester",
                    "message": f"Could not evaluate: {type(exc).__name__}: {exc}",
                }
            )
    return results
