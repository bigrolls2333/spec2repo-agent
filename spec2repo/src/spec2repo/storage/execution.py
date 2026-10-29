"""MySQL is the sole execution authority. All fencing decisions use DB time."""

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4

from sqlalchemy import func, select, text
from sqlalchemy.orm import sessionmaker

from spec2repo.storage.models import Attempt, Checkpoint, OutboxEvent, Run, RunEvent


class LeaseLost(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    run_id: str
    generation: int
    worker: str
    spec_hash: str


class ExecutionRepository:
    def __init__(self, engine, *, lease_seconds=60):
        if engine.dialect.name != "mysql":
            raise ValueError("Execution leases require MySQL")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("lease_seconds must be between 1 and 3600")
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.lease_seconds = lease_seconds

    @staticmethod
    def now(session):
        return session.scalar(text("SELECT UTC_TIMESTAMP(6)"))

    @staticmethod
    def event(session, run, kind, payload, now):
        session.add(RunEvent(run_id=run.id, event_type=kind, payload=payload, created_at=now))

    def locked(self, session, run_id):
        return session.scalar(select(Run).where(Run.id == run_id).with_for_update())

    def guard(self, session, lease):
        run = self.locked(session, lease.run_id)
        now = self.now(session)  # Read after acquiring lock, never before a potentially long wait.
        if (
            run is None
            or run.status != "RUNNING"
            or run.generation != lease.generation
            or run.lease_owner != lease.worker
            or run.lease_until is None
            or run.lease_until <= now
            or run.spec_hash != lease.spec_hash
        ):
            raise LeaseLost("Execution ownership expired, cancelled or superseded")
        return run, now

    def claim(self, event_id, run_id, version, generation, worker):
        if not worker or len(worker) > 128:
            raise ValueError("Invalid worker identity")
        with self.sessions.begin() as session:
            run = self.locked(session, run_id)
            event = session.get(OutboxEvent, event_id)
            if (
                run is None
                or event is None
                or event.run_id != run_id
                or event.event_type != "run.queued"
                or event.payload.get("version") != version
                or event.payload.get("generation", 0) != generation
            ):
                return "invalid", None
            if run.status == "RUNNING" and run.lease_until is not None:
                return "busy", None  # Only recovery, not message idle, can revoke ownership.
            if run.status != "QUEUED" or run.version != version or run.generation != generation:
                return "obsolete", None
            now = self.now(session)
            run.generation += 1
            run.version += 1
            run.status = "RUNNING"
            run.lease_owner = worker
            run.lease_until = now + timedelta(seconds=self.lease_seconds)
            run.updated_at = now
            session.add(
                Attempt(
                    run_id=run.id,
                    generation=run.generation,
                    worker=worker,
                    status="RUNNING",
                    started_at=now,
                )
            )
            self.event(session, run, "execution.claimed", {"generation": run.generation}, now)
            return "claimed", Lease(run.id, run.generation, worker, run.spec_hash)

    def heartbeat(self, lease):
        with self.sessions.begin() as session:
            run, now = self.guard(session, lease)
            run.lease_until = now + timedelta(seconds=self.lease_seconds)

    def assert_owned(self, lease):
        with self.sessions.begin() as session:
            self.guard(session, lease)

    def checkpoints(self, lease):
        with self.sessions.begin() as session:
            self.guard(session, lease)
            rows = session.scalars(
                select(Checkpoint)
                .where(Checkpoint.run_id == lease.run_id)
                .order_by(Checkpoint.stage)
            ).all()
            if any(row.spec_hash != lease.spec_hash for row in rows):
                raise ValueError("Checkpoint specification mismatch")
            return [(row.stage, row.snapshot_hash) for row in rows]

    def checkpoint(self, lease, stage, snapshot_hash):
        if (
            stage < 1
            or len(snapshot_hash) != 64
            or any(c not in "0123456789abcdef" for c in snapshot_hash)
        ):
            raise ValueError("Invalid checkpoint")
        with self.sessions.begin() as session:
            run, now = self.guard(session, lease)
            previous = session.get(Checkpoint, (run.id, stage))
            if previous:
                if previous.snapshot_hash != snapshot_hash:
                    raise ValueError("Committed checkpoints are immutable")
                return
            if stage > 1 and session.get(Checkpoint, (run.id, stage - 1)) is None:
                raise ValueError("Previous stage is not committed")
            session.add(
                Checkpoint(
                    run_id=run.id,
                    stage=stage,
                    generation=lease.generation,
                    spec_hash=lease.spec_hash,
                    snapshot_hash=snapshot_hash,
                    created_at=now,
                )
            )
            self.event(
                session,
                run,
                "execution.checkpoint",
                {"stage": stage, "generation": lease.generation, "snapshot_hash": snapshot_hash},
                now,
            )

    def park(self, lease):
        """The runtime fixture ends in durable waiting, never fake SUCCEEDED."""
        with self.sessions.begin() as session:
            run, now = self.guard(session, lease)
            if session.get(Checkpoint, (run.id, 2)) is None:
                raise ValueError("Fixture stages must be committed before waiting")
            run.status = "WAITING_APPROVAL"
            run.version += 1
            run.lease_owner = run.lease_until = None
            run.updated_at = now
            attempt = session.get(Attempt, (run.id, lease.generation))
            attempt.status, attempt.ended_at = "WAITING_APPROVAL", now
            self.event(
                session, run, "execution.fixture_completed", {"generation": lease.generation}, now
            )

    def fail(self, lease, reason):
        with self.sessions.begin() as session:
            run, now = self.guard(session, lease)
            run.status = "FAILED"
            run.version += 1
            run.lease_owner = run.lease_until = None
            run.updated_at = now
            attempt = session.get(Attempt, (run.id, lease.generation))
            attempt.status, attempt.ended_at = "FAILED", now
            self.event(
                session,
                run,
                "execution.failed",
                {"reason": reason, "generation": lease.generation},
                now,
            )

    def recover(self, *, limit=100):
        """Revoke expired attempts and transactionally create new dispatch intents."""
        with self.sessions.begin() as session:
            now = self.now(session)
            runs = session.scalars(
                select(Run)
                .where(Run.status == "RUNNING", Run.lease_until <= now)
                .order_by(Run.lease_until)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for run in runs:
                losses = session.scalar(
                    select(func.count()).select_from(Attempt)
                    .where(Attempt.run_id == run.id, Attempt.status == "LOST")
                )
                attempt = session.get(Attempt, (run.id, run.generation))
                attempt.status, attempt.ended_at = "LOST", now
                run.version += 1
                run.status = "FAILED" if losses >= 2 else "QUEUED"
                run.lease_owner = run.lease_until = None
                run.updated_at = now
                if run.status == "QUEUED":
                    session.add(
                        OutboxEvent(
                            id=str(uuid4()),
                            run_id=run.id,
                            event_type="run.queued",
                            payload={
                                "run_id": run.id,
                                "version": run.version,
                                "generation": run.generation,
                            },
                            created_at=now,
                        )
                    )
                self.event(
                    session,
                    run,
                    "execution.requeued" if run.status == "QUEUED" else "execution.exhausted",
                    {"generation": run.generation},
                    now,
                )
            return len(runs)

    def reconcile(self, *, stale_seconds=60, limit=100):
        # Execution mode ignores phase-3 simulation receipts. Redis can be rebuilt.
        with self.sessions.begin() as session:
            cutoff = self.now(session) - timedelta(seconds=stale_seconds)
            runs = session.scalars(
                select(Run)
                .where(Run.status == "QUEUED", Run.updated_at < cutoff)
                .order_by(Run.updated_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for run in runs:
                rows = session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.run_id == run.id, OutboxEvent.event_type == "run.queued"
                    )
                ).all()
                for row in rows:
                    if row.payload.get("version") == run.version:
                        row.published_at = None
                run.updated_at = self.now(session)
            return len(runs)

    def cleanup_candidates(self):
        with self.sessions() as session:
            rows = session.execute(
                select(Attempt.run_id, Attempt.generation, Run.status, Attempt.status)
                .join(Run, Run.id == Attempt.run_id)
                .where((Attempt.status != "RUNNING") | (Run.status == "CANCELLING"))
            ).all()
            return rows

    def finish_cancel(self, run_id, generation):
        # Called only after the corresponding container has been removed.
        with self.sessions.begin() as session:
            run = self.locked(session, run_id)
            if run.status != "CANCELLING" or run.generation != generation:
                return False
            now = self.now(session)
            run.status = "CANCELLED"
            run.version += 1
            run.lease_owner = run.lease_until = None
            run.updated_at = now
            attempt = session.get(Attempt, (run.id, generation))
            attempt.status, attempt.ended_at = "CANCELLED", now
            self.event(session, run, "execution.cancelled", {"generation": generation}, now)
            return True
