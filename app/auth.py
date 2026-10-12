"""STUB for Part 1 (auth). Only the Principal type, used as audit.record_view's argument.

auth.require(*roles) belongs to Part 1 and is not stubbed yet.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Principal:
    role: str  # SYSTEM, REVIEWER or VIEWER
    actor_id: str
    system_id: str | None
