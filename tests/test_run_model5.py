"""The Model 5 fixture runner's pure parts (Phase 4 spec 4.7; Part 4 decision 144).

No database and no server: the parsers and checks are called directly, and whole examples run
against httpx.MockTransport so a failing expectation is shown to fail the run.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest

from tools import run_model5 as m5

T0 = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("token", "seconds"),
    [
        ("+0s", 0),
        ("+4h", 14400),
        ("+3d", 259200),
        ("+5d4h", 5 * 86400 + 4 * 3600),
        ("+7d16h", 7 * 86400 + 16 * 3600),
        ("+1d2h3m4s", 86400 + 7200 + 180 + 4),
    ],
)
def test_offsets_are_compound_units_from_the_example_start(token: str, seconds: int) -> None:
    assert m5.parse_offset(token) == seconds


@pytest.mark.parametrize("token", ["5d", "+", "+4x", "-1d", "+1.5h", "+d", ""])
def test_a_bad_offset_is_refused(token: str) -> None:
    with pytest.raises(ValueError):
        m5.parse_offset(token)


def test_json_path_walks_objects_and_list_indexes() -> None:
    body = {"revisions": [{"prior_tier": "OTHER_SERIOUS"}], "a": {"b": None}}
    assert m5.json_path(body, "$.revisions.0.prior_tier") == "OTHER_SERIOUS"
    assert m5.json_path(body, "$.a.b") is None
    assert m5.json_path(body, "$") == body
    with pytest.raises(KeyError):
        m5.json_path(body, "$.revisions.1")
    with pytest.raises(KeyError):
        m5.json_path(body, "$.a.c")


def test_substitution_fills_saved_values_and_refuses_unknown_ones() -> None:
    assert m5.substitute({"p": ["/v1/events/${event}"]}, {"event": "evt-1"}) == {
        "p": ["/v1/events/evt-1"]
    }
    with pytest.raises(KeyError):
        m5.substitute("${missing}", {})


@pytest.mark.parametrize(
    ("expr", "got", "ok"),
    [
        ("$.disposition.assigned_at + 15d", "2026-10-20T13:00:00Z", True),
        ("$.disposition.assigned_at + 15d", "2026-10-20T13:00:01Z", False),
        ("${anchor} + 10d", "2026-10-15T13:00:00Z", True),
        ("$.detected_at + 4h ~60s", "2026-10-05T13:00:59Z", True),
        ("$.detected_at + 4h ~60s", "2026-10-05T13:01:01Z", False),
        ("$.detected_at + 4h ~60s", "2026-10-05T12:59:59Z", False),
    ],
)
def test_relative_times_are_exact_unless_a_tolerance_is_given(
    expr: str, got: str, ok: bool
) -> None:
    body = {
        "detected_at": "2026-10-05T09:00:00Z",
        "disposition": {"assigned_at": "2026-10-05T13:00:00Z"},
        "value": got,
    }
    variables = {"anchor": "2026-10-05T13:00:00Z"}
    if ok:
        m5.check_relative(body, "$.value", expr, variables)
    else:
        with pytest.raises(m5.Mismatch):
            m5.check_relative(body, "$.value", expr, variables)


def test_api_times_with_a_fraction_compare_by_instant() -> None:
    body = {"a": "2026-10-05T13:00:00.250000Z", "b": "2026-10-15T13:00:00.25Z"}
    m5.check_relative(body, "$.b", "$.a + 10d", {})


def test_the_sink_view_filters_by_type_and_event() -> None:
    items: list[dict[str, Any]] = [
        {"received_at": "x", "body": {"notification_type": "verification_ping"}},
        {"received_at": "x", "body": {"notification_type": "flag_notification", "event_id": "e1"}},
        {
            "received_at": "x",
            "body": {"notification_type": "reminder", "event_id": "e1", "escalation_pct": 50},
        },
        {
            "received_at": "x",
            "body": {"notification_type": "reminder", "event_id": "e2", "escalation_pct": 50},
        },
        {
            "received_at": "x",
            "body": {"notification_type": "reminder", "event_id": "e1", "escalation_pct": 80},
        },
    ]
    assert m5.sink_bodies(items, "reminder", "e1") == [items[2]["body"], items[4]["body"]]
    assert m5.sink_bodies(items, "flag_notification", "e1") == [items[1]["body"]]
    assert m5.sink_bodies(items, "reminder", "e3") == []


def test_a_redelivered_notification_counts_once() -> None:
    """Delivery is at least once (spec: a receiver can see the same notification_id twice and
    should de-duplicate on it), so the sink view keeps the first copy of each id."""
    first = {"notification_type": "reminder", "event_id": "e1", "escalation_pct": 50}
    items: list[dict[str, Any]] = [
        {"received_at": "x", "body": {**first, "notification_id": "ntf-1"}},
        {"received_at": "y", "body": {**first, "notification_id": "ntf-1"}},
        {"received_at": "z", "body": {**first, "escalation_pct": 80, "notification_id": "ntf-2"}},
    ]
    assert [b["escalation_pct"] for b in m5.sink_bodies(items, "reminder", "e1")] == [50, 80]


def _entry(seq: int, entry_type: str, actor: str, subject: str, **payload: Any) -> dict[str, Any]:
    return {
        "seq": seq,
        "entry_type": entry_type,
        "actor_id": actor,
        "subject_id": subject,
        "payload": payload,
    }


def test_the_audit_delta_is_exact_for_the_event_and_ignores_other_subjects() -> None:
    new = [
        _entry(5, "NOTIFICATION_DELIVERED", "system", "evt-gap"),
        _entry(6, "DISPOSITION_RECORDED", "rev-1", "evt-1", disposition_id="dsp-1", tier="DEATH"),
    ]
    saved = m5.match_delta(
        new,
        "evt-1",
        [
            {
                "entry_type": "DISPOSITION_RECORDED",
                "actor_id": "rev-1",
                "payload": {"tier": "DEATH"},
                "save": {"first": "payload.disposition_id"},
            }
        ],
    )
    assert saved == {"first": "dsp-1"}
    with pytest.raises(m5.Mismatch):
        m5.match_delta(new, "evt-1", [])  # something was appended that the step does not expect
    with pytest.raises(m5.Mismatch):
        m5.match_delta(
            new, "evt-1", [{"entry_type": "DISPOSITION_RECORDED", "actor_id": "viewer-1"}]
        )
    with pytest.raises(m5.Mismatch):
        m5.match_delta(
            new,
            "evt-1",
            [{"entry_type": "DISPOSITION_RECORDED", "payload": {"tier": "OTHER_SERIOUS"}}],
        )
    assert m5.match_delta(new[:1], "evt-1", []) == {}


def _server(responses: dict[str, httpx.Response]) -> httpx.Client:
    """A fake service: the clock answers, the sink is empty, other calls come from responses."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/test/clock/advance":
            seconds = json.loads(request.content)["seconds"]
            handler.offset += seconds  # type: ignore[attr-defined]
            now = T0 + timedelta(seconds=handler.offset)  # type: ignore[attr-defined]
            return httpx.Response(200, json={"simulated_time": now.strftime("%Y-%m-%dT%H:%M:%SZ")})
        if request.url.path.startswith("/hook/"):
            return httpx.Response(200, json=[])
        if request.url.path == "/v1/test/reset":
            handler.offset = 0  # type: ignore[attr-defined]
        key = f"{request.method} {request.url.path}"
        return responses.get(key, httpx.Response(404, json={"error_code": "NOT_FOUND"}))

    handler.offset = 0  # type: ignore[attr-defined]
    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://service")


