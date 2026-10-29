import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from spec2repo.domain.lifecycle import LifecycleError, RunStatus
from spec2repo.domain.spec import CoreSpec
from spec2repo.runtime.delivery import Relay, SimulationWorker
from spec2repo.runtime.executor import recover_and_cleanup
from spec2repo.runtime.streams import Streams, redis_client
from spec2repo.sandbox.docker import DockerSandbox, SandboxStopped
from spec2repo.storage.execution import ExecutionRepository, LeaseLost
from spec2repo.storage.models import Attempt, Base, Checkpoint, OutboxEvent, Run, RunEvent
from spec2repo.storage.runs import RunRepository
from spec2repo.storage.workspaces import Workspaces


@pytest.fixture
def execution_db(mysql_engine):
    with mysql_engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    return mysql_engine


@pytest.fixture
def execution_streams():
    url = os.environ.get("SPEC2REPO_TEST_REDIS_URL")
    if not url:
        pytest.skip("Redis not configured")
    client = redis_client(url)
    assert client.connection_pool.connection_kwargs.get("db") == 15
    client.ping()
    queue = Streams(client, stream=f"spec2repo:test:{uuid4()}", group="execution-v1")
    yield queue
    client.delete(queue.stream, queue.dead)
    client.close()


@pytest.fixture
def sandbox(tmp_path):
    if os.environ.get("SPEC2REPO_TEST_DOCKER") != "1":
        pytest.skip("Set SPEC2REPO_TEST_DOCKER=1 for real sandbox experiments")
    spaces = Workspaces(tmp_path / "runtime")
    return spaces, DockerSandbox(spaces)


def create(engine, payload):
    run, _ = RunRepository(engine).create("alice", uuid4().hex, CoreSpec.model_validate(payload))
    with Session(engine) as session:
        event = session.scalar(select(OutboxEvent).where(OutboxEvent.run_id == run["id"]))
        return run["id"], event.id


def expire(engine, run_id):
    with engine.begin() as connection:
        connection.execute(
            text("UPDATE runs SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=:id"),
            {"id": run_id},
        )


@pytest.mark.mysql
def test_one_owner_and_old_generation_cannot_commit(execution_db, spec_payload):
    run_id, event_id = create(execution_db, spec_payload)
    repo = ExecutionRepository(execution_db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(lambda worker: repo.claim(event_id, run_id, 0, 0, worker), ["a", "b"])
        )
    assert sorted(r[0] for r in results) == ["busy", "claimed"]
    old = next(r[1] for r in results if r[1])
    repo.checkpoint(old, 1, "a" * 64)
    expire(execution_db, run_id)
    with pytest.raises(LeaseLost):
        repo.heartbeat(old)  # An expired owner cannot resurrect itself, even before takeover.
    assert repo.recover() == 1
    with Session(execution_db) as session:
        run = session.get(Run, run_id)
        event = session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.run_id == run_id)
            .order_by(OutboxEvent.created_at.desc())
        )
        version, eid = run.version, event.id
    status, new = repo.claim(eid, run_id, version, old.generation, "replacement")
    assert status == "claimed" and new.generation == old.generation + 1
    for operation in [
        lambda: repo.heartbeat(old),
        lambda: repo.checkpoint(old, 2, "b" * 64),
        lambda: repo.park(old),
        lambda: repo.fail(old, "late"),
    ]:
        with pytest.raises(LeaseLost):
            operation()
    assert repo.checkpoints(new) == [(1, "a" * 64)]
    with pytest.raises(LeaseLost):
        repo.heartbeat(replace(new, worker="impostor"))


@pytest.mark.mysql
def test_cancellation_fences_commit_and_legacy_state_setter(execution_db, spec_payload):
    run_id, event_id = create(execution_db, spec_payload)
    repo = ExecutionRepository(execution_db)
    _, lease = repo.claim(event_id, run_id, 0, 0, "owner")
    with pytest.raises(LifecycleError):
        RunRepository(execution_db).change_status(
            "alice", run_id, RunStatus.SUCCEEDED, expected_version=1, verification_passed=True
        )
    assert RunRepository(execution_db).cancel("alice", run_id)["status"] == "CANCELLING"
    with pytest.raises(LeaseLost):
        repo.checkpoint(lease, 1, "c" * 64)
    assert repo.finish_cancel(run_id, lease.generation)
    assert not repo.finish_cancel(run_id, lease.generation)


