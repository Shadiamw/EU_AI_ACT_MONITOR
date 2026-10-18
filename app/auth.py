"""API-key authentication. Only SHA-256 digests of high-entropy keys are stored."""

from collections.abc import Callable
from dataclasses import dataclass
from hashlib import sha256
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy import text

from app.db import get_engine
from app.errors import ApiError


@dataclass(frozen=True)
class Principal:
    role: str  # SYSTEM, REVIEWER or VIEWER
    actor_id: str
    system_id: str | None


def key_digest(key: str) -> str:
    return sha256(key.encode("utf-8")).hexdigest()


def resolve_principal(
    x_api_key: Annotated[str | None, Header()] = None,
) -> Principal | None:
    """Resolve X-API-Key without exposing or logging the credential."""
    if not x_api_key or len(x_api_key) > 4096:
        return None
    # Finish credential lookup before the handler. In particular, webhook verification
    # must not inherit an open transaction from authentication.
    with get_engine().connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        row = connection.execute(
            text(
                "SELECT role, actor_id, system_id FROM api_key WHERE key_hash = :digest AND enabled"
            ),
            {"digest": key_digest(x_api_key)},
        ).one_or_none()
    return None if row is None else Principal(row.role, row.actor_id, row.system_id)


def require(*roles: str) -> Callable[..., Principal]:
    """A dependency returning the caller's principal if its role is one of `roles`."""
    allowed = frozenset(roles)

    def dependency(principal: Annotated[Principal | None, Depends(resolve_principal)]) -> Principal:
        if principal is None:
            raise ApiError(401, "MISSING_OR_INVALID_KEY", "missing or invalid API key")
        if principal.role not in allowed:
            raise ApiError(403, "FORBIDDEN_ROLE", "this role may not call this endpoint")
        return principal

    return dependency
