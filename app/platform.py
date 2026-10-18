"""Part 4 platform operations: test reset and clock advance (spec 4.6, decision 133)."""

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app import clock, config, gaps, notifications, observability
from tools.seed_part1 import seed as install_seed

# Truncate order is free under CASCADE. Owner role only.
RESET_TABLES = (
    "notification_delivery",
    "notification",
    "webhook",
    "harm_field",
    "event",
    "metric_sample",
    "breach_series",
    "ledger_entry",
    "monitored_system",
    "sweeper_heartbeat",
    "api_key",
    "threshold_version",
    "clock_state",
)


def _admin_engine() -> Engine:
    return create_engine(config.database_admin_url(), poolclass=NullPool)


def require_test_environment() -> None:
    if not config.is_test_environment():
        raise RuntimeError("test-only endpoints require ENVIRONMENT=test")


def set_offset_seconds(seconds: int) -> int:
    """Write the single clock_state row. Returns the stored offset."""
    engine = _admin_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text("DELETE FROM clock_state"))
            conn.execute(
                text("INSERT INTO clock_state (singleton, offset_seconds) VALUES (true, :seconds)"),
                {"seconds": seconds},
            )
    finally:
        engine.dispose()
    return seconds


def reset() -> None:
    """POST /v1/test/reset: empty the tables, zero the clock, reinstall the seed fixtures."""
    require_test_environment()
    engine = _admin_engine()
    try:
        with engine.begin() as conn:
            conn.execute(text(f"TRUNCATE {', '.join(RESET_TABLES)}"))
        with Session(engine) as session:
            install_seed(session)
            session.commit()
        set_offset_seconds(0)
        observability.reset_counters()
    finally:
        engine.dispose()


def advance_clock(seconds: int) -> dict[str, int | list[str]]:
    """POST /v1/test/clock/advance: add to the offset, then sweep and deliver once (spec 4.6)."""
    require_test_environment()
    if seconds < 1:
        raise ValueError("seconds must be at least 1")
    new_offset = clock.offset_seconds() + seconds
    set_offset_seconds(new_offset)
    from app.db import get_engine

    engine = get_engine()
    opened = gaps.sweep(engine)
    delivered = notifications.deliver_due(engine)
    return {
        "seconds_advanced": seconds,
        "offset_seconds": new_offset,
        "gaps_opened": opened,
        "deliveries": delivered,
    }
