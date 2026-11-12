"""Phase 5 Part 3, M8 follow-up: the two Part 3 settings are checked at start-up (decision 302).

NIGHTLY_VERIFY_HOUR_UTC must be an integer 0 to 23 and ALERT_DEADLINE_WARNING_HOURS a positive
integer. A bad value stops the API and the worker at start-up, the same way a bad
ESCALATION_PERCENTAGES or DEADLINE_CALENDAR does (tests/part3/test_escalation_schema.py,
tests/part2/test_deadlines.py); unset, both still start with the defaults 2 and 24. One approved
test beyond the 18 spec tests.
"""

import pytest

from app import config, worker
from app.main import create_app


def start_worker(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """worker.main() up to its loop: the loop is replaced, so a start that passes the checks
    returns instead of running forever."""
    started: list[object] = []
    monkeypatch.setattr(worker, "run_forever", started.append)
    worker.main()
    return started


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("NIGHTLY_VERIFY_HOUR_UTC", ""),
        ("NIGHTLY_VERIFY_HOUR_UTC", "two"),
        ("NIGHTLY_VERIFY_HOUR_UTC", "2.5"),
        ("NIGHTLY_VERIFY_HOUR_UTC", "-1"),
        ("NIGHTLY_VERIFY_HOUR_UTC", "24"),
        ("NIGHTLY_VERIFY_HOUR_UTC", "100"),
        ("ALERT_DEADLINE_WARNING_HOURS", ""),
        ("ALERT_DEADLINE_WARNING_HOURS", "twelve"),
        ("ALERT_DEADLINE_WARNING_HOURS", "1.5"),
        ("ALERT_DEADLINE_WARNING_HOURS", "-24"),
        ("ALERT_DEADLINE_WARNING_HOURS", "0"),
    ],
)
def test_bad_phase5_settings_fail_at_startup(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    # Unset: the defaults, and both processes start.
    monkeypatch.delenv("NIGHTLY_VERIFY_HOUR_UTC", raising=False)
    monkeypatch.delenv("ALERT_DEADLINE_WARNING_HOURS", raising=False)
    assert (config.nightly_verify_hour_utc(), config.alert_deadline_warning_hours()) == (2, 24)
    create_app()
    assert len(start_worker(monkeypatch)) == 1

    # A bad value stops both at start-up, naming the setting.
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        create_app()
    with pytest.raises(RuntimeError, match=name):
        start_worker(monkeypatch)
