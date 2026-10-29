"""Short transactions, durable idempotency and conditional state updates."""

import re
from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from spec2repo.domain.lifecycle import (
    LifecycleError,
    RunSnapshot,
    RunStatus,
    StaleVersionError,
    transition,
)
from spec2repo.domain.spec import CoreSpec
from spec2repo.storage.models import IdempotencyKey, OutboxEvent, Run, RunEvent


class RunNotFound(LookupError):
    pass


class IdempotencyConflict(ValueError):
    pass


def utc_now():
    # MySQL DATETIME stores no timezone: all stored values explicitly represent UTC.
    return datetime.now(UTC).replace(tzinfo=None)


def run_payload(run: Run):
    return {
        "id": run.id,
        "status": run.status,
        "version": run.version,
        "generation": run.generation,
        "spec_id": run.spec["id"],
        "spec_version": run.spec["version"],
        "spec_hash": run.spec_hash,
        "spec": run.spec,
        "created_at": run.created_at.replace(tzinfo=UTC),
        "updated_at": run.updated_at.replace(tzinfo=UTC),
    }


class RunRepository:
    def __init__(self, engine: Engine):
        self.sessions = sessionmaker(engine, expire_on_commit=False)

    def create(self, owner: str, key: str, spec: CoreSpec) -> tuple[dict, bool]:
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
            raise ValueError("Invalid idempotency key")
        fingerprint = spec.fingerprint()
        try:
            with self.sessions.begin() as session:
                if existing := self._lookup_key(session, owner, key):
                    return self._replay(session, existing, fingerprint), True
                now = utc_now()
                status = (
                    RunStatus.QUEUED if spec.ready_for_planning else RunStatus.WAITING_CLARIFICATION
                )
                run = Run(
                    id=str(uuid4()),
                    owner_id=owner,
                    spec=spec.model_dump(mode="json"),
                    spec_hash=fingerprint,
                    status=status.value,
                    version=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(run)
                session.flush()
                session.add(
                    IdempotencyKey(
                        owner_id=owner,
                        key=key,
                        request_hash=fingerprint,
                        run_id=run.id,
                        created_at=now,
                    )
                )
                # Force the unique constraint before recording the event/outbox.
                session.flush()
                self._event(session, run.id, "run.created", {"status": status.value}, now)
                if status == RunStatus.QUEUED:
                    session.add(
                        OutboxEvent(
                            id=str(uuid4()),
                            run_id=run.id,
                            event_type="run.queued",
                            payload={"run_id": run.id, "version": 0, "generation": 0},
                            created_at=now,
                        )
                    )
                session.flush()
                payload = run_payload(run)
            return payload, False
        except IntegrityError:
            # A competing request can win after the initial lookup. The failed
            # transaction (including its newly inserted run) has been rolled back.
            # A fresh session avoids reusing the old REPEATABLE READ snapshot.
            with self.sessions() as session:
                existing = self._lookup_key(session, owner, key)
                if existing is None:
                    raise
                return self._replay(session, existing, fingerprint), True

    @staticmethod
    def _lookup_key(session: Session, owner: str, key: str):
        return session.get(IdempotencyKey, (owner, key))

    @staticmethod
    def _replay(session: Session, entry: IdempotencyKey, fingerprint: str):
        if entry.request_hash != fingerprint:
            raise IdempotencyConflict("Idempotency-Key already used with different content")
        return run_payload(session.get(Run, entry.run_id))

    @staticmethod
    def _owned(session: Session, owner: str, run_id: str):
        run = session.scalar(select(Run).where(Run.id == run_id, Run.owner_id == owner))
        if run is None:
            raise RunNotFound("Run not found")
        return run

    @staticmethod
    def _event(session: Session, run_id: str, kind: str, payload: dict, now: datetime):
        session.add(RunEvent(run_id=run_id, event_type=kind, payload=payload, created_at=now))

    def get(self, owner: str, run_id: str):
        with self.sessions() as session:
            return run_payload(self._owned(session, owner, run_id))

    def list(self, owner: str, *, limit: int = 20, offset: int = 0):
        with self.sessions() as session:
            runs = session.scalars(
                select(Run)
                .where(Run.owner_id == owner)
                .order_by(Run.created_at.desc(), Run.id.desc())
                .limit(limit)
                .offset(offset)
            )
            return [run_payload(run) for run in runs]

    def events(self, owner: str, run_id: str, *, after: int = 0, limit: int = 100):
        with self.sessions() as session:
            self._owned(session, owner, run_id)
            rows = session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id, RunEvent.id > after)
                .order_by(RunEvent.id)
                .limit(limit)
            )
            return [
                {
                    "id": row.id,
                    "event_type": row.event_type,
                    "payload": row.payload,
                    "created_at": row.created_at.replace(tzinfo=UTC),
                }
                for row in rows
            ]

    def change_status(
        self,
        owner: str,
        run_id: str,
        target: RunStatus,
        *,
        expected_version: int,
        verification_passed: bool = False,
    ):
        """Internal operation, never exposed as an arbitrary client status setter.

        Leased workers use their fenced repositories. This CAS entry point only
        permits cancellation while a lease is present; version is not a fencing token.
        """
        with self.sessions.begin() as session:
            run = self._owned(session, owner, run_id)
            if run.lease_owner is not None and target != RunStatus.CANCELLING:
                raise LifecycleError("Leased execution requires fenced operations")
            next_state = transition(
                RunSnapshot(RunStatus(run.status), run.version),
                target,
                expected_version=expected_version,
                verification_passed=verification_passed,
            )
            now = utc_now()
            changed = session.execute(
                update(Run)
                .where(
                    Run.id == run_id,
                    Run.owner_id == owner,
                    Run.version == expected_version,
                    Run.status == run.status,
                )
                .values(status=target.value, version=next_state.version, updated_at=now),
                execution_options={"synchronize_session": False},
            )
            if changed.rowcount != 1:
                raise StaleVersionError("Run changed concurrently; read the current version")
            self._event(
                session,
                run_id,
                "run.status_changed",
                {
                    "from": run.status,
                    "to": target.value,
                    "version": next_state.version,
                },
                now,
            )
            session.refresh(run)
            payload = run_payload(run)
        return payload

    def cancel(self, owner: str, run_id: str):
        # Re-read after a CAS conflict. This is a bounded retry of the decision,
        # not blindly replaying a stale desired status.
        for _ in range(3):
            run = self.get(owner, run_id)
            status = RunStatus(run["status"])
            if status in {RunStatus.CANCELLED, RunStatus.CANCELLING}:
                return run
            target = RunStatus.CANCELLING if status == RunStatus.RUNNING else RunStatus.CANCELLED
            try:
                return self.change_status(owner, run_id, target, expected_version=run["version"])
            except StaleVersionError:
                continue
        raise StaleVersionError("Run changed repeatedly; retry after reading its status")
