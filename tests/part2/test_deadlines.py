"""Phase 4 Part 2: the deadline engine (spec 2.5 and 6), the calendar settings (spec 8) and the
revision rule of Addendum rule 1. Pure functions: no database and no clock, except the one
start-up test that builds the app.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app import config, deadlines
from app.main import create_app

ANCHOR = datetime(2026, 9, 27, 10, 12, tzinfo=UTC)  # a Sunday
CALENDAR_DAYS = "calendar_days"
WORKING_DAYS = "working_days;holidays="


def at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def test_the_tier_days_and_the_four_tiers() -> None:
    assert deadlines.TIER_DAYS == {
        "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD": 2,
        "DEATH": 10,
        "OTHER_SERIOUS": 15,
    }
    assert deadlines.TIERS == (
        "DEATH",
        "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD",
        "OTHER_SERIOUS",
        "NOT_SERIOUS",
    )


# Spec 2.5, worked examples (Pinned; required tests), the calendar-days rows.
@pytest.mark.parametrize(
    ("tier", "expected"),
    [
        ("OTHER_SERIOUS", "2026-10-12T10:12:00Z"),  # contract Example 2
        ("CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "2026-09-29T10:12:00Z"),  # AC-11, 48 h
        ("DEATH", "2026-10-07T10:12:00Z"),  # AC-11, 10 days
    ],
)
def test_calendar_days_worked_examples(tier: str, expected: str) -> None:
    deadline = deadlines.compute(ANCHOR, tier, CALENDAR_DAYS)
    assert deadline == at(expected)
    assert deadline - ANCHOR == timedelta(days=deadlines.TIER_DAYS[tier])


def test_not_serious_never_has_a_deadline() -> None:
    assert deadlines.compute(ANCHOR, "NOT_SERIOUS", CALENDAR_DAYS) is None
    assert deadlines.compute(ANCHOR, "NOT_SERIOUS", WORKING_DAYS) is None


def test_working_days_from_a_sunday() -> None:
    """Spec 2.5 worked example: Sunday 2026-09-27T10:12:00Z, OTHER_SERIOUS, no holidays."""
    assert deadlines.compute(ANCHOR, "OTHER_SERIOUS", WORKING_DAYS) == at("2026-10-16T10:12:00Z")


def test_working_days_with_holidays() -> None:
    """Spec 2.5 worked example: Thursday 2026-12-24T13:00:00Z, the 2-day tier, holidays
    2026-12-25 and 2026-12-26 (the second is a Saturday anyway)."""
    calendar = "working_days;holidays=2026-12-25,2026-12-26"
    anchor = at("2026-12-24T13:00:00Z")
    expected = at("2026-12-29T13:00:00Z")
    assert deadlines.compute(anchor, "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", calendar) == expected
    # Without the holidays the same anchor ends on Monday 28 December.
    assert deadlines.compute(anchor, "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", WORKING_DAYS) == at(
        "2026-12-28T13:00:00Z"
    )


@pytest.mark.parametrize(
    ("anchor", "tier", "expected"),
    [
        # Saturday: the count starts on Monday.
        ("2026-10-03T10:00:00Z", "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "2026-10-06T10:00:00Z"),
        # Friday late: the anchor's own day is never counted.
        ("2026-10-02T23:30:00Z", "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "2026-10-06T23:30:00Z"),
        # Monday: two full weeks.
        ("2026-09-28T08:00:00Z", "DEATH", "2026-10-12T08:00:00Z"),
        # A Saturday anchor with a 10-day tier ends on a Friday.
        ("2026-10-03T00:00:00Z", "DEATH", "2026-10-16T00:00:00Z"),
    ],
)
def test_working_days_from_weekend_and_weekday_anchors(
    anchor: str, tier: str, expected: str
) -> None:
    assert deadlines.compute(at(anchor), tier, WORKING_DAYS) == at(expected)


def test_working_days_skip_a_holiday_on_a_weekday() -> None:
    calendar = "working_days;holidays=2026-09-29"
    # Monday 28 counts, Tuesday 29 is a holiday, Wednesday 30 is the second working day.
    assert deadlines.compute(
        at("2026-09-27T10:12:00Z"), "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", calendar
    ) == at("2026-09-30T10:12:00Z")


def test_working_days_are_judged_on_utc_dates() -> None:
    """Sunday 23:00 at UTC-5 is Monday 04:00 UTC, so the count starts on Tuesday (spec 2.5:
    one UTC calendar day at a time, at the anchor's UTC time of day)."""
    anchor = datetime(2026, 9, 27, 23, 0, tzinfo=timezone(timedelta(hours=-5)))
    deadline = deadlines.compute(anchor, "DEATH", WORKING_DAYS)
    assert deadline == at("2026-10-12T04:00:00Z")
    assert deadline is not None and deadline.utcoffset() == timedelta(0)


def test_microseconds_are_kept() -> None:
    anchor = ANCHOR.replace(microsecond=123456)
    for calendar in (CALENDAR_DAYS, WORKING_DAYS):
        deadline = deadlines.compute(anchor, "DEATH", calendar)
        assert deadline is not None and deadline.microsecond == 123456


def test_an_unknown_calendar_or_tier_or_a_naive_anchor_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown deadline calendar"):
        deadlines.compute(ANCHOR, "DEATH", "business_days")
    with pytest.raises(KeyError):
        deadlines.compute(ANCHOR, "SERIOUS", CALENDAR_DAYS)
    with pytest.raises(ValueError, match="timezone-aware"):
        deadlines.compute(datetime(2026, 9, 27, 10, 12), "DEATH", CALENDAR_DAYS)  # noqa: DTZ001


def test_a_stricter_tier_from_the_same_anchor_is_always_the_minimum() -> None:
    """Spec 6: revision to a stricter tier gives compute(anchor, new), the minimum of the two."""
    for calendar in (CALENDAR_DAYS, WORKING_DAYS):
        ordered = [
            deadlines.compute(ANCHOR, tier, calendar)
            for tier in ("CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "DEATH", "OTHER_SERIOUS")
        ]
        assert ordered == sorted(ordered)  # type: ignore[type-var]


# Spec 8: DEADLINE_CALENDAR and DEADLINE_HOLIDAYS, and the recorded calendar string (spec 2.5).


@pytest.mark.parametrize(
    ("calendar", "holidays", "expected"),
    [
        (None, None, "calendar_days"),
        ("", "", "calendar_days"),
        ("calendar_days", "2026-12-25", "calendar_days"),
        ("working_days", None, "working_days;holidays="),
        ("working_days", " ", "working_days;holidays="),
        (
            "working_days",
            "2026-12-26, 2026-12-25,2026-12-26",
            "working_days;holidays=2026-12-25,2026-12-26",
        ),
    ],
)
def test_current_calendar(
    monkeypatch: pytest.MonkeyPatch, calendar: str | None, holidays: str | None, expected: str
) -> None:
    for name, value in (("DEADLINE_CALENDAR", calendar), ("DEADLINE_HOLIDAYS", holidays)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert deadlines.current_calendar() == expected


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("DEADLINE_CALENDAR", "Calendar_Days"),
        ("DEADLINE_CALENDAR", "working-days"),
        ("DEADLINE_CALENDAR", "business_days"),
        ("DEADLINE_HOLIDAYS", "2026-13-01"),
        ("DEADLINE_HOLIDAYS", "2026-02-30"),
        ("DEADLINE_HOLIDAYS", "26-12-25"),
        ("DEADLINE_HOLIDAYS", "20261225"),
        ("DEADLINE_HOLIDAYS", "2026-12-25,,2026-12-26"),
        ("DEADLINE_HOLIDAYS", "2026-12-25,"),
        ("DEADLINE_HOLIDAYS", "2026-12-25;2026-12-26"),
    ],
)
def test_a_malformed_setting_stops_start_up(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    """Spec 8: anything but calendar_days or working_days stops start-up with an error; a
    malformed holiday list does too (Part 2 decision 176)."""
    monkeypatch.setenv(name, value)
    with pytest.raises(RuntimeError, match=name):
        deadlines.current_calendar()
    with pytest.raises(RuntimeError, match=name):
        create_app()


def test_holidays_are_sorted_dates_without_repeats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DEADLINE_HOLIDAYS", "2027-01-01,2026-12-25,2027-01-01")
    assert [day.isoformat() for day in config.deadline_holidays()] == ["2026-12-25", "2027-01-01"]


# Addendum rule 1: which calls need a rationale. The rows of the rule's table, plus every pair
# of serious tiers after a first serious disposition.


@pytest.mark.parametrize(
    ("prior", "new", "needs_rationale"),
    [
        (["DEATH", "NOT_SERIOUS"], "OTHER_SERIOUS", True),
        (["DEATH", "NOT_SERIOUS"], "DEATH", False),
        (["DEATH", "NOT_SERIOUS"], "CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", False),
        (["OTHER_SERIOUS", "DEATH"], "OTHER_SERIOUS", True),
        (["NOT_SERIOUS"], "OTHER_SERIOUS", False),
        (["NOT_SERIOUS", "NOT_SERIOUS"], "OTHER_SERIOUS", False),
        ([], "OTHER_SERIOUS", False),  # a first disposition
        (["CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD"], "NOT_SERIOUS", False),  # 400 rule, not 409
        # The most recent serious tier counts, not the strictest one ever.
        (["CRITICAL_INFRASTRUCTURE_OR_WIDESPREAD", "OTHER_SERIOUS"], "DEATH", False),
        (["DEATH", "NOT_SERIOUS", "OTHER_SERIOUS", "NOT_SERIOUS"], "OTHER_SERIOUS", False),
        (["OTHER_SERIOUS", "NOT_SERIOUS", "DEATH", "NOT_SERIOUS"], "OTHER_SERIOUS", True),
    ],
)
def test_the_addendum_rule_1_cases(prior: list[str], new: str, needs_rationale: bool) -> None:
    assert deadlines.extends_deadline(prior, new) is needs_rationale


@pytest.mark.parametrize("previous", list(deadlines.TIER_DAYS))
@pytest.mark.parametrize("new", list(deadlines.TIER_DAYS))
def test_only_a_longer_window_needs_a_rationale(previous: str, new: str) -> None:
    longer = deadlines.TIER_DAYS[new] > deadlines.TIER_DAYS[previous]
    assert deadlines.extends_deadline([previous], new) is longer


def test_the_most_recent_serious_tier_is_read_from_the_rows() -> None:
    assert deadlines.most_recent_serious_tier([]) is None
    assert deadlines.most_recent_serious_tier(["NOT_SERIOUS"]) is None
    assert deadlines.most_recent_serious_tier(["DEATH", "NOT_SERIOUS"]) == "DEATH"
    assert (
        deadlines.most_recent_serious_tier(["DEATH", "OTHER_SERIOUS", "NOT_SERIOUS"])
        == "OTHER_SERIOUS"
    )