@pytest.mark.mysql
def test_checkpoint_immutability_and_stage_order(execution_db, spec_payload):
    run_id, event_id = create(execution_db, spec_payload)
    repo = ExecutionRepository(execution_db)
    _, lease = repo.claim(event_id, run_id, 0, 0, "owner")
    with pytest.raises(ValueError):
        repo.park(lease)
    with pytest.raises(ValueError):
        repo.checkpoint(lease, 2, "b" * 64)
    repo.checkpoint(lease, 1, "a" * 64)
    repo.checkpoint(lease, 1, "a" * 64)
    with pytest.raises(ValueError):
        repo.checkpoint(lease, 1, "c" * 64)
    repo.checkpoint(lease, 2, "b" * 64)
    repo.park(lease)
    with pytest.raises(LeaseLost):
        repo.heartbeat(lease)
    with Session(execution_db) as session:
        assert session.get(Run, run_id).status == "WAITING_APPROVAL"
        assert session.scalar(select(func.count()).select_from(Checkpoint)) == 2


@pytest.mark.mysql
def test_recovery_is_bounded_and_emits_outbox(execution_db, spec_payload):
    run_id, eid = create(execution_db, spec_payload)
    repo = ExecutionRepository(execution_db)
    for generation in range(3):
        with Session(execution_db) as session:
            run = session.get(Run, run_id)
            eid = session.scalar(
                select(OutboxEvent.id)
                .where(OutboxEvent.run_id == run_id)
                .order_by(OutboxEvent.created_at.desc())
            )
            version = run.version
        assert repo.claim(eid, run_id, version, generation, f"w{generation}")[0] == "claimed"
        expire(execution_db, run_id)
        assert repo.recover() == 1
        assert repo.recover() == 0
    with Session(execution_db) as session:
        assert session.get(Run, run_id).status == "FAILED"
        assert session.scalar(select(func.count()).select_from(Attempt)) == 3
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 3


@pytest.mark.mysql
def test_checkpoint_and_recovery_transactions_rollback_on_event_failure(
    execution_db, spec_payload, monkeypatch
):
    run_id, event_id = create(execution_db, spec_payload)
    repo = ExecutionRepository(execution_db)
    _, lease = repo.claim(event_id, run_id, 0, 0, "owner")

    def fail_event(*_args):
        raise RuntimeError("injected event failure")

    monkeypatch.setattr(repo, "event", fail_event)
    with pytest.raises(RuntimeError, match="injected"):
        repo.checkpoint(lease, 1, "a" * 64)
    with Session(execution_db) as session:
        assert session.get(Checkpoint, (run_id, 1)) is None
    expire(execution_db, run_id)
    with pytest.raises(RuntimeError, match="injected"):
        repo.recover()
    with Session(execution_db) as session:
        run = session.get(Run, run_id)
        assert run.status == "RUNNING" and run.version == 1
        assert session.get(Attempt, (run_id, 1)).status == "RUNNING"
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.mysql
@pytest.mark.redis
def test_simulation_receipt_does_not_suppress_execution_reconciliation(
    execution_db, execution_streams, spec_payload
):
    run_id, eid = create(execution_db, spec_payload)
    relay = Relay(execution_db, execution_streams)
    relay.publish_one()
    # Phase-3 receipt exists, but is never accepted as real execution progress.
    SimulationWorker(execution_db, execution_streams, "simulation").tick()
    execution_streams.client.delete(execution_streams.stream)
    repo = ExecutionRepository(execution_db)
    assert repo.reconcile(stale_seconds=0) == 1
    assert relay.publish_one()
    msg = execution_streams.read("real")[0]
    assert repo.claim(eid, run_id, 0, 0, "real")[0] == "claimed"
    assert repo.reconcile(stale_seconds=0) == 0
    assert msg[1]["event_id"] == eid


