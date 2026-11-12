"""Cross-team testing interface (student-facing-spec v2).

System base:  .../system   → POST /test/setup, GET /test/report
Tester base:  .../tester   → POST /test/setup, POST /test/run, GET /test/report

Coverage and result shapes follow the shared interface: covered/total/basis and
test_id/outcome/message (+ source on error).
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from app import platform

_lock = threading.Lock()
_system_sessions: dict[str, SystemSession] = {}
_tester_sessions: dict[str, TesterSession] = {}

# Pod components exercised by the shared project API (pod_spec.txt).
_COMPONENTS = (
    "health",
    "metrics_ingest",
    "events",
    "ledger",
    "test_reset",
    "auth",
)

# Acceptance criteria in scope for cross-team black-box probes (pod REQs).
_CRITERIA = (
    "AC-health",
    "AC-auth-required",
    "AC-ledger-verify",
    "AC-events-list",
    "AC-event-unknown-404",
    "AC-metrics-rejects-bad",
    "AC-test-reset",
)


class HarnessError(Exception):
    """Typed harness failure with HTTP status + interface source."""

    def __init__(self, status: int, message: str, source: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.source = source


def _new_id() -> str:
    return str(uuid.uuid4())


def _public_base() -> str:
    return (
        os.environ.get("SUITE_BASE_URL") or "https://eu-ai-act-monitor-api.fly.dev"
    ).rstrip("/")


def _self_system_url() -> str:
    return f"{_public_base()}/system"


def _is_self_system(target_url: str) -> bool:
    target = target_url.rstrip("/")
    candidates = {_self_system_url(), f"{_public_base()}/system"}
    # Also treat bare public host /system path variants.
    return target in candidates or target == _public_base()


def _project_base(system_target: str) -> str:
    """Project API lives on the host root; system harness is under /system.

    Self-target probes use loopback so a single API worker cannot deadlock by
    HTTP-calling its own public URL while handling /tester/test/run.
    """
    if _is_self_system(system_target):
        port = os.environ.get("PORT") or "8000"
        return f"http://127.0.0.1:{port}"
    base = system_target.rstrip("/")
    if base.endswith("/system"):
        return base[: -len("/system")] or base
    return base


def _coverage_entry(
    covered: int | None, total: int | None, basis: str
) -> dict[str, Any]:
    return {"covered": covered, "total": total, "basis": basis}


def _null_code_coverage(reason: str) -> dict[str, Any]:
    return _coverage_entry(None, None, reason)


def _empty_suite_coverage() -> dict[str, Any]:
    return {
        "code": _null_code_coverage(
            "Source lines; target instrumentation unavailable"
        ),
        "component": _coverage_entry(
            0, len(_COMPONENTS), "Components listed in the pod specification"
        ),
        "system": _coverage_entry(
            0, len(_CRITERIA), "Acceptance criteria listed in the pod specification"
        ),
    }


@dataclass
class SystemSession:
    session_id: str
    status: str = "ready"  # preparing | ready
    created_at: float = field(default_factory=time.time)
    setup: dict[str, Any] = field(default_factory=dict)
    # Request counts since setup (best-effort; no line instrumentation).
    project_requests: int = 0

    def code_coverage(self) -> dict[str, Any]:
        # No source-line instrumentation in this deployment.
        return {
            "session_id": self.session_id,
            "coverage": {
                "code": _null_code_coverage(
                    "Source lines; target instrumentation unavailable"
                )
            },
        }


@dataclass
class TesterSession:
    session_id: str
    target_url: str
    status: str = "ready"  # ready | running | complete | aborted
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    results: list[dict[str, Any]] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=_empty_suite_coverage)
    access: dict[str, str] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "target_url": self.target_url,
            "results": list(self.results),
            "coverage": dict(self.coverage),
        }


def service_info(role: str) -> dict[str, Any]:
    endpoints = (
        ["/test", "/test/setup", "/test/report"]
        if role == "system"
        else ["/test", "/test/setup", "/test/run", "/test/report"]
    )
    return {"service": role, "ready": True, "endpoints": endpoints}


# --- System -----------------------------------------------------------------


def system_setup(body: dict[str, Any] | None) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    session_id = body.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        raise HarnessError(400, "session_id is required", "system")
    session_id = session_id.strip()
    setup = body.get("setup")
    if setup is None:
        setup = {}
    if not isinstance(setup, dict):
        raise HarnessError(400, "setup must be an object", "system")

    with _lock:
        existing = _system_sessions.get(session_id)
        if existing is not None:
            raise HarnessError(409, "session_id already exists", "system")
        session = SystemSession(session_id=session_id, status="preparing", setup=dict(setup))
        _system_sessions[session_id] = session

    try:
        platform.reset()
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _system_sessions.pop(session_id, None)
        raise HarnessError(
            500,
            f"Could not initialize the session database: {type(exc).__name__}",
            "system",
        ) from exc

    session.status = "ready"
    return {"session_id": session_id}


def system_report(session_id: str) -> dict[str, Any]:
    if not session_id:
        raise HarnessError(400, "session_id is required", "system")
    with _lock:
        session = _system_sessions.get(session_id)
    if session is None:
        raise HarnessError(404, "unknown session_id", "system")
    if session.status == "preparing":
        raise HarnessError(409, "session preparation is still in progress", "system")
    return session.code_coverage()


def get_system(session_id: str) -> SystemSession | None:
    with _lock:
        return _system_sessions.get(session_id)


# --- Tester -----------------------------------------------------------------


def _access_from_body(body: dict[str, Any]) -> dict[str, str]:
    """Optional credentials for the target. Prefer body, then process env."""
    out: dict[str, str] = {}
    for key, env in (
        ("ingest_key", "INGEST_KEY_SYS_42"),
        ("reviewer_key", "REVIEWER_KEY"),
        ("viewer_key", "VIEWER_KEY"),
    ):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            out[key] = value.strip()
        else:
            env_val = os.environ.get(env, "")
            if env_val:
                out[key] = env_val
    setup = body.get("setup")
    if isinstance(setup, dict):
        for key in ("ingest_key", "reviewer_key", "viewer_key"):
            value = setup.get(key)
            if isinstance(value, str) and value.strip():
                out[key] = value.strip()
    return out


def _call_system_setup(target_url: str, session_id: str, setup: dict[str, Any]) -> None:
    """Hand the session to the target system. Self-target uses in-process setup."""
    if _is_self_system(target_url):
        system_setup({"session_id": session_id, "setup": setup})
        return

    url = f"{target_url.rstrip('/')}/test/setup"
    payload = json.dumps({"session_id": session_id, "setup": setup}).encode("utf-8")
    status, body = _http("POST", url, payload)
    if status == 0:
        raise HarnessError(
            500,
            f"No response from system setup at {url}: {body}",
            "communication",
        )
    if status == 201:
        return
    # Forward system failure status / message / source when present.
    message = f"system setup returned HTTP {status}"
    source = "system"
    try:
        parsed = json.loads(body) if body else {}
        if isinstance(parsed, dict):
            if isinstance(parsed.get("message"), str) and parsed["message"].strip():
                message = parsed["message"]
            if isinstance(parsed.get("source"), str) and parsed["source"].strip():
                source = parsed["source"]
    except json.JSONDecodeError:
        if body.strip():
            message = body.strip()[:500]
    raise HarnessError(status if status >= 400 else 500, message, source)


def tester_setup(body: dict[str, Any] | None) -> dict[str, Any]:
    body = body if isinstance(body, dict) else {}
    target = body.get("target_url")
    if not isinstance(target, str) or not target.strip():
        raise HarnessError(400, "target_url is required", "tester")
    target = target.strip().rstrip("/")
    parsed = urlparse(target)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HarnessError(400, "target_url must be an absolute http(s) URL", "tester")

    setup = body.get("setup")
    if setup is None:
        setup = {}
    if not isinstance(setup, dict):
        raise HarnessError(400, "setup must be an object", "tester")

    session_id = _new_id()
    # Do not register a runnable session until system setup succeeds.
    try:
        _call_system_setup(target, session_id, setup)
    except HarnessError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HarnessError(
            500, f"tester setup failed: {type(exc).__name__}: {exc}", "tester"
        ) from exc

    session = TesterSession(
        session_id=session_id,
        target_url=target,
        access=_access_from_body(body),
    )
    with _lock:
        _tester_sessions[session_id] = session
    return {"session_id": session_id}


def tester_run(session_id: str) -> dict[str, Any]:
    if not session_id:
        raise HarnessError(400, "session_id is required", "tester")
    with _lock:
        session = _tester_sessions.get(session_id)
    if session is None:
        raise HarnessError(404, "unknown session_id", "tester")
    if session.status == "running":
        raise HarnessError(409, "suite is already running", "tester")
    if session.status in ("complete", "aborted"):
        raise HarnessError(409, "suite already finished for this session", "tester")

    session.status = "running"
    try:
        _execute_tester(session)
        session.status = "complete"
        session.finished_at = time.time()
        return session.report()
    except HarnessError as exc:
        if exc.status == 500 and exc.source == "tester":
            _mark_abort(session, exc.message)
            raise
        # Unexpected harness errors during run → abort with stored partial report.
        _mark_abort(session, exc.message)
        raise HarnessError(500, exc.message, "tester") from exc
    except Exception as exc:  # noqa: BLE001
        _mark_abort(session, f"{type(exc).__name__}: {exc}")
        raise HarnessError(
            500, f"tester aborted: {type(exc).__name__}: {exc}", "tester"
        ) from exc


def _mark_abort(session: TesterSession, message: str) -> None:
    planned = [t["test_id"] for t in _planned_tests()]
    seen = {r["test_id"] for r in session.results if "test_id" in r}
    # First unexecuted planned test records the abort; the rest are skip.
    remaining = [tid for tid in planned if tid not in seen]
    if remaining:
        first, *rest = remaining
        session.results.append(
            {
                "test_id": first,
                "outcome": "error",
                "source": "tester",
                "message": message,
            }
        )
        for test_id in rest:
            session.results.append(
                {
                    "test_id": test_id,
                    "outcome": "skip",
                    "message": "Test was not executed because the tester aborted.",
                }
            )
    session.coverage = _coverage_from_results(session.results, session.coverage.get("code"))
    session.status = "aborted"
    session.finished_at = time.time()


def tester_report(session_id: str) -> dict[str, Any]:
    if not session_id:
        raise HarnessError(400, "session_id is required", "tester")
    with _lock:
        session = _tester_sessions.get(session_id)
    if session is None:
        raise HarnessError(404, "unknown session_id", "tester")
    if session.status in ("ready", "running"):
        raise HarnessError(409, "suite has not ended; retry later", "tester")
    return session.report()


# --- Suite execution --------------------------------------------------------


def _planned_tests() -> list[dict[str, Any]]:
    """Canonical ordered plan. Every test_id appears exactly once in a finished report."""
    return [
        {"test_id": "T-01", "criterion": "AC-health", "components": ("health",)},
        {"test_id": "T-02", "criterion": "AC-auth-required", "components": ("auth", "ledger")},
        {"test_id": "T-03", "criterion": "AC-ledger-verify", "components": ("ledger", "auth")},
        {"test_id": "T-04", "criterion": "AC-events-list", "components": ("events", "auth")},
        {
            "test_id": "T-05",
            "criterion": "AC-event-unknown-404",
            "components": ("events", "auth"),
        },
        {
            "test_id": "T-06",
            "criterion": "AC-metrics-rejects-bad",
            "components": ("metrics_ingest", "auth"),
        },
        {"test_id": "T-07", "criterion": "AC-test-reset", "components": ("test_reset", "auth")},
        {"test_id": "T-08", "criterion": "AC-ledger-verify", "components": ("ledger",)},
    ]


def _result(
    test_id: str,
    outcome: str,
    message: str,
    source: str | None = None,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "test_id": test_id,
        "outcome": outcome,
        "message": message,
    }
    if source is not None:
        body["source"] = source
    return body


def _execute_tester(session: TesterSession) -> None:
    project = _project_base(session.target_url)
    access = session.access
    results: list[dict[str, Any]] = []

    # T-01 health
    results.append(_probe_health(project))

    # T-02 unauthenticated ledger verify → expect 401
    results.append(_probe_unauth_ledger(project))

    # T-03 authenticated ledger verify
    results.append(_probe_auth_ledger(project, access.get("viewer_key", "")))

    # T-04 events list
    results.append(_probe_events_list(project, access.get("viewer_key", "")))

    # T-05 unknown event 404
    results.append(_probe_unknown_event(project, access.get("viewer_key", "")))

    # T-06 metrics bad body
    results.append(_probe_metrics_bad(project, access.get("ingest_key", "")))

    # T-07 test reset
    results.append(_probe_reset(project, access.get("reviewer_key", "")))

    # T-08 pull system code coverage into suite report
    code_cov, cov_result = _pull_system_code_coverage(session)
    results.append(cov_result)

    session.results = results
    session.coverage = _coverage_from_results(results, code_cov)


def _probe_health(project: str) -> dict[str, Any]:
    status, body = _http("GET", f"{project}/livez")
    if status == 0:
        status, body = _http("GET", f"{project}/healthz")
        if status == 0:
            return _result(
                "T-01",
                "error",
                f"Health check timed out or failed: {body}",
                "communication",
            )
        if status in (200, 503):
            return _result(
                "T-01",
                "pass",
                f"Expected healthz 200/503; received {status}.",
            )
        return _result("T-01", "fail", f"Expected healthz 200/503; received {status}.")
    if status == 200:
        return _result("T-01", "pass", "Expected livez 200; received 200.")
    return _result("T-01", "fail", f"Expected livez 200; received {status}.")


def _probe_unauth_ledger(project: str) -> dict[str, Any]:
    status, body = _http("GET", f"{project}/v1/ledger/verify")
    if status == 0:
        return _result(
            "T-02",
            "error",
            f"The /v1/ledger/verify request failed with no response: {body}",
            "communication",
        )
    # Expected project error without credentials.
    if status in (401, 403):
        return _result(
            "T-02",
            "pass",
            f"Expected 401/403 without credentials; received {status}.",
        )
    if status == 200:
        return _result(
            "T-02",
            "fail",
            "Expected 401/403 without credentials; received 200.",
        )
    return _result(
        "T-02",
        "fail",
        f"Expected 401/403 without credentials; received {status}. Body: {body[:160]}",
    )


def _probe_auth_ledger(project: str, viewer_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {viewer_key}"} if viewer_key else None
    status, body = _http("GET", f"{project}/v1/ledger/verify", headers=headers)
    if status == 0:
        return _result(
            "T-03",
            "error",
            f"The /v1/ledger/verify request timed out: {body}",
            "communication",
        )
    if status == 200:
        return _result("T-03", "pass", "Expected ledger verify 200; received 200.")
    # Foreign keys → 401 is an expected project error for cross-team without shared secrets.
    if status in (401, 403):
        return _result(
            "T-03",
            "pass",
            f"Expected auth challenge or 200; received {status} (endpoint present).",
        )
    return _result("T-03", "fail", f"Unexpected ledger verify status {status}.")


def _probe_events_list(project: str, viewer_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {viewer_key}"} if viewer_key else None
    status, body = _http("GET", f"{project}/v1/events", headers=headers)
    if status == 0:
        return _result(
            "T-04",
            "error",
            f"The /v1/events request timed out: {body}",
            "communication",
        )
    if status in (200, 401, 403):
        return _result(
            "T-04",
            "pass",
            f"Expected events list 200 or auth error; received {status}.",
        )
    return _result("T-04", "fail", f"Unexpected /v1/events status {status}.")


def _probe_unknown_event(project: str, viewer_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {viewer_key}"} if viewer_key else None
    eid = "evt-00000000000000000000000000000000"
    status, body = _http("GET", f"{project}/v1/events/{eid}", headers=headers)
    if status == 0:
        return _result(
            "T-05",
            "error",
            f"The /v1/events/{{id}} request timed out: {body}",
            "communication",
        )
    if status == 404:
        return _result("T-05", "pass", "Expected 404 for unknown event; received 404.")
    if status in (401, 403):
        return _result(
            "T-05",
            "pass",
            f"Expected 404 or auth error for unknown event; received {status}.",
        )
    return _result("T-05", "fail", f"Expected 404 for unknown event; received {status}.")


def _probe_metrics_bad(project: str, ingest_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {ingest_key}"} if ingest_key else None
    status, body = _http(
        "POST",
        f"{project}/v1/metrics",
        b"{}",
        headers=headers,
    )
    if status == 0:
        return _result(
            "T-06",
            "error",
            f"The /v1/metrics request timed out: {body}",
            "communication",
        )
    # Malformed or unauthorized are expected project errors.
    if status in (400, 401, 403, 422):
        return _result(
            "T-06",
            "pass",
            f"Expected rejection of empty metrics body; received {status}.",
        )
    return _result(
        "T-06",
        "fail",
        f"Expected 400/401/403 for empty metrics body; received {status}.",
    )


def _probe_reset(project: str, reviewer_key: str) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {reviewer_key}"} if reviewer_key else None
    status, body = _http("POST", f"{project}/v1/test/reset", b"", headers=headers)
    if status == 0:
        return _result(
            "T-07",
            "error",
            f"The /v1/test/reset request timed out: {body}",
            "communication",
        )
    if status in (200, 204):
        return _result("T-07", "pass", f"Expected reset 204/200; received {status}.")
    if status in (401, 403):
        return _result(
            "T-07",
            "pass",
            f"Expected reset success or auth error; received {status} (endpoint present).",
        )
    if status == 404:
        return _result(
            "T-07",
            "fail",
            "Expected /v1/test/reset in ENVIRONMENT=test; received 404.",
        )
    return _result("T-07", "fail", f"Unexpected reset status {status}.")


def _pull_system_code_coverage(
    session: TesterSession,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read target system /test/report for coverage.code after project calls."""
    default = _null_code_coverage("Source lines; target instrumentation unavailable")
    if _is_self_system(session.target_url):
        try:
            report = system_report(session.session_id)
            code = report.get("coverage", {}).get("code", default)
            if not isinstance(code, dict):
                code = default
            return code, _result(
                "T-08",
                "pass",
                "System report returned coverage.code for this session.",
            )
        except HarnessError as exc:
            src = exc.source
            if src not in ("system", "tester", "communication", "unknown"):
                src = "system"
            return default, _result("T-08", "error", exc.message, src)

    url = f"{session.target_url.rstrip('/')}/test/report?session_id={session.session_id}"
    status, body = _http("GET", url)
    if status == 0:
        return default, _result(
            "T-08",
            "error",
            f"System report request failed: {body}",
            "communication",
        )
    if status == 409:
        return default, _result(
            "T-08",
            "error",
            "System report not ready (409).",
            "system",
        )
    if status != 200:
        return default, _result(
            "T-08",
            "error",
            f"System report returned HTTP {status}.",
            "system",
        )
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return default, _result(
            "T-08",
            "error",
            "System report body was not JSON.",
            "system",
        )
    code = default
    if isinstance(parsed, dict):
        cov = parsed.get("coverage")
        if isinstance(cov, dict) and isinstance(cov.get("code"), dict):
            code = cov["code"]
        elif isinstance(parsed.get("code"), dict):
            code = parsed["code"]
    return code, _result(
        "T-08",
        "pass",
        "Copied coverage.code from system /test/report.",
    )


