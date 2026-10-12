"""STUB for Part 1 (auth): Principal, the principal resolver and require(*roles).

There is no key parsing or hashing here; that stays with Part 1 (docs/decisions.md 29).
The principal comes from one overridable resolver, `resolve_principal`, whose default returns
the seeded REVIEWER rev-1 (plan G3). Tests override it through FastAPI's
`app.dependency_overrides` to act as viewer-1, rev-1, sys-42 or anonymous (None).

`require(*roles)` returns a FastAPI dependency that checks, in order: no principal gives 401
MISSING_OR_INVALID_KEY, then a role outside `roles` gives 403 FORBIDDEN_ROLE. Handlers run
their 404 lookups only after it returns.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends

from app.errors import ApiError


@dataclass(frozen=True)
class Principal:
    role: str  # SYSTEM, REVIEWER or VIEWER
    actor_id: str
    system_id: str | None


def resolve_principal() -> Principal | None:
    """The caller's principal, or None when there is no valid key. Stub: always rev-1."""
    return Principal(role="REVIEWER", actor_id="rev-1", system_id=None)


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
