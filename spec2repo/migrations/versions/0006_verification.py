"""Immutable acceptance reports, tied to specification and submitted artifact."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR

revision = "0006_verification"
down_revision = "0005_agent"
branch_labels = None
depends_on = None


def upgrade():
    def identity(length):
        return sa.String(length).with_variant(
            VARCHAR(length, charset="ascii", collation="ascii_bin"), "mysql"
        )

    op.create_table(
        "verifications",
        sa.Column("run_id", identity(36), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("spec_hash", identity(64), nullable=False),
        sa.Column("snapshot_hash", identity(64), nullable=False),
        sa.Column("suite_hash", identity(64), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False),
        mysql_engine="InnoDB",
    )


def downgrade():
    op.drop_table("verifications")
