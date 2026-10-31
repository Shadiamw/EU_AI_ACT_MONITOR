"""Environment configuration (spec: Configuration table)."""

import os
import re
from datetime import date


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"environment variable {name} is required")
    return value


def database_url() -> str:
    """Connection for the application role (INSERT and SELECT only on the ledger)."""
    return _required("DATABASE_URL")


def database_admin_url() -> str:
    """Owner role. Used by migrations and, in test only, by reset."""
    return _required("DATABASE_ADMIN_URL")


def ingest_key_sys_42() -> str:
    """INGEST_KEY_SYS_42 (required): the seeded SYSTEM key for sys-42. Only its SHA-256 digest
    is stored (spec 1.3, 5). Read by the seed, never logged."""
    return _required("INGEST_KEY_SYS_42")


def reviewer_key() -> str:
    """REVIEWER_KEY (required): the seeded REVIEWER key for rev-1."""
    return _required("REVIEWER_KEY")


def viewer_key() -> str:
    """VIEWER_KEY (required): the seeded VIEWER key for viewer-1."""
    return _required("VIEWER_KEY")


# Part 3 (worker) settings, from spec 5 'Configuration (environment variables)'. Each is read on
# every call so tests can change the environment.

LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def environment() -> str:
    """ENVIRONMENT (required): `test` enables test-only behaviour; anything else disables it."""
    return _required("ENVIRONMENT")


def is_test_environment() -> bool:
    return environment() == "test"


def _positive_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise RuntimeError(f"environment variable {name} must be an integer") from None
    if value < 1:
        raise RuntimeError(f"environment variable {name} must be at least 1")
    return value


def sampling_interval_seconds() -> int:
    """SAMPLING_INTERVAL_SECONDS (300). Only seeds sys-42's sampling_interval_s (Part 4); the
    sweeper reads the monitored_system row, never this variable."""
    return _positive_int("SAMPLING_INTERVAL_SECONDS", 300)


def gap_timeout_intervals() -> int:
    """GAP_TIMEOUT_INTERVALS (3). Only seeds sys-42's gap_timeout_intervals (Part 4)."""
    return _positive_int("GAP_TIMEOUT_INTERVALS", 3)


def notify_max_delay_seconds() -> int:
    """NOTIFY_MAX_DELAY_SECONDS (900): the REQ-04 bound. It judges lateness; it never delays
    sending."""
    return _positive_int("NOTIFY_MAX_DELAY_SECONDS", 900)


def sweep_interval_seconds() -> int:
    """SWEEP_INTERVAL_SECONDS (60): the gap sweeper's period, in real time."""
    return _positive_int("SWEEP_INTERVAL_SECONDS", 60)


def delivery_poll_seconds() -> int:
    """DELIVERY_POLL_SECONDS (5): the delivery pass's period, in real time."""
    return _positive_int("DELIVERY_POLL_SECONDS", 5)


def log_level() -> str:
    """LOG_LEVEL (INFO)."""
    raw = os.environ.get("LOG_LEVEL") or "INFO"
    level = raw.upper()
    if level not in LOG_LEVELS:
        raise RuntimeError(f"environment variable LOG_LEVEL must be one of {', '.join(LOG_LEVELS)}")
    return level


def cors_origins() -> list[str]:
    """CORS_ORIGINS: comma-separated browser origins allowed to call the API.

    Unset in ENVIRONMENT=test defaults to `*` so the Vercel UI can reach a local or
    tunneled API. Outside test, unset means no CORS middleware (same-origin only).
    """
    raw = os.environ.get("CORS_ORIGINS")
    if raw is None or raw.strip() == "":
        return ["*"] if is_test_environment() else []
    if raw.strip() == "*":
        return ["*"]
    return [part.strip() for part in raw.split(",") if part.strip()]


# Phase 4 slots (Phase 4 Addendum 9.1). Each owner writes its settings between its own two
# marker lines, and edits nothing else in this file.

# Phase 4 Part 2 slot: DEADLINE_CALENDAR and DEADLINE_HOLIDAYS (Phase 4 spec 8).

DEADLINE_CALENDARS = ("calendar_days", "working_days")
_HOLIDAY = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


def deadline_calendar() -> str:
    """DEADLINE_CALENDAR (calendar_days): calendar_days or working_days (REQ-10). Anything else
    is an error, which stops start-up (app.main checks it)."""
    raw = os.environ.get("DEADLINE_CALENDAR")
    if raw is None or raw == "":
        return "calendar_days"
    if raw not in DEADLINE_CALENDARS:
        raise RuntimeError(
            "environment variable DEADLINE_CALENDAR must be calendar_days or working_days"
        )
    return raw


def deadline_holidays() -> tuple[date, ...]:
    """DEADLINE_HOLIDAYS (empty): comma-separated YYYY-MM-DD dates that are not working days,
    sorted and without repeats. Spaces around a date are allowed; an empty item or any other
    form is an error (Part 2 decision 176). Checked whatever the calendar, so a typo is found at
    start-up and not when the calendar is switched."""
    raw = os.environ.get("DEADLINE_HOLIDAYS")
    if raw is None or raw.strip() == "":
        return ()
    holidays = set()
    for item in raw.split(","):
        item = item.strip()
        if not _HOLIDAY.fullmatch(item):
            raise RuntimeError("environment variable DEADLINE_HOLIDAYS must be YYYY-MM-DD dates")
        try:
            holidays.add(date.fromisoformat(item))
        except ValueError:
            raise RuntimeError(
                "environment variable DEADLINE_HOLIDAYS must be YYYY-MM-DD dates"
            ) from None
    return tuple(sorted(holidays))


# End of the Phase 4 Part 2 slot.

# Phase 4 Part 3 slot: ESCALATION_PERCENTAGES (Phase 4 spec 8).
# End of the Phase 4 Part 3 slot.
