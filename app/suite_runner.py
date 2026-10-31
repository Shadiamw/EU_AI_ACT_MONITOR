"""The console suite runner (Phase 4 spec 4.5 "Console suite runner", LIM-02; decision 141).

Two scopes, both over HTTP against this service:

- smoke: a few calls to the public surface (no pytest).
- acceptance: the HTTP acceptance runner (tests/acceptance, spec 4.8) as a subprocess, against
  this service's own URL, with a reset before and after the run so the seeded keys and a clean
  state are always restored, even when the run fails or crashes.

The database-level pytest suite never runs in a service: it runs only in CI. The runner is
given no DATABASE_* variable and is started with --confcutdir=tests/acceptance, so
tests/conftest.py (which rebuilds the database it is pointed at) is never loaded.

Only one run is active at a time. Output is kept in memory for the UI to poll.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app import clock, platform

ROOT = Path(__file__).resolve().parent.parent

_SCOPES = frozenset({"smoke", "acceptance"})
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
RUNNER_COMMAND = (
    "-m",
    "pytest",
    "tests/acceptance",
    "--confcutdir=tests/acceptance",
    "-p",
    "no:cacheprovider",
    "-q",
    "--tb=short",
)
# Never handed to the runner: it talks HTTP only (spec 4.8).
_DATABASE_VARIABLES = ("DATABASE_URL", "DATABASE_ADMIN_URL")
RUNNER_TIMEOUT_SECONDS = 60 * 20

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
            code, text = _run_acceptance()
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

    base = service_base_url()
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

    reviewer = os.environ.get("REVIEWER_KEY", "")
    reset_headers = {"Authorization": f"Bearer {reviewer}"} if reviewer else {}
    req = urllib.request.Request(base + "/v1/test/reset", method="POST", headers=reset_headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, body = resp.status, resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status, body = exc.code, exc.read().decode("utf-8", errors="replace")
    lines.append(f"POST /v1/test/reset -> {status} {body[:200]}")
    if status != 204:
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


def service_base_url() -> str:
    """This service's own URL: SUITE_BASE_URL when set, else the API's port on loopback."""
    return os.environ.get("SUITE_BASE_URL", DEFAULT_BASE_URL).rstrip("/")


def runner_environment() -> dict[str, str]:
    """The service's environment (its keys, any WEBHOOK_SINK_*), without the database URLs,
    with BASE_URL set to this service."""
    env = {k: v for k, v in os.environ.items() if k not in _DATABASE_VARIABLES}
    env["BASE_URL"] = service_base_url()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run_acceptance() -> tuple[int, str]:
    """The HTTP acceptance runner against this service, between two resets (spec 4.5)."""
    if not (ROOT / "tests" / "acceptance").is_dir():
        return 1, "tests/acceptance is not present in this image; rebuild with tests included"
    platform.reset()
    try:
        proc = subprocess.run(
            [sys.executable, *RUNNER_COMMAND],
            cwd=ROOT,
            env=runner_environment(),
            capture_output=True,
            text=True,
            timeout=RUNNER_TIMEOUT_SECONDS,
            check=False,
        )
    finally:
        platform.reset()
    text = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return proc.returncode, text.strip() or f"pytest exited {proc.returncode}"
