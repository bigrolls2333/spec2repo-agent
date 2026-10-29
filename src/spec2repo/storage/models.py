"""Control-plane tables. No API keys, model credentials or filesystem contents."""

from datetime import datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
)
from sqlalchemy.dialects.mysql import DATETIME, VARCHAR
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def binary_string(length: int):
    # Identities and idempotency keys are case-sensitive even on MySQL's usual CI collation.
    return String(length).with_variant(
        VARCHAR(length, charset="ascii", collation="ascii_bin"), "mysql"
    )


class Base(DeclarativeBase):
    pass


UTC_TIMESTAMP = DateTime().with_variant(DATETIME(fsp=6), "mysql")


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_owner_created_id", "owner_id", "created_at", "id"),
        Index("ix_runs_status_updated", "status", "updated_at"),
        Index("ix_runs_status_lease", "status", "lease_until"),
        CheckConstraint("version >= 0", name="ck_runs_version"),
    )
    id: Mapped[str] = mapped_column(binary_string(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(binary_string(64))
    spec: Mapped[dict] = mapped_column(JSON)
    spec_hash: Mapped[str] = mapped_column(binary_string(64))
    status: Mapped[str] = mapped_column(String(32))
    version: Mapped[int] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)
    updated_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)
    generation: Mapped[int] = mapped_column(default=0, server_default="0")
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_until: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, nullable=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    owner_id: Mapped[str] = mapped_column(binary_string(64), primary_key=True)
    key: Mapped[str] = mapped_column(binary_string(128), primary_key=True)
    request_hash: Mapped[str] = mapped_column(binary_string(64))
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), unique=True)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)


class OutboxEvent(Base):
    __tablename__ = "outbox"
    __table_args__ = (Index("ix_outbox_published_created", "published_at", "created_at"),)
    id: Mapped[str] = mapped_column(binary_string(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)
    published_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, nullable=True)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_id_id", "run_id", "id"),)
    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    event_type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)


class DispatchReceipt(Base):
    """Durable simulation result; never evidence of successful code generation."""

    __tablename__ = "dispatch_receipts"
    event_id: Mapped[str] = mapped_column(ForeignKey("outbox.id"), primary_key=True)
    consumer: Mapped[str] = mapped_column(String(128))
    outcome: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)


class Attempt(Base):
    __tablename__ = "attempts"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(primary_key=True)
    worker: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32))
    started_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)
    ended_at: Mapped[datetime | None] = mapped_column(UTC_TIMESTAMP, nullable=True)


class Checkpoint(Base):
    __tablename__ = "checkpoints"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    stage: Mapped[int] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column()
    spec_hash: Mapped[str] = mapped_column(binary_string(64))
    snapshot_hash: Mapped[str] = mapped_column(binary_string(64))
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)


class AgentState(Base):
    __tablename__ = "agent_states"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    phase: Mapped[str] = mapped_column(String(32), default="PLAN")
    revision: Mapped[int] = mapped_column(default=0)
    questions: Mapped[list] = mapped_column(JSON, default=list)
    snapshot_hash: Mapped[str | None] = mapped_column(binary_string(64), nullable=True)
    calls: Mapped[int] = mapped_column(default=0)
    reserved_tokens: Mapped[int] = mapped_column(default=0)
    reserved_seconds: Mapped[int] = mapped_column(default=0)


class PlanVersion(Base):
    __tablename__ = "plans"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    revision: Mapped[int] = mapped_column(primary_key=True)
    spec_hash: Mapped[str] = mapped_column(binary_string(64))
    plan_hash: Mapped[str] = mapped_column(binary_string(64))
    body: Mapped[dict] = mapped_column(JSON)
    reason: Mapped[str] = mapped_column(String(4000))
    decision: Mapped[str] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)


class SpecRevision(Base):
    __tablename__ = "spec_revisions"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    version: Mapped[int] = mapped_column(primary_key=True)
    spec_hash: Mapped[str] = mapped_column(binary_string(64))
    body: Mapped[dict] = mapped_column(JSON)


class Verification(Base):
    __tablename__ = "verifications"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    sequence: Mapped[int] = mapped_column(primary_key=True)
    generation: Mapped[int] = mapped_column()
    spec_hash: Mapped[str] = mapped_column(binary_string(64))
    snapshot_hash: Mapped[str] = mapped_column(binary_string(64))
    suite_hash: Mapped[str] = mapped_column(binary_string(64))
    report: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTC_TIMESTAMP)


class TaskMemory(Base):
    __tablename__ = "task_memories"
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), primary_key=True)
    generation: Mapped[int] = mapped_column(primary_key=True)
    spec_hash: Mapped[str] = mapped_column(binary_string(64))
    snapshot_hash: Mapped[str | None] = mapped_column(binary_string(64), nullable=True)
    body: Mapped[dict] = mapped_column(JSON)


class ToolEvidence(Base):
    __tablename__ = "tool_evidence"
    id: Mapped[str] = mapped_column(binary_string(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"), index=True)
    generation: Mapped[int] = mapped_column()
    spec_hash: Mapped[str] = mapped_column(binary_string(64))
    body: Mapped[dict] = mapped_column(JSON)
    size_bytes: Mapped[int] = mapped_column()
