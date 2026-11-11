"""Reports (Phase 5 Part 1, spec 3). Part 1 owns this module.

STUB of the Phase 5 base commit (spec 11.3), carried on the Part 3 branch because the base
commit has not landed (Part 3 decision 294). Only the interface Part 3 calls is here, with the
signature of spec 11.2: verify_stored returns [] (nothing found). Part 1's real module replaces
this file at integration.
"""

from typing import Any

from sqlalchemy.orm import Session


def verify_stored(session: Session) -> list[dict[str, Any]]:
    """Every non-erased report whose stored bytes no longer match: [{report_id, which}]. The
    stub finds nothing."""
    return []
