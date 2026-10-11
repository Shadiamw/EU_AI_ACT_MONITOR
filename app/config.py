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
