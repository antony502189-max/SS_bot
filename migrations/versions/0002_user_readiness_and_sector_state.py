"""Add registration readiness states and sector activation.

Revision ID: 0002_user_readiness_and_sector_state
Revises: 0001_initial
Create Date: 2026-08-25
"""

import sqlalchemy as sa
from alembic import op

revision = "0002_user_readiness"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE userstatus ADD VALUE IF NOT EXISTS 'NEEDS_USERNAME'")
        op.execute("ALTER TYPE userstatus ADD VALUE IF NOT EXISTS 'BLOCKED'")
    columns = {column["name"] for column in sa.inspect(bind).get_columns("sectors")}
    if "is_active" not in columns:
        with op.batch_alter_table("sectors") as batch:
            batch.add_column(sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade() -> None:
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("sectors")}
    if "is_active" in columns:
        with op.batch_alter_table("sectors") as batch:
            batch.drop_column("is_active")
