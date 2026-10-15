"""Environment configuration (spec: Configuration table)."""

import os


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
