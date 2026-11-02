"""Part 4: POST /v1/test/reset, POST /v1/test/clock/advance and /v1/test/suite (Phase 4 spec
4.5)."""

import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import clock, config, escalations, gaps, notifications, platform, runtime, suite_runner
from app.auth import key_digest
from app.main import create_app
from tests.api.conftest import FORBIDDEN, MALFORMED, NOT_FOUND, UNAUTHORIZED, ApiClient
from tests.db_helpers import bearer


def test_reset_leaves_a_valid_seed_and_zero_offset(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        response = client.request("POST", "/v1/test/reset", headers=bearer("rev-1"))
        assert response.status_code == 204
        assert response.content == b""
        verify = client.get("/v1/ledger/verify", headers=bearer("viewer-1"))
    assert verify.json() == {"valid": True, "entries_checked": 1, "first_mismatch": None}
    with owner_engine.connect() as conn:
        offset = conn.execute(text("SELECT offset_seconds FROM clock_state")).scalar_one()
        systems = conn.execute(text("SELECT system_id FROM monitored_system")).scalars().all()
    assert offset == 0
    assert systems == ["sys-42"]


def test_clock_advance_opens_a_gap_after_three_intervals(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset", headers=bearer("rev-1")).status_code == 204
        response = client.request(
            "POST",
            "/v1/test/clock/advance",
            headers=bearer("viewer-1"),
            json={"seconds": 900},
        )
        body = response.json()
    assert response.status_code == 200
    assert set(body) == {"simulated_time"}
    assert isinstance(body["simulated_time"], str) and body["simulated_time"].endswith("Z")
    with owner_engine.connect() as conn:
        types = (
            conn.execute(text("SELECT type FROM event WHERE type = 'monitoring_gap'"))
            .scalars()
            .all()
        )
    assert types == ["monitoring_gap"]


def test_clock_advance_allows_zero_seconds() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset", headers=bearer("rev-1")).status_code == 204
        response = client.request(
            "POST",
            "/v1/test/clock/advance",
            headers=bearer("rev-1"),
            json={"seconds": 0},
        )
    assert response.status_code == 200
    assert set(response.json()) == {"simulated_time"}


def test_test_routes_require_a_key() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset").status_code == 401
        assert (
            client.request("POST", "/v1/test/clock/advance", json={"seconds": 1}).status_code == 401
        )


def test_test_routes_are_404_outside_the_test_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("ENVIRONMENT", "production")
    with ApiClient(create_app()) as client:
        # 404 before any key check (Addendum rule 7): no Authorization header.
        assert client.request("POST", "/v1/test/reset").json() == NOT_FOUND
        assert (
            client.request("POST", "/v1/test/clock/advance", json={"seconds": 1}).json()
            == NOT_FOUND
        )


def test_gap_events_are_listed_through_the_events_query() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", "/v1/test/reset", headers=bearer("rev-1")).status_code == 204
        client.request(
            "POST",
            "/v1/test/clock/advance",
            headers=bearer("rev-1"),
            json={"seconds": 900},
        )
        gaps = client.get("/v1/events?type=monitoring_gap", headers=bearer("viewer-1")).json()
    assert len(gaps) == 1
    assert gaps[0]["type"] == "monitoring_gap"


# --- Phase 4 Part 4, F-b: every clause of spec 4.5 has a test --------------------------------

ADVANCE = "/v1/test/clock/advance"
RESET = "/v1/test/reset"
SUITE = "/v1/test/suite"
# Spec 4.4 (Pinned): the one harm_context row the seed installs.
SEED_CONTEXT = (
    "sys-42",
    "fundamental_rights_infringement",
    "sys-42 makes automated decisions about natural persons, so degraded performance can"
    " infringe their fundamental rights (Art. 3(49)(c)).",
)


def test_a_system_key_gets_403_on_reset_and_advance() -> None:
    """Spec 4.5 auth order: a valid key of the wrong role is 403, after the seed exists."""
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        reset = client.request("POST", RESET, headers=bearer("sys-42"))
        advance = client.request("POST", ADVANCE, headers=bearer("sys-42"), json={"seconds": 0})
    assert (reset.status_code, reset.json()) == (403, FORBIDDEN)
    assert (advance.status_code, advance.json()) == (403, FORBIDDEN)


@pytest.mark.parametrize(
    "body",
    [
        {"seconds": -1},
        {"seconds": True},
        {"seconds": "5"},
        {"seconds": 1.5},
        {"seconds": None},
        {},
        {"seconds": 1, "extra": 1},
        [1],
        "5",
    ],
    ids=["negative", "bool", "string", "float", "null", "empty", "extra-key", "array", "scalar"],
)
def test_bad_seconds_are_400(body: object) -> None:
    with ApiClient(create_app()) as client:
        response = client.request("POST", ADVANCE, headers=bearer("rev-1"), json=body)
    assert (response.status_code, response.json()) == (400, MALFORMED)


def test_a_body_that_is_not_json_is_400() -> None:
    with ApiClient(create_app()) as client:
        response = client.request(
            "POST",
            ADVANCE,
            headers={**bearer("rev-1"), "Content-Type": "application/json"},
            content=b"{not json",
        )
    assert (response.status_code, response.json()) == (400, MALFORMED)


def test_clock_advance_calls_sweep_fire_due_deliver_in_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Spec 4.5: add to the offset, then gaps.sweep, escalations.fire_due and
    notifications.deliver_due once each, in that order."""
    calls: list[str] = []

    def recorder(name: str, result: object) -> object:
        def record(engine: Engine) -> object:
            calls.append(name)
            return result

        return record

    monkeypatch.setattr(gaps, "sweep", recorder("sweep", []))
    monkeypatch.setattr(escalations, "fire_due", recorder("fire_due", []))
    monkeypatch.setattr(notifications, "deliver_due", recorder("deliver_due", 0))
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        response = client.request("POST", ADVANCE, headers=bearer("viewer-1"), json={"seconds": 60})
    assert response.status_code == 200
    assert calls == ["sweep", "fire_due", "deliver_due"]


def test_clock_advance_adds_to_the_offset_before_the_sweep(
    monkeypatch: pytest.MonkeyPatch, owner_engine: Engine
) -> None:
    seen: list[int] = []

    def sweep(engine: Engine) -> list[str]:
        seen.append(clock.offset_seconds())
        return []

    monkeypatch.setattr(gaps, "sweep", sweep)
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        client.request("POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 120})
        client.request("POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 30})
    assert seen == [120, 150]


def test_simulated_time_is_wall_time_plus_the_offset() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        before = clock.wall_now()
        body = client.request(
            "POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 86400}
        ).json()
        after = clock.wall_now()
    simulated = datetime.fromisoformat(body["simulated_time"].replace("Z", "+00:00"))
    assert (
        before + timedelta(days=1, seconds=-1) <= simulated <= after + timedelta(days=1, seconds=1)
    )


def test_runtime_seed_keeps_the_offset_and_prepare_zeroes_it(owner_engine: Engine) -> None:
    """Addendum rule 7: an ordinary process start keeps simulated time; prepare resets it."""
    platform.set_offset_seconds(3600)
    runtime.seed()
    assert clock.offset_seconds() == 3600
    runtime.prepare()
    assert clock.offset_seconds() == 0


def test_reset_zeroes_the_offset_and_restores_the_seeded_keys(owner_engine: Engine) -> None:
    platform.set_offset_seconds(7200)
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM api_key"))
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("viewer-1")).status_code == 204
        events = client.get("/v1/events", headers=bearer("viewer-1"))
    assert clock.offset_seconds() == 0
    assert events.status_code == 200
    with owner_engine.connect() as conn:
        actors = conn.execute(text("SELECT actor_id FROM api_key ORDER BY actor_id")).scalars()
        assert list(actors) == ["rev-1", "sys-42", "viewer-1"]


def test_reset_restarts_the_ledger_at_sequence_one(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        client.request("POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 900})
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
    with owner_engine.connect() as conn:
        rows = conn.execute(text("SELECT seq, entry_type FROM ledger_entry ORDER BY seq")).all()
    assert [tuple(row) for row in rows] == [(1, "SEED")]


# The seed-key bootstrap (decision 137), restricted as approved: test environment only, only
# the two configured keys, and only when no api_key row matches.


def test_with_an_empty_key_table_the_configured_reviewer_and_viewer_keys_work(
    owner_engine: Engine,
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM api_key"))
    with ApiClient(create_app()) as client:
        viewer_suite = client.request("POST", SUITE, headers=bearer("viewer-1"), json={})
        viewer_advance = client.request(
            "POST", ADVANCE, headers=bearer("viewer-1"), json={"seconds": 0}
        )
    assert viewer_advance.status_code == 200
    # The bootstrap keeps the role: a viewer key is still not a reviewer.
    assert (viewer_suite.status_code, viewer_suite.json()) == (403, FORBIDDEN)


def test_the_bootstrap_never_accepts_the_system_key_or_an_unknown_key(owner_engine: Engine) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM api_key"))
    with ApiClient(create_app()) as client:
        system = client.request("POST", RESET, headers=bearer("sys-42"))
        unknown = client.request("POST", RESET, headers={"Authorization": "Bearer not-a-key"})
    assert (system.status_code, system.json()) == (401, UNAUTHORIZED)
    assert (unknown.status_code, unknown.json()) == (401, UNAUTHORIZED)


def test_a_stored_key_row_wins_over_the_bootstrap(owner_engine: Engine) -> None:
    """Only when no api_key row matches: a stored row for the configured reviewer key with
    the VIEWER role makes that key a viewer."""
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM api_key"))
        conn.execute(
            text(
                "INSERT INTO api_key (key_hash, role, actor_id, system_id)"
                " VALUES (:key_hash, 'VIEWER', 'viewer-1', NULL)"
            ),
            {"key_hash": key_digest(config.reviewer_key())},
        )
    with ApiClient(create_app()) as client:
        response = client.request("POST", SUITE, headers=bearer("rev-1"), json={"scope": "smoke"})
    assert (response.status_code, response.json()) == (403, FORBIDDEN)


def test_the_bootstrap_is_off_outside_the_test_environment(
    monkeypatch: pytest.MonkeyPatch, owner_engine: Engine
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM api_key"))
    monkeypatch.setenv("ENVIRONMENT", "production")
    with ApiClient(create_app()) as client:
        reset = client.request("POST", RESET, headers=bearer("rev-1"))
        events = client.get("/v1/events", headers=bearer("rev-1"))
    assert reset.json() == NOT_FOUND
    # Ordinary routes never use the bootstrap.
    assert (events.status_code, events.json()) == (401, UNAUTHORIZED)


# The suite runner (spec 4.5 "Console suite runner", LIM-02, decision 141).


def test_suite_is_404_outside_test_and_403_for_a_viewer(monkeypatch: pytest.MonkeyPatch) -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        viewer_post = client.request(
            "POST", SUITE, headers=bearer("viewer-1"), json={"scope": "smoke"}
        )
        viewer_get = client.get(SUITE, headers=bearer("viewer-1"))
        no_key = client.request("POST", SUITE, json={"scope": "smoke"})
    assert (viewer_post.status_code, viewer_post.json()) == (403, FORBIDDEN)
    assert (viewer_get.status_code, viewer_get.json()) == (403, FORBIDDEN)
    assert (no_key.status_code, no_key.json()) == (401, UNAUTHORIZED)
    monkeypatch.setenv("ENVIRONMENT", "production")
    with ApiClient(create_app()) as client:
        assert client.request("POST", SUITE, json={"scope": "smoke"}).json() == NOT_FOUND
        assert client.get(SUITE).json() == NOT_FOUND


@pytest.mark.parametrize("body", [{"scope": "full"}, {"scope": "everything"}, {"scope": 1}, {}])
def test_suite_scopes_are_only_smoke_and_acceptance(
    body: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The database-level suite runs only in CI (spec 4.5): there is no `full` scope."""
    started: list[str] = []
    # Nothing may really run here, not even on code that still accepts `full`.
    monkeypatch.setattr("app.suite_runner._execute", lambda run: started.append(run.scope))
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        response = client.request("POST", SUITE, headers=bearer("rev-1"), json=body)
    assert (response.status_code, response.json()) == (400, MALFORMED)
    with pytest.raises(ValueError):
        suite_runner.start("full")
    assert started == []


@dataclass
class _Recorded:
    order: list[str] = field(default_factory=list)
    command: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str = ""


def _record_runner(
    monkeypatch: pytest.MonkeyPatch, *, returncode: int = 0, raises: bool = False
) -> _Recorded:
    recorded = _Recorded()

    def run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        recorded.order.append("runner")
        recorded.command = list(command)
        recorded.env = dict(kwargs["env"])
        recorded.cwd = str(kwargs["cwd"])
        if raises:
            raise subprocess.TimeoutExpired(command, 1)
        return subprocess.CompletedProcess(command, returncode, "3 passed in 0.1s\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(platform, "reset", lambda: recorded.order.append("reset"))
    return recorded


def test_suite_acceptance_runs_only_the_http_runner_against_this_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded = _record_runner(monkeypatch)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://x:y@db/never")
    monkeypatch.setenv("DATABASE_ADMIN_URL", "postgresql+psycopg://x:y@db/never")
    monkeypatch.delenv("BASE_URL", raising=False)
    run = suite_runner.SuiteRun(id="t1", scope="acceptance")
    suite_runner._execute(run)
    assert recorded.order == ["reset", "runner", "reset"]
    assert recorded.command == [
        sys.executable,
        "-m",
        "pytest",
        "tests/acceptance",
        "--confcutdir=tests/acceptance",
        "-p",
        "no:cacheprovider",
        "-q",
        "--tb=short",
    ]
    assert recorded.cwd == str(suite_runner.ROOT)
    assert recorded.env["BASE_URL"] == "http://127.0.0.1:8000"
    # The runner needs no database, so it is given none (the root conftest would rebuild it).
    assert "DATABASE_URL" not in recorded.env
    assert "DATABASE_ADMIN_URL" not in recorded.env
    assert recorded.env["REVIEWER_KEY"] == config.reviewer_key()
    assert recorded.env["VIEWER_KEY"] == config.viewer_key()
    assert (run.status, run.exit_code) == ("passed", 0)


def test_suite_acceptance_uses_suite_base_url_when_set(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded = _record_runner(monkeypatch)
    monkeypatch.setenv("SUITE_BASE_URL", "http://127.0.0.1:9000/")
    suite_runner._execute(suite_runner.SuiteRun(id="t2", scope="acceptance"))
    assert recorded.env["BASE_URL"] == "http://127.0.0.1:9000"


@pytest.mark.parametrize(("returncode", "raises"), [(1, False), (0, True)])
def test_suite_acceptance_resets_after_a_failed_or_crashed_run(
    monkeypatch: pytest.MonkeyPatch, returncode: int, raises: bool
) -> None:
    recorded = _record_runner(monkeypatch, returncode=returncode, raises=raises)
    run = suite_runner.SuiteRun(id="t3", scope="acceptance")
    suite_runner._execute(run)
    assert recorded.order == ["reset", "runner", "reset"]
    assert run.status == "failed"


def test_suite_runner_has_no_in_process_pytest_path() -> None:
    """LIM-02: nothing in the service runs the database-level suite."""
    source = Path(suite_runner.__file__).read_text(encoding="utf-8")
    assert '"full"' not in source
    assert "_ACCEPTANCE_NODEIDS" not in source
    assert 'cmd.append("tests")' not in source


# Reset and the Phase 4 tables (spec 4.4 and 4.5). They need the real migrations 0005 to 0007.


def _flag_and_dispose(client: ApiClient) -> str:
    now = client.request("POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 0}).json()
    ingest = client.request(
        "POST",
        "/v1/metrics",
        headers=bearer("sys-42"),
        json={
            "system_id": "sys-42",
            "metric_name": "accuracy",
            "metric_type": "performance",
            "value": 0.61,
            "timestamp": now["simulated_time"],
        },
    )
    assert ingest.status_code == 201, ingest.text
    event_id = str(ingest.json()["flagged_event_id"])
    assert client.get(f"/v1/events/{event_id}", headers=bearer("rev-1")).status_code == 200
    decided = client.request(
        "POST",
        f"/v1/events/{event_id}/disposition",
        headers=bearer("rev-1"),
        json={
            "tier": "OTHER_SERIOUS",
            "rationale": None,
            "harm_fields": {
                "health_safety_harm": {"status": "NOT_APPLICABLE"},
                "critical_infrastructure_disruption": {"status": "NOT_APPLICABLE"},
                "fundamental_rights_infringement": {"status": "CONFIRMED"},
                "property_environmental_damage": {"status": "NOT_APPLICABLE"},
            },
        },
    )
    assert decided.status_code == 200, decided.text
    return event_id


@pytest.mark.integration
def test_reset_empties_phase4_tables_and_reseeds_harm_context(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        _flag_and_dispose(client)
        with owner_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO harm_context (system_id, field_key, context_text)"
                    " VALUES ('sys-42', 'health_safety_harm', 'added by the test')"
                )
            )
            assert conn.execute(text("SELECT count(*) FROM disposition")).scalar_one() == 1
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
    with owner_engine.connect() as conn:
        dispositions = conn.execute(text("SELECT count(*) FROM disposition")).scalar_one()
        contexts = conn.execute(
            text("SELECT system_id, field_key, context_text FROM harm_context")
        ).all()
    assert dispositions == 0
    assert [tuple(row) for row in contexts] == [SEED_CONTEXT]


@pytest.mark.integration
def test_reset_empties_escalation_while_reminders_are_due(owner_engine: Engine) -> None:
    """Integration Guide 8: a reset while reminders are due leaves nothing to fire."""
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        event_id = _flag_and_dispose(client)
        with owner_engine.connect() as conn:
            planned = conn.execute(
                text("SELECT count(*) FROM escalation WHERE event_id = :e AND status = 'PENDING'"),
                {"e": event_id},
            ).scalar_one()
        assert planned == 3
        # Every planned reminder is now due, but the reset comes first.
        client.request("POST", RESET, headers=bearer("rev-1"))
        client.request("POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 16 * 86400})
    with owner_engine.connect() as conn:
        escalations = conn.execute(text("SELECT count(*) FROM escalation")).scalar_one()
        reminders = conn.execute(
            text("SELECT count(*) FROM notification WHERE notification_type = 'reminder'")
        ).scalar_one()
    assert (escalations, reminders) == (0, 0)


# Decision 226: an advance that would take the simulated time past the year 9999, or the
# stored offset past a signed 64-bit integer, is 400 MALFORMED_BODY and writes nothing.


def _ingest_now(client: ApiClient) -> int:
    """Advance 0, then ingest one sample at the simulated time; returns the status code."""
    now = client.request("POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 0})
    assert now.status_code == 200, now.text
    ingest = client.request(
        "POST",
        "/v1/metrics",
        headers=bearer("sys-42"),
        json={
            "system_id": "sys-42",
            "metric_name": "accuracy",
            "metric_type": "performance",
            "value": 0.61,
            "timestamp": now.json()["simulated_time"],
        },
    )
    return ingest.status_code


@pytest.mark.parametrize("seconds", [10**13, 2**63], ids=["past-year-9999", "past-int64"])
def test_an_advance_past_the_representable_time_is_400_and_writes_nothing(seconds: int) -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        assert (
            client.request("POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 60})
        ).status_code == 200
        response = client.request(
            "POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": seconds}
        )
        assert (response.status_code, response.json()) == (400, MALFORMED)
        assert clock.offset_seconds() == 60
        assert _ingest_now(client) == 201
    assert clock.offset_seconds() == 60


def test_an_advance_just_past_the_year_9999_is_400() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        to_the_limit = datetime.max.replace(tzinfo=clock.wall_now().tzinfo) - clock.wall_now()
        seconds = int(to_the_limit.total_seconds()) + 3600
        response = client.request(
            "POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": seconds}
        )
    assert (response.status_code, response.json()) == (400, MALFORMED)
    assert clock.offset_seconds() == 0


def test_a_large_advance_that_stays_before_the_year_9999_still_works() -> None:
    seconds = 2 * 10**11
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        response = client.request(
            "POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": seconds}
        )
        assert response.status_code == 200, response.text
        assert clock.offset_seconds() == seconds
        assert _ingest_now(client) == 201
