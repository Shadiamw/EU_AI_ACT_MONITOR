"""Worker: monitoring gaps, webhooks, notifications and their deliveries.

Part 3. The application role (the user in DATABASE_URL) gets SELECT, INSERT and UPDATE on
these tables and nothing else: no row is ever deleted. PUBLIC gets nothing.

- monitoring_gap: one row per gap event. At most one open gap (closed_at IS NULL) per system,
  enforced by a partial unique index.
- webhook: registered receivers and the notification types each one wants.
- notification: one row per notification requested for an event.
- notification_delivery: one row per (notification, webhook), created when the notification is
  requested, and the delivery pass's work queue.

Revision ID: 0003
Revises: 0002
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLES = ("monitoring_gap", "webhook", "notification", "notification_delivery")
DELIVERY_STATUSES = ("PENDING", "IN_FLIGHT", "DELIVERED", "FAILED")


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
        "monitoring_gap",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("last_received_at", timestamp, nullable=True),
        sa.Column("silent_since", timestamp, nullable=False),
        sa.Column("opened_at", timestamp, nullable=False),
        sa.Column("closed_at", timestamp, nullable=True),
        sa.Column("gap_seconds", sa.BigInteger, nullable=True),
        sa.CheckConstraint(
            "(closed_at IS NULL) = (gap_seconds IS NULL)", name="ck_monitoring_gap_closed"
        ),
    )
    op.create_index(
        "uq_monitoring_gap_one_open_per_system",
        "monitoring_gap",
        ["system_id"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "webhook",
        sa.Column("webhook_id", sa.Text, primary_key=True),
        sa.Column("url", sa.Text, nullable=False),
        sa.Column("event_types", ARRAY(sa.Text), nullable=False),
        sa.Column("registered_by", sa.Text, nullable=False),
        sa.Column("registered_at", timestamp, nullable=False),
        sa.CheckConstraint("cardinality(event_types) > 0", name="ck_webhook_event_types"),
    )

    op.create_table(
        "notification",
        sa.Column("notification_id", sa.Text, primary_key=True),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("notification_type", sa.Text, nullable=False),
        sa.Column("requested_at", timestamp, nullable=False),
    )
    op.create_index("ix_notification_event", "notification", ["event_id"])

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
        sa.Column("attempts", sa.Integer, nullable=False),
        sa.Column("next_attempt_at", timestamp, nullable=False),
        sa.Column("lease_expires_at", timestamp, nullable=True),
        sa.Column("last_status", sa.Integer, nullable=True),
        sa.Column("delivered_at", timestamp, nullable=True),
        sa.Column("latency_seconds", sa.BigInteger, nullable=True),
        sa.CheckConstraint(f"status IN ({statuses})", name="ck_notification_delivery_status"),
        sa.CheckConstraint("attempts >= 0", name="ck_notification_delivery_attempts"),
    )
    op.create_index(
        "ix_notification_delivery_due",
        "notification_delivery",
        ["status", "next_attempt_at"],
    )

    for table in TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f'GRANT SELECT, INSERT, UPDATE ON TABLE {table} TO "{app_role}"')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
