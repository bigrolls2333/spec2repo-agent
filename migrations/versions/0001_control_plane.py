"""Persist runs, idempotency decisions and transactional events."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.mysql import VARCHAR

revision = "0001_control_plane"
down_revision = None
branch_labels = None
depends_on = None


def binary_string(length):
    return sa.String(length).with_variant(
        VARCHAR(length, charset="ascii", collation="ascii_bin"), "mysql"
    )


def upgrade():
    op.create_table(
        "runs",
        sa.Column("id", binary_string(36), primary_key=True),
        sa.Column("owner_id", binary_string(64), nullable=False),
        sa.Column("spec", sa.JSON(), nullable=False),
        sa.Column("spec_hash", binary_string(64), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.CheckConstraint("version >= 0", name="ck_runs_version"),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_runs_owner_created_id", "runs", ["owner_id", "created_at", "id"])
    op.create_index("ix_runs_status_updated", "runs", ["status", "updated_at"])
    op.create_table(
        "idempotency_keys",
        sa.Column("owner_id", binary_string(64), primary_key=True),
        sa.Column("key", binary_string(128), primary_key=True),
        sa.Column("request_hash", binary_string(64), nullable=False),
        sa.Column(
            "run_id", binary_string(36), sa.ForeignKey("runs.id"), nullable=False, unique=True
        ),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_table(
        "outbox",
        sa.Column("id", binary_string(36), primary_key=True),
        sa.Column("run_id", binary_string(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime(), nullable=True),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_outbox_published_created", "outbox", ["published_at", "created_at"])
    op.create_table(
        "run_events",
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
            primary_key=True,
            autoincrement=True,
        ),
        sa.Column("run_id", binary_string(36), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        mysql_engine="InnoDB",
        mysql_charset="utf8mb4",
    )
    op.create_index("ix_run_events_run_id_id", "run_events", ["run_id", "id"])


def downgrade():
    op.drop_table("run_events")
    op.drop_table("outbox")
    op.drop_table("idempotency_keys")
    op.drop_table("runs")
