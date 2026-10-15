"""Formats shared by Part 3: ids, API timestamps and the retention date.

retain_until expectations are worked out by hand from spec 1.5 / 3.4: "detected time plus six
calendar months (at least 183 days)", so the later of the two dates.
"""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.formats import add_calendar_months, api_time, new_id, retain_until, whole_seconds


def utc(
    year: int, month: int, day: int, hour: int = 0, minute: int = 0, second: int = 0
) -> datetime:
    return datetime(year, month, day, hour, minute, second, tzinfo=UTC)


@pytest.mark.parametrize(
    ("detected", "expected"),
    [
        # 1 Sep 2026 + 6 months = 1 Mar 2027, only 181 days; +183 days = 3 Mar 2027.
        (utc(2026, 9, 1, 12, 0, 0), utc(2027, 3, 3, 12, 0, 0)),
        # 1 Oct 2026 + 6 months = 1 Apr 2027 (182 days); +183 days = 2 Apr 2027.
        (utc(2026, 10, 1, 9, 0, 0), utc(2027, 4, 2, 9, 0, 0)),
        # 31 Aug 2026 + 6 months = 28 Feb 2027 (day clamped, 181 days); +183 = 2 Mar 2027.
        (utc(2026, 8, 31, 23, 59, 59), utc(2027, 3, 2, 23, 59, 59)),
        # 1 Mar 2026 + 6 months = 1 Sep 2026, 184 days: the calendar date wins.
        (utc(2026, 3, 1, 0, 0, 0), utc(2026, 9, 1, 0, 0, 0)),
        # 27 Sep 2026 (contract Example 1) + 6 months = 27 Mar 2027, 181 days; +183 = 29 Mar.
        (utc(2026, 9, 27, 10, 5, 0), utc(2027, 3, 29, 10, 5, 0)),
        # 31 Jul 2027 + 6 months = 31 Jan 2028, 184 days: the calendar date wins.
        (utc(2027, 7, 31, 8, 0, 0), utc(2028, 1, 31, 8, 0, 0)),
    ],
)
def test_retain_until_is_the_later_of_six_months_and_183_days(
    detected: datetime, expected: datetime
) -> None:
    assert retain_until(detected) == expected
    assert retain_until(detected) - detected >= timedelta(days=183)


def test_add_calendar_months_clamps_to_the_month_end() -> None:
    assert add_calendar_months(utc(2027, 8, 31), 6) == utc(2028, 2, 29)  # leap year
    assert add_calendar_months(utc(2026, 12, 15), 6) == utc(2027, 6, 15)


def test_api_time_has_a_fraction_only_when_present() -> None:
    """Spec 4: YYYY-MM-DDTHH:MM:SSZ, with fractional seconds only if the value has them."""
    assert api_time(utc(2026, 9, 27, 10, 5, 0)) == "2026-09-27T10:05:00Z"
    assert api_time(datetime(2026, 9, 27, 10, 5, 0, 250000, tzinfo=UTC)) == (
        "2026-09-27T10:05:00.25Z"
    )
    plus_two = timezone(timedelta(hours=2))
    assert api_time(datetime(2026, 9, 27, 12, 5, 0, tzinfo=plus_two)) == "2026-09-27T10:05:00Z"
    with pytest.raises(ValueError):
        api_time(datetime(2026, 9, 27, 10, 5, 0))  # noqa: DTZ001 - the naive value under test


def test_ids_and_whole_seconds() -> None:
    assert new_id("evt").startswith("evt-") and len(new_id("evt")) == 4 + 32
    assert whole_seconds(utc(2026, 1, 1), utc(2026, 1, 1) + timedelta(seconds=98.9)) == 98
    assert whole_seconds(utc(2026, 1, 1), utc(2025, 12, 31)) == 0
