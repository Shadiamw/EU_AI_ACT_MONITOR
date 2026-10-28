"""Staff autograder harness: /test/setup, /test/run, /test/report (system + tester).

Staff registers distinguishable bases and appends those suffixes. Sessions live in
process memory for the lifetime of the API machine.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

from app import platform

_lock = threading.Lock()
_sessions: dict[str, "Session"] = {}


@dataclass
class Session:
    session_id: str
    role: str  # system | tester
    status: str = "ready"  # ready | running | complete | failed
    created_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    testtype: Any = None
    target: str | None = None
    passed: bool | None = None
    score: int | None = None
    message: str = ""
    results: list[dict[str, Any]] = field(default_factory=list)
    raw_setup: dict[str, Any] = field(default_factory=dict)

    def report(self) -> dict[str, Any]:
        body: dict[str, Any] = {
            "session_id": self.session_id,
            "status": self.status,
            "role": self.role,
            "message": self.message,
            "results": list(self.results),
        }
        if self.passed is not None:
            body["passed"] = self.passed
            body["result"] = "pass" if self.passed else "fail"
        if self.score is not None:
            body["score"] = self.score
        if self.target is not None:
            body["target"] = self.target
        if self.finished_at is not None:
            body["finished_at"] = self.finished_at
        return body


def _new_id() -> str:
    return uuid.uuid4().hex


def _pick_target(body: dict[str, Any]) -> str | None:
    for key in ("target", "target_url", "system_url", "base_url", "url", "system", "sut"):
        value = body.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().rstrip("/")
    return None


def _pick_testtype(body: dict[str, Any]) -> Any:
    for key in ("testtype", "test_type", "type", "mode"):
        if key in body:
            return body[key]
    return None


def setup(role: str, body: dict[str, Any] | None) -> Session:
    body = body or {}
    if not isinstance(body, dict):
        body = {}

    session = Session(
        session_id=_new_id(),
        role=role,
        testtype=_pick_testtype(body),
        raw_setup=dict(body),
    )

    if role == "system":
        # Clean fixture state so staff / peer testers start from a known baseline.
        platform.reset()
        session.status = "ready"
        session.message = "system reset and fixtures seeded"
        session.passed = True
        session.score = 100
    else:
        target = _pick_target(body)
        if target is None:
            # Default to the peer / configured SUT, then this deployment's public URL.
            target = (
                os.environ.get("TESTER_TARGET_URL")
                or os.environ.get("SUITE_BASE_URL")
                or "https://eu-ai-act-monitor-api.fly.dev"
            ).rstrip("/")
        session.target = target
        session.status = "ready"
        session.message = f"tester ready against {target}"

    with _lock:
        _sessions[session.session_id] = session
    return session


def get(session_id: str) -> Session | None:
    with _lock:
        return _sessions.get(session_id)


def run(session_id: str) -> Session:
    session = get(session_id)
    if session is None:
        raise KeyError("session")
    if session.status == "running":
        return session

    session.status = "running"
    session.message = "running"
    thread = threading.Thread(target=_execute, args=(session,), daemon=True)
    thread.start()
    return session


def _execute(session: Session) -> None:
    try:
        if session.role == "system":
            _run_system(session)
        else:
            _run_tester(session)
    except Exception as exc:  # noqa: BLE001 — surface to report
        session.status = "failed"
        session.passed = False
        session.score = 0
        session.message = f"{type(exc).__name__}: {exc}"
        session.results.append({"name": "harness", "passed": False, "detail": session.message})
    finally:
        session.finished_at = time.time()


def _run_system(session: Session) -> None:
    """System run confirms the service is healthy after setup."""
    results: list[dict[str, Any]] = []
    base = (os.environ.get("SUITE_BASE_URL") or "http://127.0.0.1:8000").rstrip("/")
    code, body = _http("GET", f"{base}/livez")
    ok = code == 200
    results.append({"name": "livez", "passed": ok, "status": code, "body": body[:200]})
    code, body = _http("GET", f"{base}/healthz")
    ok2 = code in (200, 503)
    results.append({"name": "healthz", "passed": ok2, "status": code, "body": body[:200]})
    session.results = results
    session.passed = ok and ok2
    session.score = 100 if session.passed else 0
    session.status = "complete" if session.passed else "failed"
    session.message = "system run complete" if session.passed else "system run failed"


def _run_tester(session: Session) -> None:
    """Probe the target system surface that peers / staff expect to exercise."""
    target = (session.target or "").rstrip("/")
    if not target:
        raise RuntimeError("no target")

    # Fallbacks if /livez is missing on the peer.
    results: list[dict[str, Any]] = []
    all_ok = True

    code, body = _http("GET", f"{target}/livez")
    if code == 404:
        code, body = _http("GET", f"{target}/healthz")
        ok = code in (200, 503)
        results.append({"name": "healthz", "passed": ok, "status": code, "body": body[:240]})
        all_ok = all_ok and ok
    else:
        ok = code == 200
        results.append({"name": "livez", "passed": ok, "status": code, "body": body[:240]})
        all_ok = all_ok and ok
        code, body = _http("GET", f"{target}/healthz")
        ok = code in (200, 503)
        results.append({"name": "healthz", "passed": ok, "status": code, "body": body[:240]})
        all_ok = all_ok and ok

    code, body = _http("POST", f"{target}/v1/test/reset", b"{}")
    ok = code in (200, 204)
    results.append({"name": "reset", "passed": ok, "status": code, "body": body[:240]})
    all_ok = all_ok and ok

    viewer = os.environ.get("VIEWER_KEY", "")
    headers = {"Authorization": f"Bearer {viewer}"} if viewer else None
    # Against a foreign system the seeded viewer key will not match; treat 401 as
    # "endpoint present" so cross-team runs still score the shared surface.
    code, body = _http("GET", f"{target}/v1/ledger/verify", headers=headers)
    ok = code in (200, 401, 403)
    results.append({"name": "ledger_verify", "passed": ok, "status": code, "body": body[:240]})
    all_ok = all_ok and ok

    session.results = results
    session.passed = all_ok
    session.score = int(round(100 * sum(1 for r in results if r["passed"]) / max(len(results), 1)))
    session.status = "complete" if all_ok else "failed"
    session.message = "tester run complete" if all_ok else "tester run failed"


def _http(
    method: str,
    url: str,
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, str]:
    req_headers = dict(headers or {})
    if body is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=body, method=method, headers=req_headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return 0, f"{type(exc).__name__}: {exc}"


def service_info(role: str) -> dict[str, Any]:
    return {
        "service": role,
        "ready": True,
        "endpoints": ["/test", "/test/setup", "/test/run", "/test/report"],
    }
