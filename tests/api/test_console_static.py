"""The console reviewer panel (Phase 4 spec 4.6; decision 142), checked on the served files.

These are string checks on app/static as the API serves it under /ui. The on-screen behaviour
needs Parts 1 and 2 and is checked by hand (README 13, the console checklist).
"""

from __future__ import annotations

import re

import pytest

from app.main import create_app
from tests.api.conftest import ApiClient

PINNED_LABELS = {
    "DEATH": "Death (10-day deadline)",
    "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": (
        "Critical infrastructure disruption / widespread infringement (2-day deadline)"
    ),
    "OTHER_SERIOUS": "Other serious incident (15-day deadline)",
    "NOT_SERIOUS": "Assessed — not a serious incident (rationale required, no deadline)",
}
HARM_FIELDS = [
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
]


@pytest.fixture(scope="module")
def served() -> dict[str, str]:
    with ApiClient(create_app()) as client:
        pages = {name: client.get(f"/ui/{name}") for name in ("", "app.js", "app.css")}
    for name, response in pages.items():
        assert response.status_code == 200, name
    return {
        "index.html": pages[""].text,
        "app.js": pages["app.js"].text,
        "app.css": pages["app.css"].text,
    }


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _block(source: str, start: str, end: str) -> str:
    """The source text from `start` up to (not including) `end`."""
    begin = source.index(start)
    return source[begin : source.index(end, begin + len(start))]


def test_the_four_pinned_disposition_labels_exactly(served: dict[str, str]) -> None:
    select = _block(served["index.html"], '<select id="disposition-tier"', "</select>")
    options = dict(re.findall(r'<option value="([A-Z_]+)"[^>]*>(.*?)</option>', select, re.S))
    assert {tier: _squash(label) for tier, label in options.items()} == PINNED_LABELS


def test_harm_fields_are_in_contract_order(served: dict[str, str]) -> None:
    js = served["app.js"]
    positions = [js.index(f'"{key}"') for key in HARM_FIELDS]
    assert positions == sorted(positions)


def test_an_empty_field_is_shown_as_no_evidence_available(served: dict[str, str]) -> None:
    assert '"no evidence available"' in served["app.js"]


def test_no_harm_status_is_preselected(served: dict[str, str]) -> None:
    """Decision 5: a pre-checked CONFIRMED could record a confirmation nobody made."""
    for name in ("index.html", "app.js"):
        radios = re.findall(r'<input[^>]*type="radio"[^>]*>', served[name])
        assert all("checked" not in radio for radio in radios), name
    # A stored decision (CONFIRMED or NOT_APPLICABLE) is shown; a pending field is not.
    js = served["app.js"]
    assert "PREFILLED_PENDING_CONFIRMATION" in js


def test_review_as_defaults_to_viewer(served: dict[str, str]) -> None:
    """Decision 5: loading an event as a reviewer starts the review (D3), so it is a choice."""
    select = _block(served["index.html"], '<select id="review-as"', "</select>")
    options = re.findall(r'<option value="(\w+)"([^>]*)>', select)
    selected = [value for value, attrs in options if "selected" in attrs]
    first = options[0][0]
    assert (selected or [first]) == ["viewer"]
    assert {value for value, _ in options} == {"viewer", "reviewer"}


def test_revisions_are_rendered_from_the_contract_keys(served: dict[str, str]) -> None:
    js = served["app.js"]
    for key in ("prior_tier", "new_tier", "reviewer_id", "revised_at"):
        assert f"rev.{key}" in js, key
    assert "rev.tier" not in js
    assert "rev.assigned_at" not in js


def test_history_has_a_revisions_table(served: dict[str, str]) -> None:
    assert 'id="revisions-table"' in served["index.html"]


def test_elapsed_time_uses_the_simulated_now_and_only_on_an_event_load(
    served: dict[str, str],
) -> None:
    js = served["app.js"]
    helper = _block(js, "async function simulatedNow(", "\n  }\n")
    # The clock read: an advance of 0 (Addendum rule 7), with the key the user chose.
    assert '"/v1/test/clock/advance"' in helper
    assert "seconds: 0" in helper
    assert "auth: reviewAuth()" in helper
    # Outside the test environment (404) or on any failure, the browser clock.
    assert "Date.now()" in helper
    # Called once, from the event-load handler, never from a timer or a poll.
    assert js.count("simulatedNow(") == 2
    load = _block(js, 'getElementById("load-event")', 'getElementById("submit-disposition")')
    assert "await simulatedNow()" in load
    for timer in ("setInterval", "setTimeout"):
        for match in re.finditer(timer, js):
            assert "simulatedNow" not in js[match.start() : match.start() + 400]
    risk = _block(js, "function renderRisk(", "\n  }\n")
    assert "Date.now()" not in risk


def test_the_risk_indicator_text_follows_the_spec(served: dict[str, str]) -> None:
    js = served["app.js"]
    assert "Detected at ${event.detected_at}: ${h} h ${m} min elapsed; " in js
    assert '"the legal window may already be running"' in js
    assert "Detected ${event.detected_at}, classified ${disp.assigned_at}: " in js
    assert "${h} h ${m} min elapsed before classification" in js


def test_the_timeline_uses_the_part3_formula(served: dict[str, str]) -> None:
    timeline = _block(served["app.js"], "function escalationInstants(", "\n  }\n")
    assert "[50, 80, 100]" in timeline
    assert "Math.ceil(" in timeline


def test_api_errors_are_shown_with_error_code_and_message(served: dict[str, str]) -> None:
    js = served["app.js"]
    formatter = _block(js, "function describeError(", "\n  }\n")
    assert "error_code" in formatter and "message" in formatter
    assert js.count("describeError(") >= 3


def test_the_suite_panel_offers_no_full_suite(served: dict[str, str]) -> None:
    html = served["index.html"]
    assert 'data-suite="full"' not in html
    assert set(re.findall(r'data-suite="(\w+)"', html)) == {"smoke", "acceptance", "refresh"}
