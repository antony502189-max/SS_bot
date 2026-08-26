"""Track task-chat membership reconciliation and reminder delivery."""

import sqlalchemy as sa
from alembic import op

revision = "0006_task_chat_member_tracking"
down_revision = "0005_task_chat_states"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in sa.inspect(op.get_bind()).get_columns("task_chat_members")}


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE membershipstate ADD VALUE IF NOT EXISTS 'NOT_JOINED'")
    existing = _columns()
    additions = {
        "last_reminder_at": sa.DateTime(timezone=True),
        "reminder_count": sa.Integer(),
        "joined_at": sa.DateTime(timezone=True),
        "last_checked_at": sa.DateTime(timezone=True),
    }
    for name, type_ in additions.items():
        if name not in existing:
            nullable = name != "reminder_count"
            op.add_column(
                "task_chat_members",
                sa.Column(
                    name,
                    type_,
                    nullable=nullable,
                    server_default="0" if name == "reminder_count" else None,
                ),
            )
    if "reminder_count" not in existing:
        op.alter_column("task_chat_members", "reminder_count", server_default=None)


def downgrade() -> None:
    # PostgreSQL enum values are deliberately not removed; this migration is forward-only.
    pass
