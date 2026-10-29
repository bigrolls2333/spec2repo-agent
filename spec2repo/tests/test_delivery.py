"""Fault windows exercised against real MySQL and Redis, no fakeredis."""

import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from uuid import uuid4

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import ResponseError
from redis.exceptions import TimeoutError as RedisTimeoutError
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from spec2repo.domain.spec import CoreSpec
from spec2repo.runtime.delivery import Relay, SimulationWorker
from spec2repo.runtime.streams import Streams, redis_client
from spec2repo.storage.models import Base, DispatchReceipt, OutboxEvent, Run, RunEvent
from spec2repo.storage.runs import RunRepository, utc_now

pytestmark = [pytest.mark.mysql, pytest.mark.redis]


@pytest.fixture
def transport_db(mysql_engine):
    with mysql_engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    return mysql_engine


@pytest.fixture
def streams():
    url = os.environ.get("SPEC2REPO_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis integration not configured")
    client = redis_client(url)
    # Never FLUSHDB: only delete this test's randomly named keys.
    assert client.connection_pool.connection_kwargs.get("db") == 15, "Use test Redis DB 15"
    client.ping()
    queue = Streams(client, stream=f"spec2repo:test:{uuid4()}")
    yield queue
    client.delete(queue.stream, queue.dead)
    client.close()


def create(engine, payload):
    run, _ = RunRepository(engine).create("alice", str(uuid4()), CoreSpec.model_validate(payload))
    with Session(engine) as session:
        row = session.scalar(select(OutboxEvent).where(OutboxEvent.run_id == run["id"]))
        return run, row.id


def receipts(engine):
    with Session(engine) as session:
        return session.scalar(select(func.count()).select_from(DispatchReceipt))


def stale(engine):
    with Session(engine) as session, session.begin():
        for row in session.scalars(select(OutboxEvent)):
            if row.published_at:
                row.published_at = utc_now() - timedelta(seconds=120)


def test_duplicate_notifications_commit_one_simulation(transport_db, streams, spec_payload):
    run, eid = create(transport_db, spec_payload)
    for _ in range(10):
        streams.publish(eid, run["id"], 0)
    messages = streams.read("one", count=10)
    worker = SimulationWorker(transport_db, streams, "one")
    assert [worker.handle(*m) for m in messages].count("simulated") == 1
    assert receipts(transport_db) == 1
    assert streams.client.xpending(streams.stream, streams.group)["pending"] == 0
    with Session(transport_db) as session:
        assert session.get(Run, run["id"]).status == "QUEUED"
        assert session.scalar(select(func.count()).select_from(RunEvent)) == 2


