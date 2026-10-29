"""Leased execution and immutable stage checkpoints."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR

revision = "0004_execution"
down_revision = "0003_dispatch_receipts"
branch_labels = None
depends_on = None


def upgrade():
    timestamp = sa.DateTime().with_variant(DATETIME(fsp=6), "mysql")
    text36 = sa.String(36).with_variant(
        VARCHAR(36, charset="ascii", collation="ascii_bin"), "mysql"
    )
    text64 = sa.String(64).with_variant(
        VARCHAR(64, charset="ascii", collation="ascii_bin"), "mysql"
    )
    op.add_column("runs", sa.Column("generation", sa.Integer(), server_default="0", nullable=False))
    op.add_column("runs", sa.Column("lease_owner", sa.String(128), nullable=True))
    op.add_column("runs", sa.Column("lease_until", timestamp, nullable=True))
    op.create_index("ix_runs_status_lease", "runs", ["status", "lease_until"])
    op.create_table(
        "attempts",
        sa.Column("run_id", text36, sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("worker", sa.String(128), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("started_at", timestamp, nullable=False),
        sa.Column("ended_at", timestamp, nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "checkpoints",
        sa.Column("run_id", text36, sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("stage", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("spec_hash", text64, nullable=False),
        sa.Column("snapshot_hash", text64, nullable=False),
        sa.Column("created_at", timestamp, nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )


def downgrade():
    op.drop_table("checkpoints")
    op.drop_table("attempts")
    op.drop_index("ix_runs_status_lease", "runs")
    for column in ["lease_until", "lease_owner", "generation"]:
        op.drop_column("runs", column)
