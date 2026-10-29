"""Leased fixture execution to exercise recovery before integrating the agent."""

import threading
from uuid import UUID

from sqlalchemy.exc import SQLAlchemyError

from spec2repo.domain.lifecycle import LifecycleError
from spec2repo.harness.model import ModelError
from spec2repo.harness.progress import AgentStalled
from spec2repo.sandbox.docker import SandboxError
from spec2repo.storage.agent import BudgetExceeded
from spec2repo.storage.execution import ExecutionRepository, LeaseLost
from spec2repo.storage.workspaces import SnapshotError


class Heartbeat:
    def __init__(self, repository, lease, interval):
        self.repository, self.lease, self.interval = repository, lease, interval
        self.halt, self.lost = threading.Event(), threading.Event()
        self.thread = threading.Thread(target=self.loop, daemon=True)

    def loop(self):
        while not self.halt.wait(self.interval):
            try:
                self.repository.heartbeat(self.lease)
            except (LeaseLost, SQLAlchemyError):
                self.lost.set()  # Fail closed when authority cannot be established.
                return

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.halt.set()
        self.thread.join(timeout=15)


class ExecutionWorker:
    def __init__(
        self,
        engine,
        streams,
        consumer,
        workspaces,
        sandbox,
        *,
        lease_seconds=60,
        heartbeat_seconds=10,
        fixture_delay=0,
    ):
        if heartbeat_seconds <= 0 or heartbeat_seconds >= lease_seconds / 2:
            raise ValueError("Heartbeat interval must be less than half the lease")
        if not 0 <= fixture_delay <= 300:
            raise ValueError("Fixture delay must be between 0 and 300 seconds")
        self.repository = ExecutionRepository(engine, lease_seconds=lease_seconds)
        self.streams, self.consumer = streams, consumer
        self.workspaces, self.sandbox = workspaces, sandbox
        self.interval, self.delay = heartbeat_seconds, fixture_delay
        self.cursor = "0-0"

    def handle(self, message_id, fields):
        try:
            event_id, run_id = str(UUID(fields["event_id"])), str(UUID(fields["run_id"]))
            version, generation = int(fields["version"]), int(fields.get("generation", 0))
        except (ValueError, TypeError, KeyError):
            self.streams.reject(message_id, "invalid_envelope")
            return "rejected"
        outcome, lease = self.repository.claim(event_id, run_id, version, generation, self.consumer)
        if outcome == "invalid":
            self.streams.reject(message_id, "unknown_or_mismatched_event")
            return outcome
        if outcome == "busy":
            return outcome  # Never ACK another live execution based solely on message idle.
        if outcome == "obsolete":
            self.streams.ack(message_id)
            return outcome
        try:
            with Heartbeat(self.repository, lease, self.interval) as heartbeat:
                self.execute(lease, heartbeat.lost)
        except LeaseLost:
            self.sandbox.remove(lease.run_id, lease.generation)
            self.repository.finish_cancel(lease.run_id, lease.generation)
            return "ownership_lost"  # Pending is resolved after durable waiting/recovery.
        except (SandboxError, SnapshotError, OSError, ModelError, BudgetExceeded,
                LifecycleError, AgentStalled) as exc:
            self.sandbox.remove(lease.run_id, lease.generation)
            try:
                self.repository.fail(lease, type(exc).__name__)
            except LeaseLost:
                self.repository.finish_cancel(lease.run_id, lease.generation)
                return "ownership_lost"
            self.streams.ack(message_id)
            return "failed"
        self.streams.ack(message_id)
        return "waiting"

    def execute(self, lease, stop):
        workspace = self.workspaces.create(lease)
        stages = self.repository.checkpoints(lease)
        completed = stages[-1][0] if stages else 0
        if stages:
            self.workspaces.restore(stages[-1][1], workspace)
        commands = [
            "from pathlib import Path; Path('prepared.txt').write_text('prepared', encoding='utf-8')",
            (
                "from pathlib import Path; import time; "
                "assert Path('prepared.txt').read_text() == 'prepared'; "
                "Path('stage2.started').write_text('started'); "
                f"time.sleep({self.delay!r}); "
                "Path('checked.txt').write_text('checked', encoding='utf-8')"
            ),
        ]
        for stage, command in enumerate(commands, start=1):
            if stage <= completed:
                continue
            self.repository.assert_owned(lease)
            self.sandbox.run(
                lease,
                ["python", "-c", command],
                authorized=lambda: self.repository.assert_owned(lease),
                stop=stop,
                timeout=max(30, self.delay + 10),
            )
            digest = self.workspaces.snapshot(workspace)
            self.workspaces.verify(digest)
            if stop.is_set():
                raise LeaseLost("Heartbeat authority lost")
            self.repository.checkpoint(lease, stage, digest)
        if stop.is_set():
            raise LeaseLost("Heartbeat authority lost")
        self.repository.park(lease)

    def tick(self, *, idle_ms=30000):
        self.cursor, pending = self.streams.reclaim(
            self.consumer, idle_ms=idle_ms, cursor=self.cursor, count=1
        )
        results = [self.handle(*row) for row in pending]
        results.extend(self.handle(*row) for row in self.streams.read(self.consumer))
        return results


def recover_and_cleanup(repository, sandbox):
    recovered = repository.recover()
    for run_id, generation, status, _attempt_status in repository.cleanup_candidates():
        sandbox.remove(run_id, generation)
        if status == "CANCELLING":
            repository.finish_cancel(run_id, generation)
    repository.reconcile()
    from spec2repo.storage.verification import VerificationRepository

    VerificationRepository(repository.sessions.kw["bind"]).schedule()
    return recovered