def test_publish_success_then_sql_rollback_can_republish(transport_db, streams, spec_payload):
    create(transport_db, spec_payload)
    relay = Relay(transport_db, streams)

    def fail_update(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith("UPDATE outbox"):
            raise RuntimeError("injected failure after XADD")

    event.listen(transport_db, "before_cursor_execute", fail_update)
    try:
        with pytest.raises(RuntimeError):
            relay.publish_one()
    finally:
        event.remove(transport_db, "before_cursor_execute", fail_update)
    assert streams.client.xlen(streams.stream) == 1
    assert relay.publish_one()
    assert streams.client.xlen(streams.stream) == 2
    worker = SimulationWorker(transport_db, streams, "one")
    for msg in streams.read("one", count=10):
        worker.handle(*msg)
    assert receipts(transport_db) == 1


def test_redis_failure_does_not_lose_database_task(
    transport_db, streams, spec_payload, monkeypatch
):
    run, eid = create(transport_db, spec_payload)
    relay = Relay(transport_db, streams)
    original = streams.publish

    def unavailable(*_args):
        raise RedisConnectionError("injected connection refusal")

    monkeypatch.setattr(streams, "publish", unavailable)
    with pytest.raises(RedisConnectionError):
        relay.publish_one()
    assert RunRepository(transport_db).get("alice", run["id"])["status"] == "QUEUED"
    with Session(transport_db) as session:
        assert session.get(OutboxEvent, eid).published_at is None
    monkeypatch.setattr(streams, "publish", original)
    assert relay.publish_one()
    assert SimulationWorker(transport_db, streams, "recovered").tick() == ["simulated"]


def test_commit_before_ack_then_reclaim_is_idempotent(
    transport_db, streams, spec_payload, monkeypatch
):
    create(transport_db, spec_payload)
    Relay(transport_db, streams).publish_one()
    msg = streams.read("dead")[0]
    original = streams.ack

    def lost_ack(_message_id):
        raise RedisTimeoutError("injected ACK timeout")

    monkeypatch.setattr(streams, "ack", lost_ack)
    with pytest.raises(RedisTimeoutError):
        SimulationWorker(transport_db, streams, "dead").handle(*msg)
    assert receipts(transport_db) == 1
    assert streams.client.xpending(streams.stream, streams.group)["pending"] == 1
    monkeypatch.setattr(streams, "ack", original)
    assert SimulationWorker(transport_db, streams, "replacement").tick(idle_ms=0) == ["duplicate"]
    assert receipts(transport_db) == 1
    assert streams.client.xpending(streams.stream, streams.group)["pending"] == 0


def test_processing_rollback_leaves_pending_for_recovery(transport_db, streams, spec_payload):
    create(transport_db, spec_payload)
    Relay(transport_db, streams).publish_one()
    msg = streams.read("dead")[0]

    def fail_receipt(_conn, _cursor, statement, _params, _context, _many):
        if statement.startswith("INSERT INTO dispatch_receipts"):
            raise RuntimeError("injected consumer failure")

    event.listen(transport_db, "before_cursor_execute", fail_receipt)
    try:
        with pytest.raises(RuntimeError):
            SimulationWorker(transport_db, streams, "dead").handle(*msg)
    finally:
        event.remove(transport_db, "before_cursor_execute", fail_receipt)
    assert receipts(transport_db) == 0
    assert streams.client.xpending(streams.stream, streams.group)["pending"] == 1
    assert SimulationWorker(transport_db, streams, "new").tick(idle_ms=0) == ["simulated"]


def test_stream_loss_reconciles_from_mysql(transport_db, streams, spec_payload):
    create(transport_db, spec_payload)
    relay = Relay(transport_db, streams)
    relay.publish_one()
    streams.read("lost")  # Lose both message and group/PEL.
    streams.client.delete(streams.stream)
    assert relay.reconcile() == 0  # Fresh records are not immediately replayed.
    stale(transport_db)
    assert relay.reconcile() == 1
    assert relay.publish_one()
    assert SimulationWorker(transport_db, streams, "new").tick() == ["simulated"]
    stale(transport_db)
    assert relay.reconcile() == 0  # A committed receipt suppresses further replay.


def test_cancelled_and_mismatched_messages_do_not_execute(transport_db, streams, spec_payload):
    run, eid = create(transport_db, spec_payload)
    RunRepository(transport_db).cancel("alice", run["id"])
    streams.publish(eid, run["id"], 0)
    worker = SimulationWorker(transport_db, streams, "one")
    assert worker.tick() == ["obsolete"]
    streams.publish(eid, str(uuid4()), 0)
    streams.client.xadd(streams.stream, {"junk": "do not persist raw payload"})
    for msg in streams.read("one", count=10):
        assert worker.handle(*msg) == "rejected"
    assert streams.client.xlen(streams.dead) == 2
    assert streams.client.xpending(streams.stream, streams.group)["pending"] == 0
    assert receipts(transport_db) == 1


def test_concurrent_duplicate_consumers(transport_db, streams, spec_payload):
    run, eid = create(transport_db, spec_payload)
    for _ in range(2):
        streams.publish(eid, run["id"], 0)
    messages = streams.read("assigned", count=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(SimulationWorker(transport_db, streams, f"w{i}").handle, *msg)
            for i, msg in enumerate(messages)
        ]
        assert sorted(f.result() for f in futures) == ["duplicate", "simulated"]
    assert receipts(transport_db) == 1


def test_rejection_sink_failure_does_not_ack(streams):
    streams.client.xadd(streams.stream, {"invalid": "message"})
    message_id, _ = streams.read("one")[0]
    streams.client.set(streams.dead, "wrong-type-fault")
    with pytest.raises(ResponseError):
        streams.reject(message_id, "invalid_envelope")
    assert streams.client.xpending(streams.stream, streams.group)["pending"] == 1
    streams.client.delete(streams.dead)
    streams.reject(message_id, "invalid_envelope")
    assert streams.client.xpending(streams.stream, streams.group)["pending"] == 0


def test_two_relays_publish_without_missing_events(transport_db, streams, spec_payload):
    for _ in range(20):
        create(transport_db, spec_payload)

    def publish_all():
        relay = Relay(transport_db, streams)
        while relay.publish_one():
            pass

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(publish_all) for _ in range(2)]
        for future in futures:
            future.result()
    rows = streams.client.xrange(streams.stream)
    assert len(rows) == len({fields["event_id"] for _, fields in rows}) == 20


def test_two_worker_processes_handle_twenty_jobs(transport_db, streams, spec_payload):
    for _ in range(20):
        create(transport_db, spec_payload)
    relay = Relay(transport_db, streams)
    while relay.publish_one():
        pass
    env = os.environ.copy()
    env.update(
        SPEC2REPO_DATABASE_URL=os.environ["SPEC2REPO_TEST_MYSQL_URL"],
        SPEC2REPO_REDIS_URL=os.environ["SPEC2REPO_TEST_REDIS_URL"],
        SPEC2REPO_STREAM=streams.stream,
    )
    args = [sys.executable, "-m", "spec2repo.runtime", "worker", "--simulate"]
    processes = []
    try:
        # Each independent process consumes one first, proving both identities work.
        for name in ["process-a", "process-b"]:
            processes.append(subprocess.Popen(args + ["--consumer", name, "--once"], env=env))
        for process in processes:
            assert process.wait(timeout=15) == 0
        assert receipts(transport_db) == 2
        processes.clear()
        for name in ["process-a", "process-b"]:
            processes.append(subprocess.Popen(args + ["--consumer", name], env=env))
        deadline = time.monotonic() + 15
        while receipts(transport_db) < 20 and time.monotonic() < deadline:
            assert all(p.poll() is None for p in processes)
            time.sleep(0.1)
        assert receipts(transport_db) == 20
        with Session(transport_db) as session:
            assert set(session.scalars(select(DispatchReceipt.consumer))) == {
                "process-a",
                "process-b",
            }
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(RunEvent)
                    .where(RunEvent.event_type == "dispatch.simulated")
                )
                == 20
            )
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=5)
