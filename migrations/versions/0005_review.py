"""Review (Phase 4 spec 4.4): harm_context and harm_field.prefill_evidence (spec 1.7).

Phase 4 Part 1. This file fills the placeholder of the Phase 4 base commit (Addendum 9.1); the
revision id and the chain are unchanged.

- harm_context: one row per (system_id, field_key), the plain-text statement of why that
  system's failures relate to that harm. field_key is one of the four harm-field keys.
  system_id is a plain foreign key to monitored_system, without ON DELETE CASCADE (Addendum
  rule 5). context_text is required: a field with nothing to say has no row, so the pre-fill
  leaves it blank (spec 1.3 step 2). Seeded by Part 4 and changed only by migration or seed, so
  the application role gets SELECT only (spec 1.7).
- harm_field.prefill_evidence: the evidence generated at flag time, never updated (spec 1.7).
  Nullable; rows that exist before this migration get null. The application role keeps its
  normal read and write on harm_field (0003).

PUBLIC gets nothing on harm_context.

Revision ID: 0005
Revises: 0004
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
HARM_FIELDS = (
    "health_safety_harm",
    "critical_infrastructure_disruption",
    "fundamental_rights_infringement",
    "property_environmental_damage",
)


def _app_role() -> str:
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    owner = op.get_bind().execute(sa.text("SELECT current_user")).scalar_one()
    if role == owner:
        raise RuntimeError("DATABASE_URL must use the application role, not the owner role")
    return role


def upgrade() -> None:
    app_role = _app_role()
    keys = ", ".join(f"'{key}'" for key in HARM_FIELDS)
    op.create_table(
        "harm_context",
        sa.Column(
            "system_id",
            sa.Text,
            sa.ForeignKey("monitored_system.system_id", name="fk_harm_context_system"),
            primary_key=True,
        ),
        sa.Column("field_key", sa.Text, primary_key=True),
        sa.Column("context_text", sa.Text, nullable=False),
        sa.CheckConstraint(f"field_key IN ({keys})", name="ck_harm_context_field_key"),
    )
    op.execute("REVOKE ALL ON TABLE harm_context FROM PUBLIC")
    op.execute(f'GRANT SELECT ON TABLE harm_context TO "{app_role}"')
    op.add_column("harm_field", sa.Column("prefill_evidence", sa.Text, nullable=True))


def downgrade() -> None:
    op.drop_column("harm_field", "prefill_evidence")
    op.drop_table("harm_context")
