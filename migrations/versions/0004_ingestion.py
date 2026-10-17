"""Part 1 ingestion persistence and the minimal platform interfaces it consumes.

Platform owners may extend api_key and threshold_version; existing revisions stay intact.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    from sqlalchemy.engine import make_url

    from app.config import database_url

    role = make_url(database_url()).username
    # Quote through the dialect, never interpolate an unquoted role identifier.
    assert role is not None
    quoted = op.get_bind().dialect.identifier_preparer.quote(role)
    op.create_table(
        "api_key",
        sa.Column("key_hash", sa.Text, primary_key=True),
        sa.Column("role", sa.Text, nullable=False),
        sa.Column("actor_id", sa.Text, nullable=False),
        sa.Column("system_id", sa.Text),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.CheckConstraint("role IN ('SYSTEM', 'REVIEWER', 'VIEWER')"),
        sa.CheckConstraint("(role = 'SYSTEM') = (system_id IS NOT NULL)"),
    )
    op.create_table(
        "threshold_version",
        sa.Column("threshold_version_id", sa.Text, primary_key=True),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("metric_name", sa.Text, nullable=False),
        sa.Column("metric_type", sa.Text, nullable=False),
        sa.Column("comparator", sa.Text, nullable=False),
        sa.Column("threshold_value", sa.Numeric, nullable=False),
        sa.Column("effective_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("metric_type IN ('performance', 'drift')"),
        sa.CheckConstraint("comparator IN ('gt', 'lt')"),
        sa.UniqueConstraint("system_id", "metric_name", "metric_type", "effective_at"),
    )
    op.create_table(
        "metric_sample",
        sa.Column("sample_id", sa.Text, primary_key=True),
        sa.Column("system_id", sa.Text, nullable=False),
        sa.Column("metric_name", sa.Text, nullable=False),
        sa.Column("metric_type", sa.Text, nullable=False),
        sa.Column("value", sa.Numeric, nullable=False),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("threshold_version_id", sa.Text, nullable=False),
        sa.Column("event_id", sa.Text),
        sa.UniqueConstraint("system_id", "metric_name", "metric_type", "timestamp"),
        sa.CheckConstraint("metric_type IN ('performance', 'drift')"),
    )
    op.add_column("event", sa.Column("metric_type", sa.Text))
    op.add_column("event", sa.Column("threshold_version_id", sa.Text))
    op.add_column(
        "event", sa.Column("series_open", sa.Boolean, nullable=False, server_default=sa.false())
    )
    op.add_column(
        "event",
        sa.Column("evidence_bundle", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.add_column("event", sa.Column("disposition", JSONB))
    op.add_column("event", sa.Column("deadline_utc", sa.DateTime(timezone=True)))
    op.add_column(
        "event",
        sa.Column("revisions", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
    )
    op.create_index(
        "event_open_series",
        "event",
        ["system_id", "metric_name", "metric_type"],
        unique=True,
        postgresql_where=sa.text("series_open"),
    )
    op.create_index("event_listing", "event", ["detected_at", "event_id"])
    for table in ("api_key", "threshold_version", "metric_sample"):
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        privileges = "SELECT, INSERT, UPDATE" if table == "metric_sample" else "SELECT"
        op.execute(f"GRANT {privileges} ON TABLE {table} TO {quoted}")


def downgrade() -> None:
    op.drop_index("event_listing", table_name="event")
    op.drop_index("event_open_series", table_name="event")
    for column in (
        "revisions",
        "deadline_utc",
        "disposition",
        "evidence_bundle",
        "series_open",
        "threshold_version_id",
        "metric_type",
    ):
        op.drop_column("event", column)
    for table in ("metric_sample", "threshold_version", "api_key"):
        op.drop_table(table)
