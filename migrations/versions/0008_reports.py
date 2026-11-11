"""Reports (Phase 5 spec 3.5): report and report_submission.

STUB for Phase 5 Part 1: a placeholder of the Phase 5 base commit (spec 11.1 step 2), carried
on the Part 3 branch because the base commit has not landed (Part 3 decision 294). It keeps the
revision chain fixed, so the branch reaches head 0010. Part 1's real file replaces this one at
integration; the revision id and the chain never change.

Revision ID: 0008
Revises: 0007
"""

from collections.abc import Sequence

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
