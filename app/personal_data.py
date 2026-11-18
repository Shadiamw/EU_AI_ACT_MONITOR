"""Personal-data sealing (Phase 5 spec 4.3 and 11.2). Part 2 owns this module.

This is the identity stub of the Phase 5 base commit (spec 11.3), so Part 1 can build and test
the report against the real call signatures. `seal` and `seal_bytes` return their input
unchanged, `open` and `open_bytes` return the stored value unchanged, and `is_erased` is always
False. Part 2 replaces the bodies with per-record AES-GCM sealing and erasure and keeps the
signatures. If erasure is cut (spec 1.4), this stub stays for good.
"""

from sqlalchemy.orm import Session


def seal(session: Session, subject_id: str, field: str, text: str) -> str:
    return text


def seal_bytes(session: Session, subject_id: str, field: str, data: bytes) -> bytes:
    return data


def open(  # noqa: A001 - the name is pinned by spec 11.2
    session: Session, subject_id: str, field: str, stored: str | None
) -> str | None:
    return stored


def open_bytes(session: Session, subject_id: str, field: str, stored: bytes | None) -> bytes | None:
    return stored


def is_erased(session: Session, event_id: str) -> bool:
    return False
