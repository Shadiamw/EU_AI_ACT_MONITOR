"""Authentication (spec 1.3): Principal, the principal resolver and require(*roles).

A request authenticates with `Authorization: Bearer <key>`. The key is hashed with SHA-256 and
its hex digest is compared, with `hmac.compare_digest`, against every digest in `api_key` (Part
4 seeds it; only digests are stored). Every stored digest is compared, so the time taken does
not depend on which key matched or how much of a digest did (decision 123). No header, a header
that is not `Bearer <key>`, or a key with no matching digest gives no principal, so 401
MISSING_OR_INVALID_KEY. Keys are never logged, not even partly.

The lookup uses its own short autocommit connection, closed before the handler runs, so
authentication never leaves a transaction open, in particular during the webhook verification
ping (decision 114).

`require(*roles)` returns a FastAPI dependency that checks, in order: no principal gives 401
MISSING_OR_INVALID_KEY, then a role outside `roles` gives 403 FORBIDDEN_ROLE. Handlers validate
bodies and look resources up only after it returns. It also puts the actor id on the request,
for the request log line (spec 1.8). Tests replace `resolve_principal` through FastAPI's
`app.dependency_overrides`.
"""

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import text

from app.db import get_engine
from app.errors import ApiError

# A key longer than this is refused before it is hashed.
MAX_KEY_LENGTH = 4096


@dataclass(frozen=True)
class Principal:
    role: str  # SYSTEM, REVIEWER or VIEWER
    actor_id: str
    system_id: str | None


def key_digest(key: str) -> str:
    """The lowercase hex SHA-256 of a key, as stored in api_key.key_hash."""
    return sha256(key.encode("utf-8")).hexdigest()


def bearer_key(authorization: str | None) -> str | None:
    """The key from an `Authorization: Bearer <key>` header, or None if the header is missing
    or malformed. The scheme is case-insensitive (RFC 9110); the key is one token with no
    whitespace."""
    if authorization is None:
        return None
    scheme, separator, key = authorization.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    if not key or len(key) > MAX_KEY_LENGTH or any(c.isspace() for c in key):
        return None
    return key


def resolve_principal(
    authorization: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """The caller's principal, or None when there is no valid key."""
    key = bearer_key(authorization)
    if key is None:
        return None
    presented = key_digest(key).encode("ascii")
    with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        stored = connection.execute(
            text("SELECT key_hash, role, actor_id, system_id FROM api_key")
        ).all()
    match = None
    for row in stored:
        # No early exit: every stored digest is compared.
        if hmac.compare_digest(row.key_hash.encode("utf-8"), presented) and match is None:
            match = row
    return None if match is None else Principal(match.role, match.actor_id, match.system_id)


def require(*roles: str) -> Callable[..., Principal]:
    """A dependency returning the caller's principal if its role is one of `roles`."""
    allowed = frozenset(roles)

    def dependency(
        request: Request, principal: Annotated[Principal | None, Depends(resolve_principal)]
    ) -> Principal:
        if principal is None:
            raise ApiError(401, "MISSING_OR_INVALID_KEY", "missing or invalid API key")
        request.state.actor_id = principal.actor_id
        if principal.role not in allowed:
            raise ApiError(403, "FORBIDDEN_ROLE", "this role may not call this endpoint")
        return principal

    return dependency
