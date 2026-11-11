"""Ledger operations (Phase 5 spec 6.1 and 6.5): ledger_verification_run and platform_counter.

Phase 5 Part 3. Empty for now: the revision id and the chain are fixed by the Phase 5 base
commit (spec 11.1 step 2; Part 3 decision 294). Part 3 fills in upgrade() and downgrade() in
this file only (M4 and M6); the revision id and the chain never change.

Revision ID: 0010
Revises: 0009
"""

from collections.abc import Sequence

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
