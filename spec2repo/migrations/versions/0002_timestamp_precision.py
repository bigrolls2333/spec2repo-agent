"""Preserve API timestamp precision after a MySQL round trip."""

from alembic import op
from sqlalchemy.dialects.mysql import DATETIME

revision = "0002_timestamp_precision"
down_revision = "0001_control_plane"
branch_labels = None
depends_on = None

COLUMNS = [
    ("runs", "created_at", False),
    ("runs", "updated_at", False),
    ("idempotency_keys", "created_at", False),
    ("outbox", "created_at", False),
    ("outbox", "published_at", True),
    ("run_events", "created_at", False),
]


def change_precision(before, after):
    if op.get_bind().dialect.name == "mysql":
        for table, column, nullable in COLUMNS:
            op.alter_column(
                table,
                column,
                existing_type=DATETIME(fsp=before),
                type_=DATETIME(fsp=after),
                existing_nullable=nullable,
            )


def upgrade():
    change_precision(0, 6)


def downgrade():
    change_precision(6, 0)
