"""Add recoverable Telegram task-chat states.

Revision ID: 0005_task_chat_states
Revises: 0004_task_lifecycle_and_notifications
Create Date: 2026-08-25
"""

from alembic import op

revision = "0005_task_chat_states"
down_revision = "0004_task_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TYPE chatstatus ADD VALUE IF NOT EXISTS 'CREATING'")
        op.execute("ALTER TYPE chatstatus ADD VALUE IF NOT EXISTS 'DEGRADED'")


def downgrade() -> None:
    # PostgreSQL enum values are deliberately not removed during downgrade.
    pass
