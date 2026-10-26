"""Phase 4 Part 1: harm-field pre-fill at flag time (Phase 4 spec 1.3, AC-07).

Every expected evidence text is typed out from spec 1.3 and the contract's values, never built
with the code's formatter, so a formatting bug cannot hide behind an expected value built the
same way. The seed (tools/seed_part1.py) writes one harm_context row: sys-42's
fundamental_rights_infringement.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy import Engine, text

from app import gaps
from tests.api.conftest import ApiClient
from tests.db_helpers import HARM_FIELDS
from tests.part1.conftest import T0, get, metric, post, post_raw, rows
from tests.worker.conftest import FakeClock

RIGHTS = "fundamental_rights_infringement"
# Spec 1.3's own worked example (the code block there wraps one line of text for reading).
AC07_EVIDENCE = (
    "accuracy = 0.61 is below the limit 0.70 (threshold thr-2) at 2026-10-05T09:00:00Z."
    " sys-42 makes automated decisions about natural persons, so degraded performance can"
    " infringe their fundamental rights (Art. 3(49)(c))."
)
# Contract Example 1 (accuracy_drift 0.34 against thr-1, gt 0.30) through the same template.
EXAMPLE_1_EVIDENCE = (
    "accuracy_drift = 0.34 is above the limit 0.30 (threshold thr-1) at 2026-09-27T10:05:00Z."
    " sys-42 makes automated decisions about natural persons, so degraded performance can"
    " infringe their fundamental rights (Art. 3(49)(c))."
)
CONTEXT_TEXT = (
    "sys-42 makes automated decisions about natural persons, so degraded performance can"
    " infringe their fundamental rights (Art. 3(49)(c))."
)
AC07_AT = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)


def pending(evidence: str | None = None) -> dict[str, Any]:
    return {
        "status": "PREFILLED_PENDING_CONFIRMATION",
        "evidence": evidence,
        "confirmed_by": None,
        "confirmed_at": None,
    }


def post_ac07(client: ApiClient, fake_clock: FakeClock) -> str:
    """Spec 1.3's example: accuracy 0.61 on sys-42 at 2026-10-05T09:00:00Z (thr-2, lt 0.70)."""
    fake_clock.set(AC07_AT)
    response = post(
        client,
        metric_name="accuracy",
        metric_type="performance",
        value=0.61,
        timestamp="2026-10-05T09:00:00Z",
    )
    assert response.status_code == 201
    event_id: str = response.json()["flagged_event_id"]
    return event_id


def stored_fields(owner_engine: Engine, event_id: str) -> list[dict[str, Any]]:
    return rows(
        owner_engine,
        "SELECT field_key, status, evidence, prefill_evidence, confirmed_by, confirmed_at"
        " FROM harm_field WHERE event_id = :event_id ORDER BY field_key",
        event_id=event_id,
    )


