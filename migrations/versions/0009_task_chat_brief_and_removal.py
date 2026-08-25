"""Track pinned task briefs and removed chat members."""

import sqlalchemy as sa

from alembic import op

revision = "0009_task_chat_brief_and_removal"
down_revision = "0008_task_photo_previews"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE membershipstate ADD VALUE IF NOT EXISTS 'removed'")
    existing = {column["name"] for column in sa.inspect(bind).get_columns("task_chats")}
    if "pinned_message_id" not in existing:
        op.add_column("task_chats", sa.Column("pinned_message_id", sa.Integer(), nullable=True))


def downgrade() -> None:
    pass
