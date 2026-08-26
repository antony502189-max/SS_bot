"""Add event archive retention and purge tracking."""

import sqlalchemy as sa
from alembic import op

revision = "0007_event_archive_retention"
down_revision = "0006_task_chat_member_tracking"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("events")}
    additions = {
        "retention_delete_at": sa.DateTime(timezone=True),
        "retention_extended_until": sa.DateTime(timezone=True),
        "retention_warning_sent_at": sa.DateTime(timezone=True),
        "purged_at": sa.DateTime(timezone=True),
    }
    for name, type_ in additions.items():
        if name not in existing:
            op.add_column("events", sa.Column(name, type_, nullable=True))
    indexes = {index["name"] for index in sa.inspect(op.get_bind()).get_indexes("events")}
    if "ix_events_retention_delete_at" not in indexes:
        op.create_index("ix_events_retention_delete_at", "events", ["retention_delete_at"])


def downgrade() -> None:
    pass
