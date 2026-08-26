"""Repair enum labels previously added using StrEnum values instead of SQLAlchemy enum names.

Revision ID: 0012_fix_postgres_enum_labels
Revises: 0011_report_lifecycle
Create Date: 2026-08-26
"""

from alembic import op

revision = "0012_fix_postgres_enum_labels"
down_revision = "0011_report_lifecycle"
branch_labels = None
depends_on = None


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for enum_name, labels in {
        "userstatus": ["NEEDS_USERNAME", "BLOCKED"],
        "chatstatus": ["CREATING", "DEGRADED"],
        "membershipstate": ["NOT_JOINED", "REMOVED"],
    }.items():
        for label in labels:
            op.execute(f"ALTER TYPE {enum_name} ADD VALUE IF NOT EXISTS '{label}'")


def downgrade() -> None:
    # PostgreSQL enum values cannot be safely removed while rows may reference them.
    pass
