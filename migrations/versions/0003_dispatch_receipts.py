"""Durable delivery simulation receipts, separate from run success."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR

revision = "0003_dispatch_receipts"
down_revision = "0002_timestamp_precision"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "dispatch_receipts",
        sa.Column(
            "event_id",
            sa.String(36).with_variant(
                VARCHAR(36, charset="ascii", collation="ascii_bin"), "mysql"
            ),
            sa.ForeignKey("outbox.id"),
            primary_key=True,
        ),
        sa.Column("consumer", sa.String(128), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False
        ),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade():
    op.drop_table("dispatch_receipts")
