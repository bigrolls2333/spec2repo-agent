import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_agent import (
    ScriptedModel,
    claim,
    create,
    decision,
    plan_for,
)
from test_agent import (
    agent_db as agent_db,  # noqa: PLC0414
)
from test_execution import execution_streams as execution_streams  # noqa: PLC0414
from test_execution import expire
from test_execution import sandbox as sandbox  # noqa: PLC0414

from spec2repo.api.app import create_app
from spec2repo.domain.lifecycle import LifecycleError
from spec2repo.domain.spec import CoreSpec
from spec2repo.harness.verification import SUITES, Verifier
from spec2repo.runtime.agent_worker import AgentWorker
from spec2repo.runtime.delivery import Relay
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.execution import LeaseLost
from spec2repo.storage.models import OutboxEvent, Run, Verification
from spec2repo.storage.verification import VerificationRepository

ROOT = Path(__file__).resolve().parents[1]
NOTES = '''import os, sqlite3
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
app = FastAPI()
db = sqlite3.connect(os.environ.get('NOTES_DB', 'notes.db'), check_same_thread=False)
db.execute('CREATE TABLE IF NOT EXISTS notes(id INTEGER PRIMARY KEY, text TEXT)')
db.commit()
class Note(BaseModel):
    text: str = Field(min_length=1, max_length=200)
@app.post('/notes', status_code=201)
def create(note: Note):
    cur = db.execute('INSERT INTO notes(text) VALUES (?)', (note.text,))
    db.commit()
    return {'id': cur.lastrowid, 'text': note.text}
@app.get('/notes/{id}')
def read(id: int):
    row = db.execute('SELECT id,text FROM notes WHERE id=?', (id,)).fetchone()
    if row is None: raise HTTPException(404)
    return {'id': row[0], 'text': row[1]}
'''


def prepared(engine, payload):
    run = create(engine, payload)
    repo = AgentRepository(engine)
    repo.propose(claim(engine, run["id"]), plan_for(payload))
    repo.decide("alice", run["id"], decision(repo, run["id"]))
    repo.finish(claim(engine, run["id"]), "a" * 64)
    return run["id"], repo


def report(lease, outcome="FAILED", **overrides):
    return {"spec_hash": lease.spec_hash, "snapshot_hash": "a" * 64,
            "suite_hash": "b" * 64, "outcome": outcome, "checks": [],
            "feedback": [{"acceptance_id": "AC-CREATE", "actual": "Mismatch"}], **overrides}


def test_frozen_suites_match_exact_examples():
    for name in ("notes", "task"):
        spec = CoreSpec.model_validate_json(
            (ROOT / f"examples/{name}_api.spec.json").read_text(encoding="utf-8")
        )
        assert SUITES[spec.fingerprint()] == name
        assert spec.model_copy(update={"version": spec.version + 1}).fingerprint() not in SUITES


@pytest.mark.mysql
def test_schedule_concurrency_repair_limit_and_budget_survive(agent_db, spec_payload):
    run_id, agent = prepared(agent_db, spec_payload)
    repo = VerificationRepository(agent_db)
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(lambda _: repo.schedule(), range(2))) == 1
    lease = claim(agent_db, run_id)
    assert repo.commit(lease, report(lease)) == "QUEUED"
    for index in range(2):
        repairing = claim(agent_db, run_id)
        assert agent.context(repairing)["phase"] == "REPAIR"
        assert agent.context(repairing)["verification_feedback"]
        agent.reserve(repairing, 10, 1)
        agent.finish(repairing, "a" * 64)
        assert repo.schedule() == 1
        verifying = claim(agent_db, run_id)
        outcome = repo.commit(verifying, report(verifying))
        assert outcome == ("QUEUED" if index == 0 else "FAILED")
    rows = repo.reports("alice", run_id)
    assert [r["repair_scheduled"] for r in rows] == [True, True, False]
    assert agent.review("alice", run_id)["budget"]["calls"] == 2


