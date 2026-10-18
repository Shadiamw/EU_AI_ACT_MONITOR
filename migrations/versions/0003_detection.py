"""Detection: metric_sample, breach_series, event and harm_field (spec 1.7, 4.4).

Part 1. This file replaces the Part 1 STUB `0003_detection_stub.py` that Parts 2 and 3 used. It
keeps the stub's `event` and `harm_field` tables exactly (Part 3's SQL and
`tests/worker/test_schema.py` pin them) and adds Part 1's other two tables, with the spec 1.7
table, column and index names:

- metric_sample: every accepted sample. Unique on (system_id, metric_name, timestamp), the
  duplicate key of spec 1.4 step 5. event_id is set when the sample breached (a new or a joined
  event).
- breach_series: one row per run of consecutive breaching samples for (system_id, metric_name).
  At most one is open (closed_at null): the partial unique index. occurrence_count starts at 1.
- event: every flagged event, breach or gap. The gap sweeper inserts monitoring_gap events (spec
  1.7: "Part 3 inserts gap events into your event table") and closes them.
- harm_field: four rows per event (spec 1.5 and 1.7).

No foreign keys between these tables: the spec names none, and Part 4's reset and the tests
truncate them in any order (decision 127). The application role gets normal read and write
(spec 4.4).

Revision ID: 0003
Revises: 0002
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
TABLES = ("metric_sample", "breach_series", "event", "harm_field")
EVENT_STATES = ("FLAGGED", "UNDER_REVIEW", "DISPOSITIONED", "CLOSED")
HARM_FIELD_STATUSES = ("PREFILLED_PENDING_CONFIRMATION", "CONFIRMED", "NOT_APPLICABLE")


def _app_role() -> str:
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    return role


def _in(values: Sequence[str]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    app_role = _app_role()
    timestamp = sa.DateTime(timezone=True)

    op.create_table(
        "metric_sample",
        sa.Column("sample_id", sa.Text, primary_key=True),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("metric_name", sa.Text, nullable=False),
        sa.Column("metric_type", sa.Text, nullable=False),
        sa.Column("value", sa.Numeric, nullable=False),
        sa.Column("timestamp", timestamp, nullable=False),
        sa.Column("received_at", timestamp, nullable=False),
        sa.Column("event_id", sa.Text, nullable=True),
        sa.CheckConstraint(
            "metric_type IN ('performance', 'drift')", name="ck_metric_sample_metric_type"
        ),
        sa.UniqueConstraint(
            "system_id", "metric_name", "timestamp", name="uq_metric_sample_identity"
        ),
    )

    op.create_table(
        "breach_series",
        sa.Column("series_id", sa.Text, primary_key=True),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("metric_name", sa.Text, nullable=False),
        sa.Column("event_id", sa.Text, nullable=False),
        sa.Column("opened_at", timestamp, nullable=False),
        sa.Column("closed_at", timestamp, nullable=True),
        sa.Column("occurrence_count", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.CheckConstraint("occurrence_count >= 1", name="ck_breach_series_occurrence_count"),
    )
    op.create_index(
        "uq_breach_series_one_open_per_metric",
        "breach_series",
        ["system_id", "metric_name"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    op.create_table(
        "event",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("type", sa.Text, nullable=False),
        sa.Column("metric_name", sa.Text, nullable=True),
        sa.Column("observed_value", sa.Numeric, nullable=True),
        sa.Column("threshold_value", sa.Numeric, nullable=True),
        sa.Column("threshold_version_id", sa.Text, nullable=True),
        sa.Column("detected_at", timestamp, nullable=False),
        sa.Column("received_at", timestamp, nullable=False),
        sa.Column("state", sa.Text, nullable=False),
        sa.Column("deadline_utc", timestamp, nullable=True),
        sa.Column("evidence_bundle", JSONB, nullable=True),
        sa.Column("evidence_sha256", sa.Text, nullable=True),
        sa.Column("retain_until", timestamp, nullable=False),
        sa.CheckConstraint(f"state IN ({_in(EVENT_STATES)})", name="ck_event_state"),
    )
    op.create_index(
        "ix_event_detected_at_event_id",
        "event",
        [sa.text("detected_at DESC"), "event_id"],
    )
    op.create_index(
        "uq_event_one_open_gap_per_system",
        "event",
        ["system_id"],
        unique=True,
        postgresql_where=sa.text("type = 'monitoring_gap' AND state = 'FLAGGED'"),
    )

    op.create_table(
        "harm_field",
        sa.Column("event_id", sa.Text, primary_key=True),
        sa.Column("field_key", sa.Text, primary_key=True),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("evidence", sa.Text, nullable=True),
        sa.Column("confirmed_by", sa.Text, nullable=True),
        sa.Column("confirmed_at", timestamp, nullable=True),
        sa.CheckConstraint(f"status IN ({_in(HARM_FIELD_STATUSES)})", name="ck_harm_field_status"),
    )

    for table in TABLES:
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table} TO "{app_role}"')


def downgrade() -> None:
    for table in reversed(TABLES):
        op.drop_table(table)
