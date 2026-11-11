"""Personal data (Phase 5 spec 4): record keys and erasure.

STUB for Phase 5 Part 2b: a placeholder of the Phase 5 base commit (spec 11.1 step 2), carried
on the Part 3 branch because the base commit has not landed (Part 3 decision 294). It keeps the
revision chain fixed, so the branch reaches head 0010. Part 2b fills in upgrade() and
downgrade(); the revision id and the chain never change.

Revision ID: 0009
Revises: 0008
"""

from collections.abc import Sequence

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
