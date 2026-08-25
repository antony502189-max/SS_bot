"""Add task closure and durable notification fields.

Revision ID: 0004_task_lifecycle_and_notifications
Revises: 0003_users_trigram_index
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_task_lifecycle_and_notifications"
down_revision = "0003_users_trigram_index"
branch_labels = None
depends_on = None


def add_if_missing(table: str, column: sa.Column) -> None:
    columns = {item["name"] for item in sa.inspect(op.get_bind()).get_columns(table)}
    if column.name not in columns:
        with op.batch_alter_table(table) as batch:
            batch.add_column(column)


def upgrade() -> None:
    add_if_missing("tasks", sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True))
    add_if_missing("notifications", sa.Column("task_id", sa.Uuid(), nullable=True))
    add_if_missing("notifications", sa.Column("event_id", sa.Uuid(), nullable=True))
    add_if_missing(
        "notifications",
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
    )
    add_if_missing(
        "notifications", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True)
    )
    add_if_missing(
        "notifications", sa.Column("attempts", sa.Integer(), nullable=False, server_default="0")
    )
    add_if_missing(
        "notifications", sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True)
    )
    add_if_missing("notifications", sa.Column("last_error", sa.Text(), nullable=True))


def downgrade() -> None:
    # Retain lifecycle history on downgrade; destructive column removal is intentionally omitted.
    pass