def test_ac07_prefill_fills_the_rights_field_and_leaves_the_others_blank(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = post_ac07(client, fake_clock)
    detail = get(client, f"/v1/events/{event_id}").json()
    assert detail["state"] == "FLAGGED"
    assert detail["disposition"] is None
    assert detail["harm_fields"] == {
        key: pending(AC07_EVIDENCE if key == RIGHTS else None) for key in HARM_FIELDS
    }
    assert not any(f["status"] == "CONFIRMED" for f in detail["harm_fields"].values())


def test_the_above_case_uses_the_gt_word(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    event_id = post(client).json()["flagged_event_id"]  # contract Example 1
    detail = get(client, f"/v1/events/{event_id}").json()
    assert detail["harm_fields"][RIGHTS] == pending(EXAMPLE_1_EVIDENCE)


@pytest.mark.parametrize("case", ["ac07", "example_1"])
def test_evidence_and_prefill_evidence_are_stored_alike(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock, case: str
) -> None:
    if case == "ac07":
        event_id, expected = post_ac07(client, fake_clock), AC07_EVIDENCE
    else:
        event_id, expected = post(client).json()["flagged_event_id"], EXAMPLE_1_EVIDENCE
    assert stored_fields(owner_engine, event_id) == [
        {
            "field_key": key,
            "status": "PREFILLED_PENDING_CONFIRMATION",
            "evidence": expected if key == RIGHTS else None,
            "prefill_evidence": expected if key == RIGHTS else None,
            "confirmed_by": None,
            "confirmed_at": None,
        }
        for key in sorted(HARM_FIELDS)
    ]


def test_no_context_row_gives_four_blank_fields(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(text("DELETE FROM harm_context"))
    event_id = post(client).json()["flagged_event_id"]
    assert get(client, f"/v1/events/{event_id}").json()["harm_fields"] == {
        key: pending() for key in HARM_FIELDS
    }
    assert [
        (r["evidence"], r["prefill_evidence"]) for r in stored_fields(owner_engine, event_id)
    ] == [(None, None)] * 4


def test_context_for_another_system_is_not_used(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO monitored_system"
                " (system_id, sampling_interval_s, gap_timeout_intervals, last_received_at)"
                " VALUES ('sys-7', 300, 3, :t)"
            ),
            {"t": T0},
        )
        conn.execute(
            text(
                "INSERT INTO harm_context (system_id, field_key, context_text)"
                " VALUES ('sys-7', 'health_safety_harm', 'sys-7 text')"
            )
        )
    event_id = post(client).json()["flagged_event_id"]
    fields = get(client, f"/v1/events/{event_id}").json()["harm_fields"]
    assert fields["health_safety_harm"] == pending()
    assert fields[RIGHTS] == pending(EXAMPLE_1_EVIDENCE)


def test_a_series_join_adds_no_harm_fields(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    first = post(client).json()["flagged_event_id"]
    fake_clock.advance(300)
    joined = post(client, value=0.35, timestamp="2026-09-27T10:10:00Z").json()
    assert joined["flagged_event_id"] == first
    assert len(rows(owner_engine, "SELECT 1 FROM harm_field")) == 4
    # The pre-fill is from the first sample; the join does not rewrite it.
    [rights] = rows(owner_engine, "SELECT evidence FROM harm_field WHERE field_key = :k", k=RIGHTS)
    assert rights == {"evidence": EXAMPLE_1_EVIDENCE}


def test_gap_events_are_unchanged(
    client: ApiClient, app_engine: Engine, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 1.3: gap events keep four NOT_APPLICABLE fields with null evidence and confirmer,
    even for a system that has context."""
    fake_clock.advance(900)
    [gap] = gaps.sweep(app_engine)
    assert stored_fields(owner_engine, gap) == [
        {
            "field_key": key,
            "status": "NOT_APPLICABLE",
            "evidence": None,
            "prefill_evidence": None,
            "confirmed_by": None,
            "confirmed_at": None,
        }
        for key in sorted(HARM_FIELDS)
    ]


def test_event_flagged_payload_and_the_example_1_response_are_unchanged(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    response = post(client)
    assert response.status_code == 201
    body = response.json()
    assert set(body) == {"sample_id", "flagged_event_id"}
    event_id = body["flagged_event_id"]
    assert rows(
        owner_engine,
        "SELECT entry_type, actor_id, subject_id, payload FROM ledger_entry ORDER BY seq",
    )[1:] == [
        {
            "entry_type": "EVENT_FLAGGED",
            "actor_id": "sys-42",
            "subject_id": event_id,
            "payload": {
                "event_id": event_id,
                "system_id": "sys-42",
                "metric_name": "accuracy_drift",
                "observed_value": "0.34",
                "threshold_value": "0.30",
                "threshold_version_id": "thr-1",
            },
        },
        {
            "entry_type": "NOTIFICATION_REQUESTED",
            "actor_id": "system",
            "subject_id": event_id,
            "payload": {
                "notification_id": rows(owner_engine, "SELECT notification_id FROM notification")[
                    0
                ]["notification_id"],
                "event_id": event_id,
                "notification_type": "flag_notification",
            },
        },
    ]


def test_prefill_evidence_is_never_in_a_response_body(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    posted = post(client)
    event_id = posted.json()["flagged_event_id"]
    bodies = [
        posted.text,
        get(client, f"/v1/events/{event_id}").text,
        get(client, f"/v1/events/{event_id}", actor="rev-1").text,
        get(client, "/v1/events").text,
        get(client, f"/v1/events/{event_id}/audit").text,
    ]
    for body in bodies:
        assert "prefill_evidence" not in body


@pytest.mark.parametrize(
    ("remove_context", "expected"), [(False, "prefilled=1 blank=3"), (True, "prefilled=0 blank=4")]
)
def test_the_debug_line_has_the_counts_and_no_text(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    caplog: pytest.LogCaptureFixture,
    remove_context: bool,
    expected: str,
) -> None:
    if remove_context:
        with owner_engine.begin() as conn:
            conn.execute(text("DELETE FROM harm_context"))
    # Every api logger at DEBUG, so the no-text check covers the detection and request lines too.
    caplog.set_level(logging.DEBUG, logger="api")
    event_id = post(client).json()["flagged_event_id"]
    lines = [r.getMessage() for r in caplog.records if r.name == "api.review"]
    assert lines == [f"prefill: event_id={event_id} {expected}"]
    assert [r.levelno for r in caplog.records if r.name == "api.review"] == [logging.DEBUG]
    assert CONTEXT_TEXT not in caplog.text
    assert "degraded performance" not in caplog.text
    assert "is above the limit" not in caplog.text


def test_observed_and_limit_values_keep_their_digits(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Plain decimal strings, as in the EVENT_FLAGGED payload: 0.30 stays 0.30 and a value
    past the limit only in Decimal is written in full."""
    content = json.dumps(metric(value="VALUE")).replace('"VALUE"', "0.30000000000000000001")
    event_id = post_raw(client, content).json()["flagged_event_id"]
    [rights] = rows(owner_engine, "SELECT evidence FROM harm_field WHERE field_key = :k", k=RIGHTS)
    assert rights["evidence"].startswith(
        "accuracy_drift = 0.30000000000000000001 is above the limit 0.30 (threshold thr-1)"
        " at 2026-09-27T10:05:00Z. sys-42 makes"
    ), event_id


# Spec 1.3 per key: a text for each field of sys-42 (the rights text is the seeded one).
OWN_TEXTS = {
    "health_safety_harm": "sys-42 text for health and safety (4f1a).",
    "critical_infrastructure_disruption": "sys-42 text for critical infrastructure (8b2c).",
    RIGHTS: CONTEXT_TEXT,
    "property_environmental_damage": "sys-42 text for property and the environment (c3d9).",
}


def test_each_field_gets_its_own_context_text(
    client: ApiClient, owner_engine: Engine, fake_clock: FakeClock
) -> None:
    """Spec 1.3 steps 1 and 3 for each key: with a context row for all four fields, each field
    gets the template followed by its own row's text, and another system's row is not used."""
    with owner_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO monitored_system"
                " (system_id, sampling_interval_s, gap_timeout_intervals, last_received_at)"
                " VALUES ('sys-7', 300, 3, :t)"
            ),
            {"t": T0},
        )
        insert = text(
            "INSERT INTO harm_context (system_id, field_key, context_text)"
            " VALUES (:system_id, :key, :text)"
        )
        for key, own in OWN_TEXTS.items():
            if key != RIGHTS:
                conn.execute(insert, {"system_id": "sys-42", "key": key, "text": own})
        conn.execute(
            insert, {"system_id": "sys-7", "key": RIGHTS, "text": "sys-7 text, never for sys-42."}
        )
    event_id = post_ac07(client, fake_clock)
    stored = {row.pop("field_key"): row for row in stored_fields(owner_engine, event_id)}
    for key, own in OWN_TEXTS.items():
        expected = (
            "accuracy = 0.61 is below the limit 0.70 (threshold thr-2) at 2026-10-05T09:00:00Z. "
            + own
        )
        assert stored[key] == {
            "status": "PREFILLED_PENDING_CONFIRMATION",
            "evidence": expected,
            "prefill_evidence": expected,
            "confirmed_by": None,
            "confirmed_at": None,
        }, key


@pytest.mark.parametrize(
    ("literal", "changes", "at", "sentence", "payload_value"),
    [
        (
            "1e-7",
            {
                "metric_name": "accuracy",
                "metric_type": "performance",
                "timestamp": "2026-10-05T09:00:00Z",
            },
            AC07_AT,
            "accuracy = 0.0000001 is below the limit 0.70 (threshold thr-2)"
            " at 2026-10-05T09:00:00Z.",
            "0.0000001",
        ),
        (
            "3.4E-1",
            {},
            T0,
            "accuracy_drift = 0.34 is above the limit 0.30 (threshold thr-1)"
            " at 2026-09-27T10:05:00Z.",
            "0.34",
        ),
    ],
    ids=["1e-7", "3.4E-1"],
)
def test_exponent_form_values_are_written_as_plain_decimals(
    client: ApiClient,
    owner_engine: Engine,
    fake_clock: FakeClock,
    literal: str,
    changes: dict[str, Any],
    at: datetime,
    sentence: str,
    payload_value: str,
) -> None:
    """Spec 1.3: values are plain decimal strings, also when the sender writes a JSON number in
    exponent form (parsed as Decimal("1E-7")), and the same string as the EVENT_FLAGGED payload.
    The 201 body carries no value."""
    fake_clock.set(at)
    content = json.dumps(metric(value="VALUE", **changes)).replace('"VALUE"', literal)
    response = post_raw(client, content)
    assert response.status_code == 201
    assert set(response.json()) == {"sample_id", "flagged_event_id"}
    event_id = response.json()["flagged_event_id"]
    [rights] = rows(
        owner_engine,
        "SELECT evidence FROM harm_field WHERE event_id = :e AND field_key = :k",
        e=event_id,
        k=RIGHTS,
    )
    assert rights["evidence"] == f"{sentence} {CONTEXT_TEXT}"
    [flagged] = rows(
        owner_engine,
        "SELECT payload FROM ledger_entry WHERE entry_type = 'EVENT_FLAGGED' AND subject_id = :e",
        e=event_id,
    )
    assert flagged["payload"]["observed_value"] == payload_value
