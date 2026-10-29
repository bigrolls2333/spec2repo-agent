import json
import threading

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_agent import ScriptedModel, claim, create, decision, plan_for
from test_agent import agent_db as agent_db  # noqa: PLC0414
from test_execution import execution_streams as execution_streams  # noqa: PLC0414
from test_execution import sandbox as sandbox  # noqa: PLC0414
from test_parallel_tools import implementation
from test_verification import prepared, report

from spec2repo.harness.evaluation import summarize
from spec2repo.harness.loop import AgentLoop
from spec2repo.harness.progress import AgentStalled, ProgressMonitor, workspace_digest
from spec2repo.harness.tools import ReadFile, Terminal
from spec2repo.runtime.agent_worker import AgentWorker
from spec2repo.runtime.delivery import Relay
from spec2repo.sandbox.docker import CommandFailed
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.models import RunEvent
from spec2repo.storage.verification import VerificationRepository
from spec2repo.storage.workspaces import Workspaces


def test_detection_requires_distinct_turns_identical_evidence_and_unchanged_files():
    monitor = ProgressMonitor()
    args = Terminal(argv=["pytest"])
    result = {"ok": False, "output": "failed", "exit_code": 1, "evidence_id": "e"}
    for _ in range(8):
        assert monitor.observe("terminal", args, result, "a", "a", 1) is None
    assert monitor.observe("terminal", args, result, "a", "a", 2) is None
    assert monitor.observe("terminal", args, result, "a", "a", 3)["action"] == "reconsider"
    assert not monitor.stalled
    monitor.observe("terminal", args, result, "a", "b", 4)
    assert monitor.feedback is None
    for call in range(5, 9):
        monitor.observe("terminal", args, result, "b", "b", call)
    assert not monitor.stalled
    assert monitor.observe("terminal", args, result, "b", "b", 9)["action"] == "stop"


def test_reads_only_warn_unknown_fingerprint_disables_and_success_resets():
    monitor = ProgressMonitor()
    for call in range(1, 7):
        monitor.observe("read_file", ReadFile(path="a"), {"ok": True, "output": "x"}, "a", "a", call)
    assert not monitor.stalled and monitor.feedback["kind"] == "repeated_read"
    monitor.observe("terminal", Terminal(argv=["true"]), {"ok": True, "output": ""}, "a", "a", 7)
    assert monitor.feedback is None
    for call in range(8, 16):
        monitor.observe("terminal", Terminal(argv=["false"]), {"ok": False, "output": "x"}, None, None, call)
    assert not monitor.stalled


def test_fingerprint_uses_content_not_mtime_and_excludes_test_cache(tmp_path):
    source = tmp_path / "a.py"
    source.write_text("one")
    initial = workspace_digest(tmp_path)
    source.touch()
    assert workspace_digest(tmp_path) == initial
    cache = tmp_path / ".pytest_cache"
    cache.mkdir()
    (cache / "cache").write_text("changed")
    assert workspace_digest(tmp_path) == initial
    source.write_text("two")
    assert workspace_digest(tmp_path) != initial


@pytest.mark.mysql
@pytest.mark.parametrize("profile", ["baseline", "memory", "guarded"])
def test_loop_intervenes_only_in_guarded_profile_and_preserves_trace(
    agent_db, spec_payload, tmp_path, profile
):
    repo, lease = implementation(agent_db, spec_payload)

    class Sandbox:
        def run(self, *args, authorized, **kwargs):
            authorized()
            raise CommandFailed(1, "same failure")

    model = ScriptedModel([
        *[("terminal", {"argv": ["pytest"]}) for _ in range(5)],
        ("request_clarification", {"questions": ["Need input"]}),
    ])
    loop = AgentLoop(repo, model, Workspaces(tmp_path), Sandbox(), profile=profile)
    if profile == "guarded":
        with pytest.raises(AgentStalled):
            loop.run(lease, threading.Event())
        assert len(model.messages) == 5
        feedback = json.loads(model.messages[3][1]["content"])["progress_feedback"]
        assert feedback["repeated_turns"] == 3 and feedback["evidence_ids"]
    else:
        assert loop.run(lease, threading.Event()) == "clarification"
        assert all("progress_feedback" not in json.loads(m[1]["content"]) for m in model.messages)
    assert ("task_memory" in json.loads(model.messages[0][1]["content"])) == (profile != "baseline")
    with Session(agent_db) as session:
        events = session.scalars(select(RunEvent).where(RunEvent.run_id == lease.run_id)).all()
        replies = [e.payload for e in events if e.event_type == "agent.tool_returned"]
        assert len(replies) == 5
        assert len({r["tool_call_id"] for r in replies}) == 5
        assert all(r["evidence_id"] and r["exit_code"] == 1 for r in replies)
        report = summarize([{"accepted": False, "status": "FAILED", "events": [
            {"type": e.event_type, "payload": e.payload} for e in events
        ]}])
        assert report["repeated_failure_operations"] == 4
        assert report["interventions"] == (2 if profile == "guarded" else 0)


