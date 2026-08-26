"""Normalize existing task-chat IDs to the Bot API supergroup form.

Revision ID: 0012_normalize_task_chat_ids
Revises: 0011_report_drafts
"""

from alembic import op

revision = "0012_normalize_task_chat_ids"
down_revision = "0011_report_drafts"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Telethon Channel.id values are positive; Bot API chat IDs are `-100…`.
    op.execute(
        "UPDATE task_chats SET telegram_chat_id = -1000000000000 - telegram_chat_id "
        "WHERE telegram_chat_id > 0"
    )


def downgrade() -> None:
    # Retain normalized IDs: reverting them would break Bot API chat_member matching.
    pass
