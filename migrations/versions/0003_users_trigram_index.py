"""Use a PostgreSQL trigram index for user autocomplete.

Revision ID: 0003_users_trigram_index
Revises: 0002_user_readiness_and_sector_state
Create Date: 2026-08-25
"""

from alembic import op

revision = "0003_users_trigram_index"
down_revision = "0002_user_readiness"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("ix_users_full_name_trgm", table_name="users")
        op.execute(
            "CREATE INDEX ix_users_full_name_trgm ON users USING gin (normalized_full_name gin_trgm_ops)"
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.drop_index("ix_users_full_name_trgm", table_name="users")
        op.create_index("ix_users_full_name_trgm", "users", ["normalized_full_name"])
