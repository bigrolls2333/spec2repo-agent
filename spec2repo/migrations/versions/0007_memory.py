"""Task memory and bounded tool evidence."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision = "0007_memory"
down_revision = "0006_verification"
branch_labels = depends_on = None


def identity(length):
    return sa.String(length).with_variant(
        VARCHAR(length, charset="ascii", collation="ascii_bin"), "mysql"
    )


def upgrade():
    op.create_table(
        "task_memories",
        sa.Column("run_id", identity(36), sa.ForeignKey("runs.id"), primary_key=True),
        sa.Column("generation", sa.Integer(), primary_key=True),
        sa.Column("spec_hash", identity(64), nullable=False),
        sa.Column("snapshot_hash", identity(64), nullable=True),
        sa.Column("body", sa.JSON(), nullable=False), mysql_engine="InnoDB",
    )
    op.create_table(
        "tool_evidence",
        sa.Column("id", identity(36), primary_key=True),
        sa.Column("run_id", identity(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("spec_hash", identity(64), nullable=False),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False), mysql_engine="InnoDB",
    )
    op.create_index("ix_tool_evidence_run_id", "tool_evidence", ["run_id"])


def downgrade():
    op.drop_table("tool_evidence")
    op.drop_table("task_memories")
