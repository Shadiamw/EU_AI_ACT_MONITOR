"""Phase 5 spec 5.4 (D10): the simulated time and a sample timestamp stop at 9999-07-01."""

from datetime import UTC, datetime

import pytest
from sqlalchemy import Engine, text

from app import clock, platform
from app.formats import TIME_LIMIT
from app.main import create_app
from tests.api.conftest import ApiClient
from tests.db_helpers import bearer

ADVANCE = "/v1/test/clock/advance"
RESET = "/v1/test/reset"
MALFORMED = {"error_code": "MALFORMED_BODY", "message": "the request is malformed"}


def _seconds_until(moment: datetime) -> int:
    return int((moment - clock.wall_now()).total_seconds())


def _breach(client: ApiClient, timestamp: str) -> int:
    return client.request(
        "POST",
        "/v1/metrics",
        headers=bearer("sys-42"),
        json={
            "system_id": "sys-42",
            "metric_name": "accuracy",
            "metric_type": "performance",
            "value": 0.61,
            "timestamp": timestamp,
        },
    ).status_code


def test_the_limit_is_the_first_of_july_9999() -> None:
    assert datetime(9999, 7, 1, tzinfo=UTC) == TIME_LIMIT


def test_clock_advance_into_the_last_half_of_9999_is_400() -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        # A minute past the limit (the exact instant is the unit test below: the wall clock
        # moves between computing the offset and the request).
        at_limit = client.request(
            "POST",
            ADVANCE,
            headers=bearer("rev-1"),
            json={"seconds": _seconds_until(TIME_LIMIT) + 60},
        )
        assert (at_limit.status_code, at_limit.json()) == (400, MALFORMED)
        assert clock.offset_seconds() == 0
        # One second before the limit, minus a margin for the wall clock: accepted, and an
        # ingest at that time works (spec 5.4).
        before = datetime(9999, 6, 30, 23, 59, 30, tzinfo=UTC)
        ok = client.request(
            "POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": _seconds_until(before)}
        )
        assert ok.status_code == 200, ok.text
        assert ok.json()["simulated_time"].startswith("9999-06-30T23:59:")
        assert _breach(client, ok.json()["simulated_time"]) == 201
        # A further advance past the limit is still refused and changes nothing.
        offset = clock.offset_seconds()
        again = client.request("POST", ADVANCE, headers=bearer("rev-1"), json={"seconds": 3600})
        assert again.status_code == 400
        assert clock.offset_seconds() == offset
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
    assert clock.offset_seconds() == 0


def test_the_bound_is_exact_at_9999_07_01(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the wall clock fixed, the last accepted simulated time is one second before the
    limit and the limit itself is refused (spec 5.4: "at or after")."""
    wall = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
    monkeypatch.setattr(clock, "wall_now", lambda: wall)
    to_limit = int((TIME_LIMIT - wall).total_seconds())
    platform._check_representable(to_limit - 1)
    for offset in (to_limit, to_limit + 1, 2**63 - 1):
        with pytest.raises(ValueError):
            platform._check_representable(offset)
    with pytest.raises(ValueError):
        platform._check_representable(2**63)


def test_a_sample_dated_after_9999_07_01_is_400(owner_engine: Engine) -> None:
    with ApiClient(create_app()) as client:
        assert client.request("POST", RESET, headers=bearer("rev-1")).status_code == 204
        for stamp in ("9999-07-01T00:00:00Z", "9999-12-31T23:59:59Z"):
            assert _breach(client, stamp) == 400
        # The last second before the limit still works, and retain_until is representable.
        assert _breach(client, "9999-06-30T23:59:59Z") == 201
    with owner_engine.connect() as conn:
        retain = conn.execute(text("SELECT retain_until FROM event")).scalar_one()
    assert retain == datetime(9999, 12, 30, 23, 59, 59, tzinfo=UTC)
