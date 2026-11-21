"""Personal-data sealing and erasure state (Phase 5 spec 4.2 Design B, 4.3 and 11.2).

Part 2 owns this module. Every personal value that goes into an append-only store (the
disposition rationale, the ledger payload that carries it, the report bytes) is sealed under
the record key of its event with AES-256-GCM, and what is stored is the sealed form:

- text: `pd1:` followed by base64url(nonce || ciphertext || tag), no padding;
- bytes: the bytes `pd1:` followed by nonce || ciphertext || tag.

The associated data is `subject_id|field`, so a sealed value cannot be moved to another event
or another field. A fresh random 12-byte nonce makes two seals of the same text differ.

The key lives in `record_key` beside the data, unwrapped (D7): this gives erasure, not
confidentiality. Destroying the key (`destroy`) makes every sealed value of that event
unreadable at once, and no stored ledger field changes, so no hash changes (INV-11).

`open` and `open_bytes` return the stored value unchanged when it was not sealed (a row written
before migration 0009), and None when the key is destroyed. A sealed value that fails to
authenticate (altered, or opened for the wrong event or field) raises SealError.
"""

import base64
import secrets
from datetime import datetime

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import clock

PREFIX = "pd1:"
PREFIX_BYTES = PREFIX.encode("ascii")
NONCE_LENGTH = 12
KEY_LENGTH = 32


class SealError(Exception):
    """A sealed value that cannot be opened with the key it should open with."""


class KeyDestroyedError(Exception):
    """Sealing was asked for an event whose record key has been destroyed."""


def _associated_data(subject_id: str, field: str) -> bytes:
    return f"{subject_id}|{field}".encode()


def _key(session: Session, subject_id: str, *, create: bool) -> bytes | None:
    """The data key of `subject_id`; None when there is none or it is destroyed. With `create`
    the key is made on first use (INSERT ... ON CONFLICT DO NOTHING, so two concurrent first
    seals end with one key) and a destroyed key raises KeyDestroyedError."""
    if create:
        session.execute(
            text(
                "INSERT INTO record_key (subject_id, data_key, created_at)"
                " VALUES (:subject_id, :key, :now) ON CONFLICT (subject_id) DO NOTHING"
            ),
            {"subject_id": subject_id, "key": secrets.token_bytes(KEY_LENGTH), "now": clock.now()},
        )
    row = session.execute(
        text("SELECT data_key FROM record_key WHERE subject_id = :subject_id"),
        {"subject_id": subject_id},
    ).one_or_none()
    if row is None:
        return None
    if row.data_key is None:
        if create:
            raise KeyDestroyedError(subject_id)
        return None
    return bytes(row.data_key)


def _encrypt(key: bytes, subject_id: str, field: str, data: bytes) -> bytes:
    nonce = secrets.token_bytes(NONCE_LENGTH)
    return nonce + AESGCM(key).encrypt(nonce, data, _associated_data(subject_id, field))


def _decrypt(key: bytes, subject_id: str, field: str, blob: bytes) -> bytes:
    nonce, rest = blob[:NONCE_LENGTH], blob[NONCE_LENGTH:]
    try:
        return AESGCM(key).decrypt(nonce, rest, _associated_data(subject_id, field))
    except (InvalidTag, ValueError) as exc:
        raise SealError(f"cannot open {field} of {subject_id}") from exc


def is_sealed(stored: str | bytes | None) -> bool:
    if isinstance(stored, bytes):
        return stored.startswith(PREFIX_BYTES)
    return isinstance(stored, str) and stored.startswith(PREFIX)


def seal(session: Session, subject_id: str, field: str, text_value: str) -> str:
    key = _key(session, subject_id, create=True)
    assert key is not None
    blob = _encrypt(key, subject_id, field, text_value.encode("utf-8"))
    return PREFIX + base64.urlsafe_b64encode(blob).decode("ascii").rstrip("=")


def seal_bytes(session: Session, subject_id: str, field: str, data: bytes) -> bytes:
    key = _key(session, subject_id, create=True)
    assert key is not None
    return PREFIX_BYTES + _encrypt(key, subject_id, field, data)


def open(  # noqa: A001 - the name is pinned by spec 11.2
    session: Session, subject_id: str, field: str, stored: str | None
) -> str | None:
    if stored is None or not stored.startswith(PREFIX):
        return stored
    key = _key(session, subject_id, create=False)
    if key is None:
        return None
    encoded = stored[len(PREFIX) :]
    try:
        blob = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
    except ValueError as exc:
        raise SealError(f"cannot open {field} of {subject_id}") from exc
    return _decrypt(key, subject_id, field, blob).decode("utf-8")


def open_bytes(session: Session, subject_id: str, field: str, stored: bytes | None) -> bytes | None:
    if stored is None:
        return None
    data = bytes(stored)
    if not data.startswith(PREFIX_BYTES):
        return data
    key = _key(session, subject_id, create=False)
    if key is None:
        return None
    return _decrypt(key, subject_id, field, data[len(PREFIX_BYTES) :])


def destroy(session: Session, subject_id: str, when: datetime) -> bool:
    """Destroy the record key of `subject_id`: data_key to NULL, destroyed_at to `when`. The row
    stays. Returns True when a live key was destroyed, False when there was none to destroy
    (no key was ever made, or it is already destroyed)."""
    destroyed = session.execute(
        text(
            "UPDATE record_key SET data_key = NULL, destroyed_at = :when"
            " WHERE subject_id = :subject_id AND data_key IS NOT NULL RETURNING subject_id"
        ),
        {"subject_id": subject_id, "when": when},
    ).one_or_none()
    return destroyed is not None


def is_erased(session: Session, event_id: str) -> bool:
    """Whether the event has been erased (event.erased_at is set). False for an unknown id."""
    erased = session.execute(
        text("SELECT erased_at IS NOT NULL FROM event WHERE event_id = :event_id"),
        {"event_id": event_id},
    ).scalar_one_or_none()
    return bool(erased)