def test_metrics_count_failed_task_cost_and_keep_unknown_usage_unknown():
    events = [{"type": "agent.call_reserved", "payload": {}},
              {"type": "agent.model_returned", "payload": {"usage": {"total_tokens": 100}}}]
    runs = [{"accepted": True, "elapsed_seconds": 10, "events": events,
             "verifications": [{"outcome": "FAILED"}, {"outcome": "PASSED"}]},
            {"accepted": False, "elapsed_seconds": 20, "events": events}]
    result = summarize(runs)
    assert result["tokens_per_success_including_failures"] == 200
    assert result["elapsed_seconds_per_success_including_failures"] == 30
    assert result["repair_success_rate"] == 1
    runs.append({"accepted": False, "events": [{"type": "agent.call_reserved", "payload": {}}]})
    result = summarize(runs)
    assert result["tokens_per_success_including_failures"] is None
    assert result["usage_complete"] is False


@pytest.mark.mysql
def test_repair_feedback_intersects_actual_failed_acceptance_items(agent_db, spec_payload):
    run_id, agent = prepared(agent_db, spec_payload)
    verification = VerificationRepository(agent_db)
    verification.schedule()
    lease = claim(agent_db, run_id)
    verification.commit(lease, report(lease, checks=[
        {"acceptance_id": "A1", "passed": False}, {"acceptance_id": "A2", "passed": False},
    ]))
    repairing = claim(agent_db, run_id)
    assert agent.context(repairing)["repeated_acceptance_failures"] == []
    agent.finish(repairing, "a" * 64)
    verification.schedule()
    lease = claim(agent_db, run_id)
    verification.commit(lease, report(lease, checks=[
        {"acceptance_id": "A1", "passed": True}, {"acceptance_id": "A2", "passed": False},
    ]))
    repairing = claim(agent_db, run_id)
    assert agent.context(repairing)["repeated_acceptance_failures"] == ["A2"]


@pytest.mark.mysql
@pytest.mark.docker
def test_stalled_worker_persists_failure_and_acks(agent_db, spec_payload, execution_streams, sandbox):
    spaces, docker = sandbox
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    repo.propose(claim(agent_db, run["id"]), plan_for(spec_payload))
    repo.decide("alice", run["id"], decision(repo, run["id"]))
    model = ScriptedModel([("terminal", {"argv": ["python", "-c", "raise SystemExit(1)"]})] * 5)
    worker = AgentWorker(agent_db, execution_streams, "stall-test", spaces, docker, model)
    relay = Relay(agent_db, execution_streams)
    while relay.publish_one(run_id=run["id"]):
        pass
    for _ in range(5):
        if worker.tick() == "failed":
            break
    assert repo.review("alice", run["id"])["run"]["status"] == "FAILED"
    assert len(model.messages) == 5
    assert execution_streams.client.xpending(
        execution_streams.stream, execution_streams.group
    )["pending"] == 0
    with Session(agent_db) as session:
        events = session.scalars(select(RunEvent).where(RunEvent.run_id == run["id"])).all()
        assert any("AgentStalled" in json.dumps(e.payload) for e in events)
