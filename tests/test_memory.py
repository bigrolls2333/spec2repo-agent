import json
import threading
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agent import ScriptedModel, claim
from test_agent import agent_db as agent_db  # noqa: PLC0414
from test_parallel_tools import implementation

from spec2repo.harness.loop import AgentLoop
from spec2repo.harness.tools import UpdateMemory
from spec2repo.storage import memory
from spec2repo.storage.execution import LeaseLost
from spec2repo.storage.models import AgentState, Run, RunEvent, TaskMemory
from spec2repo.storage.runs import utc_now
from spec2repo.storage.workspaces import Workspaces


def notes(**kwargs):
    return UpdateMemory(**kwargs).model_dump(mode="json", exclude={"depends_on"})


@pytest.mark.mysql
def test_evidence_pages_redaction_isolation_and_quota(agent_db, spec_payload, monkeypatch):
    repo, lease = implementation(agent_db, spec_payload)
    result = repo.capture_output(lease, "terminal", None, {
        "ok": False, "exit_code": 1,
        "output": "password=hidden known-secret\n" + "x" * 10000 + "FAILED tail",
    }, "known-secret")
    assert "hidden" not in result["output"] and "known-secret" not in result["output"]
    assert "FAILED tail" in result["output"] and result["truncated"]
    identifier = result["evidence_id"]
    page = repo.read_output(lease, identifier, 8000, 4000)
    assert page["output"].endswith("FAILED tail") and page["original_ok"] is False
    assert page["next_offset"] is None and page["capture_limited"]
    _, other = implementation(agent_db, spec_payload)
    with pytest.raises(ValueError):
        repo.read_output(other, identifier, 0, 100)
    monkeypatch.setattr(memory, "MAX_ENTRIES", 1)
    assert "evidence_unavailable" in repo.capture_output(
        lease, "read_file", "a", {"ok": True, "output": "more"}
    )


@pytest.mark.mysql
def test_recovery_discards_unsubmitted_memory_and_fences_writer(agent_db, spec_payload):
    repo, lease = implementation(agent_db, spec_payload)
    result = repo.capture_output(lease, "write_file", "a", {"ok": True, "output": "written"})
    repo.update_memory(lease, notes(progress=["a completed"], evidence_ids=[result["evidence_id"]]))
    with Session(agent_db) as session, session.begin():
        session.get(Run, lease.run_id).lease_until = utc_now() - timedelta(seconds=5)
    assert repo.execution.recover() == 1
    new = claim(agent_db, lease.run_id)
    assert repo.context(new)["task_memory"]["model_notes"] == {}
    with pytest.raises(LeaseLost):
        repo.update_memory(lease, notes(progress=["late writer"]))
    with pytest.raises(ValueError):
        repo.read_output(new, result["evidence_id"], 0, 100)


@pytest.mark.mysql
def test_snapshot_bound_memory_survives_new_generation(agent_db, spec_payload):
    repo, lease = implementation(agent_db, spec_payload)
    result = repo.capture_output(lease, "terminal", None, {"ok": False, "output": "bad assertion"})
    repo.update_memory(lease, notes(failed_attempts=["fix assertion"], evidence_ids=[result["evidence_id"]]))
    repo.finish(lease, "a" * 64)
    with Session(agent_db) as session, session.begin():
        row = session.get(TaskMemory, (lease.run_id, lease.generation))
        assert row.snapshot_hash == "a" * 64
        run = session.get(Run, lease.run_id)
        session.get(AgentState, run.id).phase = "REPAIR"
        repo.enqueue(session, run)
    new = claim(agent_db, lease.run_id)
    assert repo.context(new)["task_memory"]["model_notes"]["failed_attempts"] == ["fix assertion"]
    assert repo.read_output(new, result["evidence_id"], 0, 100)["output"] == "bad assertion"


@pytest.mark.mysql
def test_compaction_preserves_notes_and_failure_evidence(agent_db, spec_payload, tmp_path):
    repo, lease = implementation(agent_db, spec_payload)
    result = repo.capture_output(lease, "terminal", None, {"ok": False, "output": "important failure"})
    repo.update_memory(lease, notes(decisions=["use sqlite"], evidence_ids=[result["evidence_id"]]))

    class Sandbox:
        def run(self, *args, authorized, **kwargs):
            authorized()
            return "written"

    model = ScriptedModel([
        *[("write_file", {"path": f"a{i}.py", "content": "x" * 7000}) for i in range(5)],
        ("read_tool_output", {"evidence_id": result["evidence_id"]}),
        ("finish", {"summary": "ready"}),
    ])
    assert AgentLoop(repo, model, Workspaces(tmp_path), Sandbox()).run(
        lease, threading.Event()
    ) == "verification"
    pinned = json.loads(model.messages[-1][1]["content"])
    assert pinned["task_memory"]["model_notes"]["decisions"] == ["use sqlite"]
    assert pinned["task_memory"]["failures"][0]["evidence_id"] == result["evidence_id"]
    assert "important failure" in model.messages[-1][-1]["content"]
    with Session(agent_db) as session:
        assert session.scalar(select(RunEvent).where(
            RunEvent.run_id == lease.run_id, RunEvent.event_type == "agent.context_compacted"
        )) is not None
