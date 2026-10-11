"""STUB for Part 4: platform tables and database roles.

Part 4 owns migration 0001 (clock_state, api_key, monitored_system, threshold_version,
sweeper_heartbeat, database roles). This empty revision exists only so 0002 has a parent.
Until Part 4 lands, the roles are created by tests/sql/bootstrap_roles.sql.

Revision ID: 0001
Revises:
"""

from collections.abc import Sequence

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