@pytest.mark.docker
def test_container_permissions_and_host_secret_isolation(sandbox, monkeypatch):
    from spec2repo.storage.execution import Lease

    spaces, docker = sandbox
    lease = Lease(str(uuid4()), 1, "probe", "a" * 64)
    spaces.create(lease)
    (spaces.root / "platform-secret.txt").write_text("host-only")
    monkeypatch.setenv("SPEC2REPO_SECRET_PROBE", "must-not-enter-container")
    code = """
import os, pathlib, socket
assert os.getuid() == 65534
assert 'SPEC2REPO_SECRET_PROBE' not in os.environ
assert not pathlib.Path('/var/run/docker.sock').exists()
assert not pathlib.Path('/platform-secret.txt').exists()
assert not pathlib.Path('/workspace/../platform-secret.txt').exists()
status = pathlib.Path('/proc/self/status').read_text()
assert 'CapEff:\\t0000000000000000' in status
assert 'NoNewPrivs:\\t1' in status
try:
    pathlib.Path('/forbidden').write_text('x')
except OSError:
    pass
else:
    raise AssertionError('root filesystem writable')
sock = socket.socket()
sock.settimeout(1)
assert sock.connect_ex(('1.1.1.1', 443)) != 0
pathlib.Path('allowed.txt').write_text('ok')
print('isolation-ok')
"""
    output = docker.run(
        lease, ["python", "-c", code], authorized=lambda: None, stop=threading.Event()
    )
    assert "isolation-ok" in output
    assert (spaces.workspace(lease.run_id, 1) / "allowed.txt").read_text() == "ok"
    assert docker.inspect(lease.run_id, 1) is None


@pytest.mark.docker
def test_timeout_removes_container(sandbox):
    from spec2repo.storage.execution import Lease

    spaces, docker = sandbox
    lease = Lease(str(uuid4()), 1, "timeout", "a" * 64)
    spaces.create(lease)
    with pytest.raises(SandboxStopped):
        docker.run(
            lease,
            ["python", "-c", "import time; time.sleep(60)"],
            authorized=lambda: None,
            stop=threading.Event(),
            timeout=0.5,
        )
    assert docker.inspect(lease.run_id, 1) is None


def wait_until(predicate, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError("Condition not reached before deadline")


def start_worker(spaces, streams, *, delay=0):
    env = os.environ.copy()
    env.update(
        SPEC2REPO_DATABASE_URL=os.environ["SPEC2REPO_TEST_MYSQL_URL"],
        SPEC2REPO_REDIS_URL=os.environ["SPEC2REPO_TEST_REDIS_URL"],
        SPEC2REPO_STREAM=streams.stream,
        SPEC2REPO_WORKSPACE_ROOT=str(spaces.root),
    )
    return subprocess.Popen(
        [
            sys.executable,
            "-m",
            "spec2repo.runtime",
            "worker",
            "--execute-fixture",
            "--once",
            "--consumer",
            uuid4().hex,
            "--lease-seconds",
            "6",
            "--heartbeat-seconds",
            "1",
            "--fixture-delay",
            str(delay),
            "--idle-ms",
            "1",
        ],
        env=env,
    )


@pytest.mark.mysql
@pytest.mark.redis
@pytest.mark.docker
def test_killed_worker_restores_last_committed_stage(
    execution_db, execution_streams, sandbox, spec_payload
):
    spaces, docker = sandbox
    run_id, _eid = create(execution_db, spec_payload)
    relay = Relay(execution_db, execution_streams)
    relay.publish_one()
    process = start_worker(spaces, execution_streams, delay=60)
    replacement = None
    try:
        old_workspace = spaces.workspace(run_id, 1)
        wait_until(lambda: (old_workspace / "stage2.started").exists())
        with Session(execution_db) as session:
            checkpoint = session.get(Checkpoint, (run_id, 1))
            digest = checkpoint.snapshot_hash
            run = session.get(Run, run_id)
            from spec2repo.storage.execution import Lease

            old = Lease(run_id, 1, run.lease_owner, run.spec_hash)
        process.kill()
        process.wait(timeout=5)
        repo = ExecutionRepository(execution_db, lease_seconds=6)
        # Real expiry, not a fabricated timestamp in this process-kill experiment.
        wait_until(lambda: repo.recover() == 1, timeout=12)
        recover_and_cleanup(repo, docker)
        assert docker.inspect(run_id, 1) is None
        with pytest.raises(LeaseLost):
            repo.checkpoint(old, 2, "f" * 64)
        # A late writer in the abandoned workspace cannot alter the immutable snapshot.
        (old_workspace / "prepared.txt").write_text("stale-worker-corruption")
        relay.publish_one()
        replacement = start_worker(spaces, execution_streams)
        assert replacement.wait(timeout=45) == 0
        with Session(execution_db) as session:
            assert session.get(Run, run_id).status == "WAITING_APPROVAL"
            assert session.get(Run, run_id).generation == 2
            first = session.get(Checkpoint, (run_id, 1))
            assert first.generation == 1 and first.snapshot_hash == digest
            assert session.get(Checkpoint, (run_id, 2)).generation == 2
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(RunEvent)
                    .where(RunEvent.run_id == run_id, RunEvent.event_type == "execution.checkpoint")
                )
                == 2
            )
        assert (spaces.workspace(run_id, 2) / "prepared.txt").read_text() == "prepared"
        assert (spaces.workspace(run_id, 2) / "checked.txt").read_text() == "checked"
    finally:
        for child in [process, replacement]:
            if child and child.poll() is None:
                child.kill()
                child.wait(timeout=5)
        for generation in [1, 2]:
            docker.remove(run_id, generation)


