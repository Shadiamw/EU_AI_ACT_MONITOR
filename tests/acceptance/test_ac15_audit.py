"""AC-15 (REQ-14): one user views an event and a second authorized user edits it; both actions
are logged with the right identity and timestamp, including the view-only action.

Spec section 7: viewer-1 views and rev-1 edits. Phase 3 spec 2.6 and 2.7: the event-detail GET
appends EVENT_VIEWED with the caller's actor id; GET .../audit returns {actor_id, action,
timestamp} by seq and then appends AUDIT_VIEWED (not in its own response). Spec 2.10: the edit
is DISPOSITION_RECORDED by the reviewer. Timestamps are the ledger's recorded_at (fixed
milliseconds), taken from the simulated clock, so each is bracketed by clock reads.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from tests.acceptance.api import Api, parse_ledger_time

pytestmark = pytest.mark.integration

MS = timedelta(milliseconds=1)


def _within(stamp: str, start: datetime, end: datetime) -> bool:
    # recorded_at is truncated or rounded to milliseconds; allow one either side.
    return start - MS <= parse_ledger_time(stamp) <= end + MS


def test_a_view_only_action_is_logged_with_the_viewer_and_the_time(api: Api) -> None:
    event_id, _ = api.flag()
    start = api.now()
    api.event(event_id, role="viewer")
    end = api.now()

    trail = api.audit(event_id, role="reviewer")
    assert all(set(row) == {"actor_id", "action", "timestamp"} for row in trail)
    views = [row for row in trail if row["action"] == "EVENT_VIEWED"]
    assert len(views) == 1, trail
    assert views[0]["actor_id"] == "viewer-1"
    assert _within(views[0]["timestamp"], start, end)
    assert [row["action"] for row in trail][:2] == ["EVENT_FLAGGED", "NOTIFICATION_REQUESTED"]

    # The audit read itself is logged, and shows on the next read (Phase 3 spec 2.7).
    again = api.audit(event_id, role="viewer")
    assert [(row["actor_id"], row["action"]) for row in again][-1] == ("rev-1", "AUDIT_VIEWED")


def test_a_view_by_one_user_and_an_edit_by_another_are_both_logged(api: Api) -> None:
    event_id, _ = api.flag()
    view_start = api.now()
    api.event(event_id, role="viewer")
    view_end = api.now()
    edit_start = api.now()
    api.disposed(event_id, "OTHER_SERIOUS")
    edit_end = api.now()

    trail = api.audit(event_id, role="viewer")
    view = [row for row in trail if row["action"] == "EVENT_VIEWED"]
    edit = [row for row in trail if row["action"] == "DISPOSITION_RECORDED"]
    assert len(view) == 1 and len(edit) == 1, trail
    assert view[0]["actor_id"] == "viewer-1"
    assert edit[0]["actor_id"] == "rev-1"
    assert _within(view[0]["timestamp"], view_start, view_end)
    assert _within(edit[0]["timestamp"], edit_start, edit_end)
    assert trail.index(view[0]) < trail.index(edit[0])
