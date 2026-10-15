"""Notifications: webhook, notification and notification_delivery (spec 3.7, 4.4).

Part 3. The application role (the user in DATABASE_URL) gets normal read and write on these
tables (spec 4.4: everything except the ledger). PUBLIC gets nothing.

- webhook: registered receivers and the notification types each one wants.
- notification: one row per event (spec 3.7), requested in the event's transaction.
- notification_delivery: one row per (notification, webhook), created by the delivery pass when
  it routes the notification (spec 3.5 step 1). A lease is `lease_until` on a PENDING row.

Revision ID: 0004
Revises: 0003
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLES = ("webhook", "notification", "notification_delivery")
DELIVERY_STATUSES = ("PENDING", "DELIVERED", "FAILED")


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
    timestamp = sa.DateTime(timezone=True)

    op.create_table(
        "webhook",
        sa.Column("webhook_id", sa.Text, primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("event_types", ARRAY(sa.Text), nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.CheckConstraint("cardinality(event_types) > 0", name="ck_webhook_event_types"),
    )

    op.create_table(
        "notification",
        sa.Column("notification_id", sa.Text, primary_key=True),
        sa.Column(
            "event_id",
            sa.Text,
            sa.ForeignKey("event.event_id", name="fk_notification_event"),
            nullable=False,
        ),
        sa.Column("notification_type", sa.Text, nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        sa.UniqueConstraint("event_id", name="uq_notification_event"),
    )

    statuses = ", ".join(f"'{status}'" for status in DELIVERY_STATUSES)
    op.create_table(
        "notification_delivery",
        sa.Column(
            "notification_id",
            sa.Text,
            sa.ForeignKey("notification.notification_id"),
            primary_key=True,
        ),
        sa.Column("webhook_id", sa.Text, sa.ForeignKey("webhook.webhook_id"), primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("attempt_count", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", timestamp, nullable=False),
        sa.Column("lease_until", timestamp, nullable=True),
        sa.Column("delivered_at", timestamp, nullable=True),
        sa.Column("last_status", sa.Text, nullable=True),
        sa.CheckConstraint(f"status IN ({statuses})", name="ck_notification_delivery_status"),
        sa.CheckConstraint("attempt_count >= 0", name="ck_notification_delivery_attempts"),
    )
    op.create_index(
        "ix_notification_delivery_due",
        "notification_delivery",
        ["status", "next_attempt_at"],
    )

    for table in TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO "{app_role}"')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