KEYS = {"system": "s", "reviewer": "r", "viewer": "v"}
SINK = m5.SinkChannel(url="http://sink/hook/t", read_url="http://sink/hook/t")


def _example(expect: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": "exT",
        "title": "test",
        "steps": [
            {
                "at": "+0s",
                "as": "reviewer",
                "call": "POST /v1/test/reset",
                "expect": {"status": 204},
            },
            {"at": "+4h", "as": "viewer", "call": "GET /v1/events/evt-1", "expect": expect},
        ],
    }


def test_a_passing_example_runs_every_step_and_the_run_exits_0() -> None:
    client = _server(
        {
            "POST /v1/test/reset": httpx.Response(204),
            "GET /v1/events/evt-1": httpx.Response(
                200, json={"state": "FLAGGED", "detected_at": "2026-10-05T09:00:00Z"}
            ),
        }
    )
    example = _example({"status": 200, "eq": {"$.state": "FLAGGED", "$.detected_at": "${T0}"}})
    lines = m5.run_example(client, example, KEYS, SINK)
    assert lines == ["ok exT step 0: POST /v1/test/reset", "ok exT step 1: GET /v1/events/evt-1"]
    assert m5.run_all(client, {"version": 1, "examples": [example]}, KEYS, SINK) == 0


@pytest.mark.parametrize(
    "expect",
    [
        {"status": 201},
        {"status": 200, "eq": {"$.state": "UNDER_REVIEW"}},
        {"status": 200, "body": {"state": "FLAGGED"}},
        {"status": 200, "len": {"$.revisions": 1}},
    ],
    ids=["status", "eq", "body", "missing-path"],
)
def test_a_failing_expectation_fails_the_example_and_the_run_exits_1(
    expect: dict[str, Any], capsys: pytest.CaptureFixture[str]
) -> None:
    client = _server(
        {
            "POST /v1/test/reset": httpx.Response(204),
            "GET /v1/events/evt-1": httpx.Response(
                200, json={"state": "FLAGGED", "detected_at": "2026-10-05T09:00:00Z"}
            ),
        }
    )
    example = _example(expect)
    with pytest.raises(m5.StepFailure) as failure:
        m5.run_example(client, example, KEYS, SINK)
    assert failure.value.index == 1
    assert m5.run_all(client, {"version": 1, "examples": [example]}, KEYS, SINK) == 1
    assert "exT FAILED at step 1" in capsys.readouterr().out


