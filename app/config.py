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


# Part 3 (worker). Every value is a whole number of seconds or a count, read on each call so
# tests can change the environment. **ASSUMPTION:** the names; the spec's Configuration table
# is not in this repository (docs/decisions.md, Part 3 decisions).


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
    """REQ-01: the metric sampling interval, 5 minutes by default."""
    return _positive_int("SAMPLING_INTERVAL_SECONDS", 300)


def gap_timeout_seconds() -> int:
    """REQ-02: silence this long opens a gap; 3 missed sampling intervals by default."""
    return _positive_int("GAP_TIMEOUT_INTERVALS", 3) * sampling_interval_seconds()


def notify_max_delay_seconds() -> int:
    """REQ-04: the maximum delay from detection to notification, 15 minutes by default."""
    return _positive_int("NOTIFY_MAX_DELAY_SECONDS", 900)


def sweep_interval_seconds() -> int:
    """How often the worker runs the gap sweeper, in wall-clock seconds: every minute."""
    return _positive_int("SWEEP_INTERVAL_SECONDS", 60)


def delivery_poll_seconds() -> int:
    """How often the worker runs the delivery pass, in wall-clock seconds."""
    return _positive_int("DELIVERY_POLL_SECONDS", 5)


def webhook_timeout_seconds() -> int:
    """How long a webhook call (delivery or verification ping) may take before it fails."""
    return _positive_int("WEBHOOK_TIMEOUT_SECONDS", 5)


def delivery_max_attempts() -> int:
    """Delivery attempts per (notification, webhook) before NOTIFICATION_FAILED."""
    return _positive_int("DELIVERY_MAX_ATTEMPTS", 5)
