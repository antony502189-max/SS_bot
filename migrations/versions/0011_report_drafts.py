"""Add an explicit report lifecycle for editable drafts and returned reports.

Revision ID: 0011_report_drafts
Revises: 0010_telegram_ids_bigint
"""

import sqlalchemy as sa

from alembic import op

revision = "0011_report_drafts"
down_revision = "0010_telegram_ids_bigint"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns("task_reports")}
    if "status" not in columns:
        with op.batch_alter_table("task_reports") as batch:
            batch.add_column(
                sa.Column(
                    "status", sa.String(length=20), nullable=False, server_default="submitted"
                )
            )
            batch.create_index("ix_task_reports_status", ["status"])
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TABLE task_reports ALTER COLUMN submitted_at DROP NOT NULL"
        )
    else:
        with op.batch_alter_table("task_reports") as batch:
            batch.alter_column(
                "submitted_at", existing_type=sa.DateTime(timezone=True), nullable=True
            )


def downgrade() -> None:
    # Existing report lifecycle history must survive a downgrade.
    pass
