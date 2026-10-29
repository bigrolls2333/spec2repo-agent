"""At-least-once publication and database-idempotent simulated consumption."""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import exists, select
from sqlalchemy.orm import sessionmaker

from spec2repo.storage.models import DispatchReceipt, OutboxEvent, Run, RunEvent
from spec2repo.storage.runs import utc_now


class Relay:
    def __init__(self, engine, streams):
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.streams = streams

    def publish_one(self, *, run_id=None):
        # One bounded Redis request per transaction. A second relay skips this row.
        # A failed/uncertain publish rolls back; its retry may duplicate the notification.
        with self.sessions.begin() as session:
            event = session.scalar(
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at.is_(None),
                    OutboxEvent.run_id == run_id if run_id else True,
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if event is None:
                return False
            self.streams.publish(
                event.id, event.run_id, event.payload["version"], event.payload.get("generation", 0)
            )
            event.published_at = utc_now()
        return True

    def reconcile(self, *, stale_seconds=60, limit=100):
        """Republish unconsumed simulation notifications, even after Stream loss.

        Execution mode uses ExecutionRepository.reconcile instead of these
        simulation receipts; only its recovery service revokes expired leases.
        """
        cutoff = utc_now() - timedelta(seconds=stale_seconds)
        with self.sessions.begin() as session:
            rows = session.scalars(
                select(OutboxEvent)
                .where(
                    OutboxEvent.published_at < cutoff,
                    ~exists().where(DispatchReceipt.event_id == OutboxEvent.id),
                )
                .order_by(OutboxEvent.published_at, OutboxEvent.id)
                .limit(limit)
                .with_for_update(skip_locked=True)
            ).all()
            for row in rows:
                row.published_at = None
            return len(rows)


class SimulationWorker:
    """Only commit a transport experiment result; never execute code or mark SUCCEEDED."""

    def __init__(self, engine, streams, consumer):
        if not consumer or len(consumer) > 128:
            raise ValueError("consumer must contain 1 to 128 characters")
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.streams = streams
        self.consumer = consumer
        self.cursor = "0-0"

    def handle(self, message_id, fields):
        try:
            event_id = str(UUID(fields["event_id"]))
            run_id = str(UUID(fields["run_id"]))
            version = int(fields["version"])
            if version < 0:
                raise ValueError("negative version")
        except (KeyError, ValueError, TypeError):
            self.streams.reject(message_id, "invalid_envelope")
            return "rejected"

        with self.sessions.begin() as session:
            # Lock the run first; serialize duplicate processing and cancellation.
            run = session.scalar(select(Run).where(Run.id == run_id).with_for_update())
            event = session.get(OutboxEvent, event_id)
            valid = (
                run is not None
                and event is not None
                and event.run_id == run_id
                and event.event_type == "run.queued"
                and event.payload.get("version") == version
            )
            if not valid:
                outcome = "rejected"
            elif session.get(DispatchReceipt, event_id) is not None:
                outcome = "duplicate"
            else:
                outcome = (
                    "simulated" if run.status == "QUEUED" and run.version == version else "obsolete"
                )
                now = utc_now()
                session.add(
                    DispatchReceipt(
                        event_id=event_id, consumer=self.consumer, outcome=outcome, created_at=now
                    )
                )
                session.add(
                    RunEvent(
                        run_id=run_id,
                        event_type="dispatch." + outcome,
                        payload={
                            "event_id": event_id,
                            "consumer": self.consumer,
                            "simulation_only": True,
                        },
                        created_at=now,
                    )
                )
        # Commit first. A crash/ACK timeout leaves a replayable message, not lost work.
        if outcome == "rejected":
            self.streams.reject(message_id, "unknown_or_mismatched_event")
        else:
            self.streams.ack(message_id)
        return outcome

    def tick(self, *, idle_ms=30000):
        self.cursor, reclaimed = self.streams.reclaim(
            self.consumer,
            idle_ms=idle_ms,
            cursor=self.cursor,
        )
        results = [self.handle(mid, fields) for mid, fields in reclaimed]
        # Always read new work as well, avoiding starvation behind a long PEL scan.
        results.extend(self.handle(mid, fields) for mid, fields in self.streams.read(self.consumer))
        return results