@pytest.mark.mysql
@pytest.mark.redis
@pytest.mark.docker
def test_cancel_running_container_cleans_up(execution_db, execution_streams, sandbox, spec_payload):
    spaces, docker = sandbox
    run_id, _eid = create(execution_db, spec_payload)
    Relay(execution_db, execution_streams).publish_one()
    process = start_worker(spaces, execution_streams, delay=60)
    try:
        wait_until(lambda: (spaces.workspace(run_id, 1) / "stage2.started").exists())
        info = docker.inspect(run_id, 1)
        host = info["HostConfig"]
        assert host["NetworkMode"] == "none" and host["ReadonlyRootfs"]
        assert host["Memory"] == 256 * 1024 * 1024 and host["PidsLimit"] == 64
        assert host["NanoCpus"] == 1_000_000_000
        assert info["Config"]["User"] == "65534:65534"
        assert len(info["Mounts"]) == 1 and info["Mounts"][0]["Destination"] == "/workspace"
        assert RunRepository(execution_db).cancel("alice", run_id)["status"] == "CANCELLING"
        assert process.wait(timeout=20) == 0
        assert RunRepository(execution_db).get("alice", run_id)["status"] == "CANCELLED"
        assert docker.inspect(run_id, 1) is None
        with Session(execution_db) as session:
            assert session.get(Checkpoint, (run_id, 2)) is None
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        docker.remove(run_id, 1)


@pytest.mark.mysql
@pytest.mark.redis
@pytest.mark.docker
def test_independent_heartbeat_keeps_long_command_owned(
    execution_db, execution_streams, sandbox, spec_payload
):
    spaces, docker = sandbox
    run_id, event_id = create(execution_db, spec_payload)
    Relay(execution_db, execution_streams).publish_one()
    process = start_worker(spaces, execution_streams, delay=10)
    try:
        wait_until(lambda: (spaces.workspace(run_id, 1) / "stage2.started").exists())
        time.sleep(7)  # Longer than the six-second lease while the command is blocked.
        repo = ExecutionRepository(execution_db, lease_seconds=6)
        assert repo.recover() == 0
        assert repo.claim(event_id, run_id, 0, 0, "contender") == ("busy", None)
        assert process.wait(timeout=30) == 0
        with Session(execution_db) as session:
            run = session.get(Run, run_id)
            assert run.status == "WAITING_APPROVAL" and run.generation == 1
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        docker.remove(run_id, 1)
