"""Reports (Phase 5 spec 3.5): report, report_submission, their grants and their UPDATE/DELETE-
blocking triggers.

Phase 5 Part 1.

- report: one row per assembled report version of an event, never edited (INV-4, INV-14). The
  bytes (json_bytes, pdf_bytes) are stored as produced; Part 2 seals them under the event's
  record key, so the column holds whatever personal_data.seal_bytes returned. Both SHA-256
  hashes are over the plaintext bytes and are stored in plain text. disposition_id is unique:
  at most one report per disposition row, so concurrent first requests produce one report
  (spec 3.8). report_version starts at 1 and is unique within the event.
- report_submission: one row per submitted report. report_id is unique (one submission per
  report, D6). report_type is one of the four Commission template 1.2 e strings a reviewer may
  record ("Final (Non-reportable incident)" is refused, C21). authority_reference is template
  1.1 b and is not personal data, so it is not sealed.
- Append-only: the application role gets INSERT and SELECT only, and one statement-level
  trigger per table raises on any UPDATE or DELETE, even one that matches no row, exactly like
  disposition (0006). There is no TRUNCATE trigger: test reset truncates as the owner role.

PUBLIC gets nothing. The report status (ASSEMBLED, SUBMITTED, SUPERSEDED) is derived when it is
read and is never a column, so neither table is ever updated (spec 3.2).

Revision ID: 0008
Revises: 0007
"""

import re
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.engine import make_url

from app.config import database_url

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TRIGGER_FUNCTION = "report_block_update_delete_fn"
TABLES = ("report", "report_submission")
REPORT_KINDS = ("SERIOUS_INCIDENT", "NOT_SERIOUS_CLOSURE")
SUBMISSION_KINDS = ("INITIAL", "FOLLOW_UP")
REPORT_TYPES = ("Initial", "Follow up", "Combined initial and final", "Final (Reportable incident)")

_ROLE_NAME = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
_HEX64 = "~ '^[0-9a-f]{64}$'"


def _app_role() -> str:
    role = make_url(database_url()).username
    if role is None or not _ROLE_NAME.match(role):
        raise RuntimeError(f"DATABASE_URL user {role!r} is not a plain lowercase role name")
    owner = op.get_bind().execute(sa.text("SELECT current_user")).scalar_one()
    if role == owner:
        raise RuntimeError("DATABASE_URL must use the application role, not the owner role")
    return role


def _in(values: Sequence[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


def upgrade() -> None:
    app_role = _app_role()
    op.create_table(
        "report",
        sa.Column("report_id", sa.Text, primary_key=True),
        sa.Column(
            "event_id",
            sa.Text,
            sa.ForeignKey("event.event_id", name="fk_report_event"),
            nullable=False,
        ),
        sa.Column("report_version", sa.Integer, nullable=False),
        sa.Column(
            "disposition_id",
            sa.Text,
            sa.ForeignKey("disposition.disposition_id", name="fk_report_disposition"),
            nullable=False,
        ),
        sa.Column("report_kind", sa.Text, nullable=False),
        sa.Column("schema_version", sa.Text, nullable=False),
        sa.Column("json_bytes", sa.LargeBinary, nullable=False),
        sa.Column("pdf_bytes", sa.LargeBinary, nullable=False),
        sa.Column("json_sha256", sa.Text, nullable=False),
        sa.Column("pdf_sha256", sa.Text, nullable=False),
        sa.Column("assembled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("assembled_by", sa.Text, nullable=False),
        sa.UniqueConstraint("event_id", "report_version", name="uq_report_event_version"),
        sa.UniqueConstraint("disposition_id", name="uq_report_disposition"),
        sa.CheckConstraint("report_version >= 1", name="ck_report_version_positive"),
        sa.CheckConstraint(f"report_kind IN ({_in(REPORT_KINDS)})", name="ck_report_kind"),
        sa.CheckConstraint(f"json_sha256 {_HEX64}", name="ck_report_json_sha256"),
        sa.CheckConstraint(f"pdf_sha256 {_HEX64}", name="ck_report_pdf_sha256"),
    )
    op.create_table(
        "report_submission",
        sa.Column("submission_id", sa.Text, primary_key=True),
        sa.Column(
            "report_id",
            sa.Text,
            sa.ForeignKey("report.report_id", name="fk_report_submission_report"),
            nullable=False,
        ),
        sa.Column(
            "event_id",
            sa.Text,
            sa.ForeignKey("event.event_id", name="fk_report_submission_event"),
            nullable=False,
        ),
        sa.Column("submitted_by", sa.Text, nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("submission_kind", sa.Text, nullable=False),
        sa.Column("report_type", sa.Text, nullable=False),
        sa.Column("expected_next_report_date", sa.Date, nullable=True),
        sa.Column("authority_reference", sa.Text, nullable=True),
        sa.UniqueConstraint("report_id", name="uq_report_submission_report"),
        sa.CheckConstraint(
            f"submission_kind IN ({_in(SUBMISSION_KINDS)})", name="ck_report_submission_kind"
        ),
        sa.CheckConstraint(
            f"report_type IN ({_in(REPORT_TYPES)})", name="ck_report_submission_type"
        ),
    )
    op.create_index("ix_report_submission_event", "report_submission", ["event_id"])

    op.execute(
        f"""
        CREATE FUNCTION {TRIGGER_FUNCTION}() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION '% is append-only: % is not allowed', TG_TABLE_NAME, TG_OP;
        END;
        $$
        """
    )
    for table in TABLES:
        # Statement-level, so an UPDATE or DELETE fails even when it matches no rows.
        op.execute(
            f"""
            CREATE TRIGGER {table}_block_update_delete
            BEFORE UPDATE OR DELETE ON {table}
            FOR EACH STATEMENT EXECUTE FUNCTION {TRIGGER_FUNCTION}()
            """
        )
        op.execute(f"REVOKE ALL ON TABLE {table} FROM PUBLIC")
        op.execute(f'REVOKE ALL ON TABLE {table} FROM "{app_role}"')
        op.execute(f'GRANT SELECT, INSERT ON TABLE {table} TO "{app_role}"')


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS report_submission")
    op.execute("DROP TABLE IF EXISTS report")
    op.execute(f"DROP FUNCTION IF EXISTS {TRIGGER_FUNCTION}()")
