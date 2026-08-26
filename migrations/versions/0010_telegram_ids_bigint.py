"""Store Telegram identifiers as 64-bit integers.

Revision ID: 0010_telegram_ids_bigint
Revises: 0009_task_chat_brief_and_removal
"""

import sqlalchemy as sa

from alembic import op

revision = "0010_telegram_ids_bigint"
down_revision = "0009_task_chat_brief_and_removal"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch:
        batch.alter_column("telegram_id", existing_type=sa.Integer(), type_=sa.BigInteger())
    with op.batch_alter_table("task_chats") as batch:
        batch.alter_column("telegram_chat_id", existing_type=sa.Integer(), type_=sa.BigInteger())


def downgrade() -> None:
    with op.batch_alter_table("task_chats") as batch:
        batch.alter_column("telegram_chat_id", existing_type=sa.BigInteger(), type_=sa.Integer())
    with op.batch_alter_table("users") as batch:
        batch.alter_column("telegram_id", existing_type=sa.BigInteger(), type_=sa.Integer())