def test_the_clock_is_moved_to_each_steps_offset_from_the_start() -> None:
    seen: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/test/clock/advance":
            seen.append(json.loads(request.content)["seconds"])
            return httpx.Response(200, json={"simulated_time": "2026-10-05T09:00:00Z"})
        if request.url.path.startswith("/hook/"):
            return httpx.Response(200, json=[])
        return httpx.Response(204)

    client = httpx.Client(transport=httpx.MockTransport(handler), base_url="http://service")
    steps = [
        {"at": at, "as": "reviewer", "call": "POST /v1/test/reset", "expect": {"status": 204}}
        for at in ("+0s", "+4h", "+3d", "+3d", "+5d4h")
    ]
    m5.run_example(client, {"id": "exC", "title": "c", "steps": steps}, KEYS, SINK)
    moves = [s for s in seen if s]
    assert moves == [14400, 3 * 86400 - 14400, 2 * 86400 + 4 * 3600]


def test_the_shipped_fixture_parses_and_uses_only_known_expectations() -> None:
    data = json.loads(m5.FIXTURE.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert [example["id"] for example in data["examples"]] == ["ex1", "ex2", "ex4"]
    for example in data["examples"]:
        offsets = [m5.parse_offset(step["at"]) for step in example["steps"]]
        assert offsets == sorted(offsets), example["id"]
        for step in example["steps"]:
            assert set(step) <= {"at", "as", "call", "body", "expect", "sink", "note"}, step
            assert set(step.get("expect", {})) <= m5.EXPECTATIONS, step


def _paths(example: dict[str, Any]) -> list[str]:
    found = []
    for step in example["steps"]:
        expect = step.get("expect", {})
        for kind in ("eq", "len", "relative"):
            found.extend(expect.get(kind, {}))
        found.extend(step.get("sink", {}).get("eq", {}))
    return found


def test_every_path_in_the_shipped_fixture_is_a_json_path() -> None:
    data = json.loads(m5.FIXTURE.read_text(encoding="utf-8"))
    for example in data["examples"]:
        for path in _paths(example):
            assert path == "$" or path.startswith("$."), (example["id"], path)


def _sink_client(bodies: list[dict[str, Any]]) -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"received_at": "x", "body": b} for b in bodies])

    return httpx.Client(transport=httpx.MockTransport(handler), base_url="http://sink")


def test_the_sink_check_compares_body_fields_and_names_a_missing_one() -> None:
    flag = {"notification_type": "flag_notification", "event_id": "e1", "type": "threshold_breach"}
    client = _sink_client([flag])
    spec: dict[str, Any] = {
        "notification_type": "flag_notification",
        "event": "e1",
        "count": 1,
        "eq": {"$.type": "threshold_breach"},
    }
    m5._check_sink(client, SINK, spec, {})
    with pytest.raises(m5.Mismatch, match="threshold_breach"):
        m5._check_sink(client, SINK, {**spec, "eq": {"$.type": "monitoring_gap"}}, {})
    with pytest.raises(m5.Mismatch, match=r"\$\.system_id"):
        m5._check_sink(client, SINK, {**spec, "eq": {"$.system_id": "sys-42"}}, {})
