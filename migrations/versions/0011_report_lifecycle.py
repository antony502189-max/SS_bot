"""Add explicit report lifecycle state and return timestamp.

Revision ID: 0011_report_lifecycle
Revises: 0010_telegram_ids_bigint
Create Date: 2026-08-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0011_report_lifecycle"
down_revision = "0010_telegram_ids_bigint"
branch_labels = None
depends_on = None

report_status = sa.Enum("DRAFT", "SUBMITTED", "RETURNED", "APPROVED", name="reportstatus")


def upgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("task_reports")}
    if "status" not in columns:
        if bind.dialect.name == "postgresql":
            report_status.create(bind, checkfirst=True)
        with op.batch_alter_table("task_reports") as batch:
            batch.add_column(
                sa.Column(
                    "status",
                    report_status,
                    nullable=False,
                    server_default="DRAFT",
                )
            )
            batch.create_index("ix_task_reports_status", ["status"], unique=False)
    if "returned_at" not in columns:
        with op.batch_alter_table("task_reports") as batch:
            batch.add_column(sa.Column("returned_at", sa.DateTime(timezone=True), nullable=True))

    # Existing reports represent already submitted work. PostgreSQL needs an explicit
    # enum cast here; SQLite stores the same field as text.
    if bind.dialect.name == "postgresql":
        op.execute(
            sa.text(
                "UPDATE task_reports SET status = CASE "
                "WHEN approved_at IS NOT NULL THEN 'APPROVED'::reportstatus "
                "ELSE 'SUBMITTED'::reportstatus END"
            )
        )
    else:
        op.execute(
            sa.text(
                "UPDATE task_reports SET status = CASE "
                "WHEN approved_at IS NOT NULL THEN 'APPROVED' ELSE 'SUBMITTED' END"
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {column["name"] for column in sa.inspect(bind).get_columns("task_reports")}
    with op.batch_alter_table("task_reports") as batch:
        if "returned_at" in columns:
            batch.drop_column("returned_at")
        if "status" in columns:
            batch.drop_index("ix_task_reports_status")
            batch.drop_column("status")
    if bind.dialect.name == "postgresql":
        report_status.drop(bind, checkfirst=True)
