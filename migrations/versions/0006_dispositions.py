"""Dispositions (Phase 4 spec 4.4): disposition, its grants and its UPDATE/DELETE-blocking trigger.

STUB for Phase 4 Part 2: a placeholder written by the Phase 4 base commit (Phase 4 Addendum
9.1). It keeps the revision chain fixed, so every part's branch has the same head. Part 2
fills in upgrade() and downgrade() in this file only; the revision id and the chain never
change.

Revision ID: 0006
Revises: 0005
"""

from collections.abc import Sequence

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
