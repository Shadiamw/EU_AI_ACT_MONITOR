"""The console's Phase 5 panels, checked on the files the API serves under /ui (spec 7.1 and
11.4; audit F-02 and F-10). String checks only: the on-screen behaviour is manual check M2.

- "Report and submission": get the report, download the PDF, list versions, record a
  submission, each with its element and click handler.
- "Ledger operations and health": the dashboard, the verification history and a manual run;
  the Health view polls GET /v1/ops/dashboard every 30000 ms while the panel is on screen and
  the tab is visible, stops when it is hidden or a call fails, never runs two calls at once and
  has its own timer, separate from the 1500 ms suite poll.
"""

from __future__ import annotations

import re

import pytest

from app.main import create_app
from tests.api.conftest import ApiClient

REPORT_IDS = (
    "report-json",
    "report-pdf",
    "report-list",
    "report-banner",
    "report-summary",
    "submission-type",
    "submission-next",
    "submission-ref",
    "report-submit",
    "report-out",
)
HEALTH_IDS = (
    "ops-dashboard",
    "ops-verifications",
    "ops-verify-now",
    "opshealth-summary",
    "opshealth-out",
)


@pytest.fixture(scope="module")
def served() -> dict[str, str]:
    with ApiClient(create_app()) as client:
        pages = {name: client.get(f"/ui/{name}") for name in ("", "app.js")}
    for name, response in pages.items():
        assert response.status_code == 200, name
    return {"index.html": pages[""].text, "app.js": pages["app.js"].text}


def _section(html: str, css_class: str) -> str:
    start = html.index(f'<section class="panel {css_class}"')
    return html[start : html.index("</section>", start)]


def _function(js: str, name: str) -> str:
    start = js.index(f"function {name}(")
    return js[start : js.index("\n  }\n", start)]


def test_the_report_and_submission_panel_has_its_controls_and_handlers(
    served: dict[str, str],
) -> None:
    panel = _section(served["index.html"], "reports")
    assert "Report and submission" in panel
    for element in REPORT_IDS:
        assert f'id="{element}"' in panel, element
    js = served["app.js"]
    for button in ("report-json", "report-pdf", "report-list"):
        assert f'document.getElementById("{button}").addEventListener("click"' in js, button
    assert 'const reportSubmit = document.getElementById("report-submit");' in js
    assert 'reportSubmit.addEventListener("click"' in js
    assert "/report/submission" in js and "format=pdf" in js


def test_the_health_panel_has_its_controls_and_handlers(served: dict[str, str]) -> None:
    panel = _section(served["index.html"], "ops-health")
    assert "Ledger operations and health" in panel
    for element in HEALTH_IDS:
        assert f'id="{element}"' in panel, element
    js = served["app.js"]
    for button in HEALTH_IDS[:3]:
        assert f'document.getElementById("{button}").addEventListener("click"' in js, button
    assert '"/v1/ops/dashboard"' in js and '"/v1/ledger/verifications"' in js


def test_the_health_view_polls_the_dashboard_every_30_seconds(served: dict[str, str]) -> None:
    js = served["app.js"]
    assert "const HEALTH_POLL_MS = 30000;" in js
    assert js.count("setInterval(") == 2
    start = _function(js, "startHealthPoll")
    assert "healthTimer = setInterval(pollDashboard, HEALTH_POLL_MS);" in start
    assert '"/v1/ops/dashboard"' in _function(js, "loadDashboard")
    stop = _function(js, "stopHealthPoll")
    assert "clearInterval(healthTimer);" in stop and "healthTimer = null;" in stop
    poll = _function(js, "pollDashboard")
    # One call at a time; a failed or unreachable call stops the poll.
    assert "if (healthInFlight) return;" in poll
    assert re.search(r"if \(!ok\) stopHealthPoll\(\);", poll)
    assert re.search(r"catch[^{]*\{[^}]*stopHealthPoll\(\);", poll, re.S)
    # Runs only while the panel is on screen and the tab is visible; stops otherwise.
    sync = _function(js, "syncHealthPoll")
    assert "healthVisible()" in sync and "stopHealthPoll();" in sync
    visible = _function(js, "healthVisible")
    assert 'document.visibilityState === "visible"' in visible
    assert "new IntersectionObserver(" in js
    assert 'document.addEventListener("visibilitychange", syncHealthPoll);' in js


def test_the_health_poll_and_the_suite_poll_never_share_a_timer(served: dict[str, str]) -> None:
    js = served["app.js"]
    suite_start = _function(js, "startPolling")
    assert "}, 1500);" in suite_start and "healthTimer" not in suite_start
    assert "healthTimer" not in _function(js, "stopPolling")
    for name in ("startHealthPoll", "stopHealthPoll", "pollDashboard", "syncHealthPoll"):
        assert "pollTimer" not in _function(js, name), name