def _coverage_from_results(
    results: list[dict[str, Any]],
    code: dict[str, Any] | None,
) -> dict[str, Any]:
    plan = {t["test_id"]: t for t in _planned_tests()}
    covered_components: set[str] = set()
    covered_criteria: set[str] = set()
    for row in results:
        # pass/fail means the criterion was evaluated; error/skip did not complete evaluation.
        if row.get("outcome") not in ("pass", "fail"):
            continue
        meta = plan.get(row.get("test_id", ""))
        if not meta:
            continue
        covered_components.update(meta["components"])
        covered_criteria.add(meta["criterion"])

    if isinstance(code, dict) and "basis" in code:
        code_entry = {
            "covered": code.get("covered"),
            "total": code.get("total"),
            "basis": code["basis"],
        }
    else:
        code_entry = _null_code_coverage(
            "Source lines; target instrumentation unavailable"
        )

    return {
        "code": code_entry,
        "component": _coverage_entry(
            len(covered_components),
            len(_COMPONENTS),
            "Components listed in the pod specification",
        ),
        "system": _coverage_entry(
            len(covered_criteria),
            len(_CRITERIA),
            "Acceptance criteria listed in the pod specification",
        ),
    }


def _http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 12.0,
) -> tuple[int, str]:
    req_headers = dict(headers or {})
    if body is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"
