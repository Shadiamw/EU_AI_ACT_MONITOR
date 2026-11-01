"""Escalations (Phase 4 spec 3.5, 3.7 and 4.4): escalation, notification uniqueness and
escalation_pct.

Phase 4 Part 3. This file fills the placeholder of the Phase 4 base commit (Addendum 9.1); the
revision id and the chain are unchanged.

- escalation: one row per (event, percentage), planned in the disposition transaction and fired
  by the scheduler. It is operational state, so the application role gets normal read and write;
  the history lives in the ledger and the disposition rows (spec 3.7). anchor_at is set by plan
  and never changes; tier and deadline_utc are set when the row becomes SENT, so the reminder
  body and every retry are built from the row (Addendum rule 5). event_id references event;
  notification_id is unique with no foreign key, because the row is updated before enqueue
  inserts the notification (rule 5).
- notification: one flag per event and one reminder per (event, percentage), by two partial
  unique indexes in place of uq_notification_event (spec 3.5). escalation_pct is set exactly on
  reminders.

The downgrade removes the reminder notifications and their delivery rows, because the old
one-notification-per-event constraint cannot hold them. The ledger keeps their entries. Every
step tolerates a database upgraded while this file was the empty placeholder (Part 3 decision
199, like Part 2 decision 190).

PUBLIC gets nothing.

Revision ID: 0007
Revises: 0006
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
STATUSES = ("PENDING", "SENT", "CANCELLED")


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
    statuses = ", ".join(f"'{status}'" for status in STATUSES)

    op.create_table(
        "escalation",
        sa.Column(
            "event_id",
            sa.Text,
            sa.ForeignKey("event.event_id", name="fk_escalation_event"),
            primary_key=True,
        ),
        sa.Column("pct", sa.Integer, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("due_at", timestamp, nullable=False),
        sa.Column("anchor_at", timestamp, nullable=False),
        sa.Column("tier", sa.Text, nullable=True),
        sa.Column("deadline_utc", timestamp, nullable=True),
        sa.Column("notification_id", sa.Text, nullable=True),
        sa.Column("sent_at", timestamp, nullable=True),
        sa.Column("cancelled_at", timestamp, nullable=True),
        sa.Column("updated_at", timestamp, nullable=False),
        sa.UniqueConstraint("notification_id", name="uq_escalation_notification"),
        sa.CheckConstraint("pct BETWEEN 1 AND 100", name="ck_escalation_pct"),
        sa.CheckConstraint(f"status IN ({statuses})", name="ck_escalation_status"),
        sa.CheckConstraint(
            "(status = 'SENT') = (notification_id IS NOT NULL)"
            " AND (status = 'SENT') = (sent_at IS NOT NULL)"
            " AND (status = 'SENT') = (tier IS NOT NULL)"
            " AND (status = 'SENT') = (deadline_utc IS NOT NULL)",
            name="ck_escalation_sent",
        ),
        sa.CheckConstraint(
            "(status = 'CANCELLED') = (cancelled_at IS NOT NULL)",
            name="ck_escalation_cancelled",
        ),
    )
    op.create_index("ix_escalation_due", "escalation", ["status", "due_at"])
    op.execute("REVOKE ALL ON TABLE escalation FROM PUBLIC")
    op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE escalation TO "{app_role}"')

    op.drop_constraint("uq_notification_event", "notification", type_="unique")
    op.add_column("notification", sa.Column("escalation_pct", sa.Integer, nullable=True))
    op.create_check_constraint(
        "ck_notification_escalation_pct",
        "notification",
        "(notification_type = 'reminder') = (escalation_pct IS NOT NULL)",
    )
    op.create_index(
        "uq_notification_flag",
        "notification",
        ["event_id"],
        unique=True,
        postgresql_where=sa.text("notification_type = 'flag_notification'"),
    )
    op.create_index(
        "uq_notification_reminder",
        "notification",
        ["event_id", "escalation_pct"],
        unique=True,
        postgresql_where=sa.text("notification_type = 'reminder'"),
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS escalation")
    op.execute(
        "DELETE FROM notification_delivery WHERE notification_id IN"
        " (SELECT notification_id FROM notification WHERE notification_type = 'reminder')"
    )
    op.execute("DELETE FROM notification WHERE notification_type = 'reminder'")
    op.execute("DROP INDEX IF EXISTS uq_notification_reminder")
    op.execute("DROP INDEX IF EXISTS uq_notification_flag")
    op.execute("ALTER TABLE notification DROP CONSTRAINT IF EXISTS ck_notification_escalation_pct")
    op.execute("ALTER TABLE notification DROP COLUMN IF EXISTS escalation_pct")
    op.execute("ALTER TABLE notification DROP CONSTRAINT IF EXISTS uq_notification_event")
    op.create_unique_constraint("uq_notification_event", "notification", ["event_id"])
