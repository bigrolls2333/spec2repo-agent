"""Durable plans, approval decisions and conservative run budgets."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR

revision = "0005_agent"
down_revision = "0004_execution"
branch_labels = None
depends_on = None


def upgrade():
    identity = sa.String(36).with_variant(
        VARCHAR(36, charset="ascii", collation="ascii_bin"), "mysql"
    )
    digest = sa.String(64).with_variant(
        VARCHAR(64, charset="ascii", collation="ascii_bin"), "mysql"
    )
    op.create_table(
        "agent_states",
        sa.Column("run_id", identity, sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("questions", sa.JSON(), nullable=False),
        sa.Column("snapshot_hash", digest, nullable=True),
        sa.Column("calls", sa.Integer(), nullable=False),
        sa.Column("reserved_tokens", sa.Integer(), nullable=False),
        sa.Column("reserved_seconds", sa.Integer(), nullable=False),
        mysql_engine="InnoDB",
    )
    op.create_table(
        "plans",
        sa.Column("run_id", identity, sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), primary_key=True),
        sa.Column("spec_hash", digest, nullable=False),
        sa.Column("plan_hash", digest, nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("reason", sa.String(4000), nullable=False),
        sa.Column("decision", sa.String(32), nullable=False),
        sa.Column(
            "created_at", sa.DateTime().with_variant(DATETIME(fsp=6), "mysql"), nullable=False
        ),
        mysql_engine="InnoDB",
    )
    op.create_table(
        "spec_revisions",
        sa.Column("run_id", identity, sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("spec_hash", digest, nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        mysql_engine="InnoDB",
    )


def downgrade():
    for name in ["spec_revisions", "plans", "agent_states"]:
        op.drop_table(name)
