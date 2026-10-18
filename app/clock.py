"""STUB for Part 4 (clock). The real module adds the test-environment offset from clock_state.

Shared rule: all code gets the current time from clock.now(); nothing else reads the system
clock. Callers use `clock.now()` through the module, so tests can patch `app.clock.now`.
"""

from datetime import UTC, datetime


def now() -> datetime:
    """Current time as a timezone-aware UTC datetime. Stub: real time, no offset."""
    return datetime.now(UTC)


def wall_now() -> datetime:
    """Real (wall-clock) UTC time, never simulated. Only for the sweeper heartbeat, which spec
    3.3 says holds real time; everything else uses now()."""
    return datetime.now(UTC)
