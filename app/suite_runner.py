"""Run the project's pytest suite from the test-environment API (UI / Pod demo).

Only one run is active at a time. Output is kept in memory for the UI to poll.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import clock

ROOT = Path(__file__).resolve().parent.parent

# Subsets that map to the Phase 3 acceptance criteria named by tools/check_acceptance.py.
_ACCEPTANCE_NODEIDS = (
    "tests/worker/test_gaps.py::test_ac02_two_missed_intervals_do_not_open_a_gap_and_three_do",
    "tests/worker/test_worker_system.py::test_gap_to_delivery_to_close_and_reverify",
    "tests/worker/test_notifications.py::test_delivery_appends_delivered_with_integer_latency_and_boolean_bound",
    "tests/worker/test_notifications.py::test_within_bound_compares_latency_with_the_configured_bound",
    "tests/worker/test_notifications.py::test_ac04_the_bound_is_configurable_to_five_minutes",
    "tests/worker/test_notifications.py::test_ac04_a_slow_receiver_with_a_low_bound_is_not_within_bound",
    "tests/test_verify.py::test_ac06_payload_tamper_is_detected",
    "tests/api/test_export_verify.py::test_ac06_payload_tamper_through_the_api",
    "tests/test_schema_permissions.py::test_app_role_has_insert_and_select_only",
    "tests/test_schema_permissions.py::test_trigger_definition_blocks_update_and_delete_but_not_truncate",
    "tests/api/test_audit.py",
)

_SCOPES = frozenset({"smoke", "acceptance", "full"})

_lock = threading.Lock()
_current: SuiteRun | None = None


@dataclass
class SuiteRun:
    id: str
    scope: str
    status: str = "queued"  # queued | running | passed | failed
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    exit_code: int | None = None
    output: str = ""
    summary: str = ""

    def to_dict(self, *, include_output: bool = True) -> dict[str, Any]:
        body: dict[str, Any] = {
            "id": self.id,
            "scope": self.scope,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "exit_code": self.exit_code,
            "summary": self.summary,
        }
        if include_output:
            body["output"] = self.output
        return body


def current_run() -> SuiteRun | None:
    with _lock:
        return _current


def start(scope: str) -> SuiteRun:
    if scope not in _SCOPES:
        raise ValueError("scope")
    with _lock:
        global _current
        if _current is not None and _current.status in ("queued", "running"):
            raise RuntimeError("busy")
        run = SuiteRun(id=uuid.uuid4().hex[:12], scope=scope)
        _current = run
    thread = threading.Thread(target=_execute, args=(run,), daemon=True)
    thread.start()
    return run


def _execute(run: SuiteRun) -> None:
    run.status = "running"
    try:
        if run.scope == "smoke":
            code, text = _run_smoke()
        else:
            code, text = _run_pytest(run.scope)
        run.output = text
        run.exit_code = code
        run.status = "passed" if code == 0 else "failed"
        run.summary = _summarize(run.scope, code, text)
    except Exception as exc:  # noqa: BLE001 — surface any runner failure to the UI
        run.output = (run.output + f"\nrunner error: {type(exc).__name__}: {exc}").strip()
        run.exit_code = 1
        run.status = "failed"
        run.summary = "suite runner failed"
    finally:
        run.finished_at = clock.wall_now().timestamp()


def _summarize(scope: str, code: int, text: str) -> str:
    if code == 0:
        return f"{scope} suite passed"
    # Prefer pytest's short summary line when present.
    for line in reversed(text.splitlines()):
        if ("failed" in line or "error" in line or "passed" in line) and (
            line.strip().startswith("=") or " in " in line
        ):
            return line.strip(" =")
    return f"{scope} suite failed (exit {code})"


def _run_smoke() -> tuple[int, str]:
    """Hit this process's public surface without invoking pytest."""
    import urllib.error
    import urllib.request

    base = os.environ.get("SUITE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    lines: list[str] = [f"smoke against {base}"]
    ok = True

    def get(path: str) -> tuple[int, str]:
        req = urllib.request.Request(base + path, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    def post(path: str, body: bytes | None = None) -> tuple[int, str]:
        req = urllib.request.Request(
            base + path,
            data=body,
            method="POST",
            headers={"Content-Type": "application/json"} if body is not None else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return resp.status, resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    status, body = get("/healthz")
    lines.append(f"GET /healthz -> {status} {body[:200]}")
    if status not in (200, 503):
        ok = False

    status, body = post("/v1/test/reset")
    lines.append(f"POST /v1/test/reset -> {status} {body[:200]}")
    if status not in (200, 204):
        ok = False

    viewer = os.environ.get("VIEWER_KEY", "")
    req = urllib.request.Request(
        base + "/v1/ledger/verify",
        headers={"Authorization": f"Bearer {viewer}"} if viewer else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            status, body = resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read().decode("utf-8", errors="replace")
    lines.append(f"GET /v1/ledger/verify -> {status} {body[:300]}")
    if status != 200:
        ok = False

    lines.append("smoke ok" if ok else "smoke failed")
    return (0 if ok else 1), "\n".join(lines)


def _run_pytest(scope: str) -> tuple[int, str]:
    if not (ROOT / "tests").is_dir():
        return 1, "tests/ is not present in this image; rebuild with tests included"
    try:
        import pytest  # noqa: F401
    except ImportError:
        return 1, "pytest is not installed; image must install the [dev] extra"

    cmd = ["python", "-m", "pytest", "-q", "--tb=line"]
    if scope == "acceptance":
        cmd.extend(_ACCEPTANCE_NODEIDS)
    else:
        cmd.append("tests")

    env = os.environ.copy()
    # Prefer the container's DATABASE_* URLs; conftest only setdefaults.
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60 * 45,
        check=False,
    )
    text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, text.strip() or f"pytest exited {proc.returncode}"
