from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from spec2repo.domain.lifecycle import RunStatus, StaleVersionError
from spec2repo.domain.spec import CoreSpec
from spec2repo.storage.models import Base, IdempotencyKey, OutboxEvent, Run, RunEvent
from spec2repo.storage.runs import RunRepository


def test_migration_matches_models(db_engine):
    with db_engine.connect() as connection:
        differences = compare_metadata(MigrationContext.configure(connection), Base.metadata)
        assert differences == []


def test_failed_outbox_insert_rolls_back_entire_creation(db_engine, spec_payload):
    def break_outbox(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("INSERT INTO OUTBOX"):
            raise RuntimeError("injected failure before commit")

    event.listen(db_engine, "before_cursor_execute", break_outbox)
    try:
        with pytest.raises(RuntimeError, match="injected"):
            RunRepository(db_engine).create(
                "alice", "atomic", CoreSpec.model_validate(spec_payload)
            )
    finally:
        event.remove(db_engine, "before_cursor_execute", break_outbox)
    with Session(db_engine) as session:
        for model in [Run, IdempotencyKey, OutboxEvent, RunEvent]:
            assert session.scalar(select(func.count()).select_from(model)) == 0


def test_stale_version_does_not_add_event(db_engine, spec_payload):
    repository = RunRepository(db_engine)
    run, _ = repository.create("alice", "versions", CoreSpec.model_validate(spec_payload))
    repository.change_status("alice", run["id"], RunStatus.RUNNING, expected_version=0)
    with pytest.raises(StaleVersionError):
        repository.change_status("alice", run["id"], RunStatus.CANCELLED, expected_version=0)
    assert len(repository.events("alice", run["id"])) == 2


def test_mysql_concurrent_idempotency(db_engine, spec_payload):
    if db_engine.dialect.name != "mysql":
        pytest.skip("InnoDB concurrency guarantee is tested on MySQL only")
    repository = RunRepository(db_engine)
    spec = CoreSpec.model_validate(spec_payload)
    start = Barrier(10)

    def create(_index):
        start.wait(timeout=10)
        return repository.create("alice", "same-request", spec)

    with ThreadPoolExecutor(max_workers=10) as pool:
        results = list(pool.map(create, range(10)))
    assert len({payload["id"] for payload, _replayed in results}) == 1
    assert sum(not replayed for _payload, replayed in results) == 1
    with Session(db_engine) as session:
        for model in [Run, IdempotencyKey, OutboxEvent, RunEvent]:
            assert session.scalar(select(func.count()).select_from(model)) == 1


def test_mysql_two_stale_readers_only_one_update_commits(db_engine, spec_payload):
    if db_engine.dialect.name != "mysql":
        pytest.skip("InnoDB concurrency guarantee is tested on MySQL only")
    repository = RunRepository(db_engine)
    run, _ = repository.create("alice", "race", CoreSpec.model_validate(spec_payload))
    repository.change_status("alice", run["id"], RunStatus.RUNNING, expected_version=0)
    both_read = Barrier(2)

    # Force both requests to read RUNNING/version=1 before either sends UPDATE.
    def before_update(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.lstrip().upper().startswith("UPDATE RUNS"):
            both_read.wait(timeout=10)

    event.listen(db_engine, "before_cursor_execute", before_update)

    def finish(target):
        try:
            repository.change_status(
                "alice",
                run["id"],
                target,
                expected_version=1,
                verification_passed=True,
            )
            return "committed"
        except StaleVersionError:
            return "stale"

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(finish, [RunStatus.SUCCEEDED, RunStatus.CANCELLING]))
    finally:
        event.remove(db_engine, "before_cursor_execute", before_update)
    assert sorted(outcomes) == ["committed", "stale"]
    assert repository.get("alice", run["id"])["version"] == 2
    assert len(repository.events("alice", run["id"])) == 3