@pytest.mark.mysql
def test_verification_fencing_identity_and_atomic_rollback(agent_db, spec_payload, monkeypatch):
    run_id, _ = prepared(agent_db, spec_payload)
    repo = VerificationRepository(agent_db)
    repo.schedule()
    old = claim(agent_db, run_id)
    expire(agent_db, run_id)
    repo.execution.recover()
    lease = claim(agent_db, run_id)
    with pytest.raises(LeaseLost):
        repo.commit(old, report(old, "PASSED"))
    with pytest.raises(LifecycleError):
        repo.commit(lease, report(lease, snapshot_hash="c" * 64))
    with Session(agent_db) as session:
        before = session.scalar(select(func.count()).select_from(OutboxEvent))

    def broken(*args):
        raise RuntimeError("Injected report commit failure")

    monkeypatch.setattr(repo, "event", broken)
    with pytest.raises(RuntimeError):
        repo.commit(lease, report(lease))
    with Session(agent_db) as session:
        assert session.scalar(select(func.count()).select_from(Verification)) == 0
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == before
        assert session.get(Run, run_id).status == "RUNNING"


@pytest.mark.mysql
def test_suite_change_and_cancel_cannot_commit(agent_db, spec_payload):
    from spec2repo.storage.runs import RunRepository

    run_id, agent = prepared(agent_db, spec_payload)
    repo = VerificationRepository(agent_db)
    repo.schedule()
    lease = claim(agent_db, run_id)
    repo.commit(lease, report(lease))
    agent.finish(claim(agent_db, run_id), "a" * 64)
    repo.schedule()
    lease = claim(agent_db, run_id)
    with pytest.raises(LifecycleError):
        repo.commit(lease, report(lease, "PASSED", suite_hash="c" * 64))
    RunRepository(agent_db).cancel("alice", run_id)
    with pytest.raises(LeaseLost):
        repo.commit(lease, report(lease, "PASSED"))


@pytest.mark.mysql
def test_infrastructure_error_never_repairs_and_empty_success_rejected(agent_db, spec_payload):
    run_id, _ = prepared(agent_db, spec_payload)
    repo = VerificationRepository(agent_db)
    repo.schedule()
    lease = claim(agent_db, run_id)
    with pytest.raises(LifecycleError):
        repo.commit(lease, report(lease, "PASSED"))
    assert repo.commit(lease, report(lease, "ERROR")) == "FAILED"
    assert not repo.reports("alice", run_id)[0]["repair_scheduled"]
    with pytest.raises(LifecycleError):
        repo.artifact("alice", run_id)


