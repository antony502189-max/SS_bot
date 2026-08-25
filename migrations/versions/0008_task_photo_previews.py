"""Store validated photo dimensions and preview locations."""

import sqlalchemy as sa

from alembic import op

revision = "0008_task_photo_previews"
down_revision = "0007_event_archive_retention"
branch_labels = None
depends_on = None


def upgrade() -> None:
    existing = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("task_photos")}
    for name, type_ in {
        "preview_object_key": sa.String(length=500),
        "width": sa.Integer(),
        "height": sa.Integer(),
    }.items():
        if name not in existing:
            op.add_column("task_photos", sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    pass
