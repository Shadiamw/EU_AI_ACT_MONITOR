"""Review (Phase 4 spec 4.4): harm_context and harm_field.prefill_evidence.

STUB for Phase 4 Part 1: a placeholder written by the Phase 4 base commit (Phase 4 Addendum
9.1). It keeps the revision chain fixed, so every part's branch has the same head. Part 1
fills in upgrade() and downgrade() in this file only; the revision id and the chain never
change.

Revision ID: 0005
Revises: 0004
"""

from collections.abc import Sequence

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