@pytest.mark.mysql
@pytest.mark.redis
@pytest.mark.docker
def test_independent_http_repair_loop_and_verified_download(
    agent_db, execution_streams, sandbox, api_settings,
):
    spaces, docker = sandbox
    payload = json.loads((ROOT / "examples/notes_api.spec.json").read_text(encoding="utf-8"))
    run = create(agent_db, payload)
    broken = NOTES.replace("min_length=1", "min_length=0")
    model = ScriptedModel([
        ("propose_plan", {"plan": plan_for(payload).model_dump(mode="json")}),
        ("write_file", {"path": "app.py", "content": broken}),
        # Malicious test/plugin files must never run in the trusted judge.
        ("write_file", {"path": "conftest.py", "content": "raise RuntimeError('tampered')"}),
        ("finish", {"summary": "Submit broken version"}),
        ("write_file", {"path": "app.py", "content": NOTES}),
        ("finish", {"summary": "Repair validation"}),
    ])
    agent, verification = AgentRepository(agent_db), VerificationRepository(agent_db)
    worker = AgentWorker(agent_db, execution_streams, "acceptance-worker", spaces, docker, model)
    relay = Relay(agent_db, execution_streams)

    def tick():
        while relay.publish_one(run_id=run["id"]):
            pass
        worker.tick(idle_ms=1)

    tick()
    agent.decide("alice", run["id"], decision(agent, run["id"]))
    tick()
    verification.schedule()
    tick()
    assert verification.reports("alice", run["id"])[0]["outcome"] == "FAILED"
    tick()  # Repair restores the submitted snapshot in a fresh generation.
    repair_context = json.loads(model.messages[-2][1]["content"])
    assert repair_context["phase"] == "REPAIR"
    assert repair_context["verification_feedback"][0]["acceptance_id"] == "A2"
    assert "边界" not in json.dumps(repair_context, ensure_ascii=False)
    verification.schedule()
    tick()
    rows = verification.reports("alice", run["id"])
    assert [r["outcome"] for r in rows] == ["FAILED", "PASSED"]
    assert len(rows[-1]["checks"]) == 15
    assert agent.review("alice", run["id"])["run"]["status"] == "SUCCEEDED"
    assert len(model.messages) == 6  # Verifying consumes no model call.
    with TestClient(create_app(api_settings, engine=agent_db, workspace_root=spaces.root)) as api:
        base = f"/api/runs/{run['id']}"
        for suffix in ("verifications", "artifacts", "artifacts/repository.zip"):
            assert api.get(base + "/" + suffix, headers={
                "X-API-Key": api_settings.api_keys["bob"]
            }).status_code == 404
        result = api.get(base + "/artifacts/repository.zip", headers={
            "X-API-Key": api_settings.api_keys["alice"]
        })
        assert result.status_code == 200 and result.content.startswith(b"PK")
    for generation in range(1, 6):
        assert docker.inspect(run["id"], generation) is None
        assert docker.inspect(run["id"], generation, verifier=True) is None


@pytest.mark.mysql
@pytest.mark.docker
@pytest.mark.parametrize("source", ["raise RuntimeError('startup')", NOTES.replace(
    "os.environ.get('NOTES_DB', 'notes.db')", "':memory:'"
)])
def test_startup_and_nonpersistent_app_cannot_pass(agent_db, sandbox, source):
    spaces, docker = sandbox
    payload = json.loads((ROOT / "examples/notes_api.spec.json").read_text(encoding="utf-8"))
    run_id, agent = prepared(agent_db, payload)
    repo = VerificationRepository(agent_db)
    repo.schedule()
    lease = claim(agent_db, run_id)
    # Create a separate source attempt, publish, then verify into the leased empty workspace.
    from dataclasses import replace

    source_dir = spaces.create(replace(lease, generation=99))
    (source_dir / "app.py").write_text(source, encoding="utf-8")
    digest = spaces.snapshot(source_dir)
    context = agent.context(lease)
    context["snapshot_hash"] = digest
    result = Verifier(spaces, docker).run(
        lease, context, lambda: agent.execution.assert_owned(lease), threading.Event()
    )
    assert result["outcome"] == "FAILED"


@pytest.mark.mysql
@pytest.mark.docker
def test_task_suite_and_judge_has_no_generated_workspace(agent_db, sandbox, spec_payload):
    from dataclasses import replace

    spaces, docker = sandbox
    run_id, agent = prepared(agent_db, spec_payload)
    repo = VerificationRepository(agent_db)
    repo.schedule()
    lease = claim(agent_db, run_id)
    source = spaces.create(replace(lease, generation=99))
    (source / "app.py").write_text(
        (ROOT / "tests/fixtures/task_app.py").read_text(encoding="utf-8"), encoding="utf-8"
    )
    context = agent.context(lease)
    context["snapshot_hash"] = spaces.snapshot(source)
    verifier = Verifier(spaces, docker)
    args = verifier.args(lease, ["python", "--version"], judge=True)
    assert "--mount" not in args and "--env" not in args
    assert "--pid" not in args and "--privileged" not in args
    assert args[args.index("--network") + 1].startswith("container:")
    result = verifier.run(lease, context, lambda: agent.execution.assert_owned(lease), threading.Event())
    assert result["outcome"] == "PASSED", result
    assert len(result["checks"]) == 24
