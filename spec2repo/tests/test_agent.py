import json
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from test_execution import execution_streams as execution_streams  # noqa: PLC0414
from test_execution import sandbox as sandbox  # noqa: PLC0414

from spec2repo.api.app import create_app
from spec2repo.domain.agent import ApprovalDecision, ClarificationDecision, ImplementationPlan
from spec2repo.domain.lifecycle import LifecycleError
from spec2repo.domain.spec import CoreSpec
from spec2repo.harness.loop import AgentLoop
from spec2repo.harness.model import ModelError
from spec2repo.runtime.agent_worker import AgentWorker
from spec2repo.runtime.delivery import Relay
from spec2repo.storage.agent import AgentRepository, BudgetExceeded
from spec2repo.storage.execution import ExecutionRepository, LeaseLost
from spec2repo.storage.models import (
    AgentState,
    Attempt,
    Base,
    OutboxEvent,
    PlanVersion,
    Run,
    RunEvent,
    SpecRevision,
)
from spec2repo.storage.runs import RunRepository
from spec2repo.storage.workspaces import Workspaces


@pytest.fixture
def agent_db(mysql_engine):
    with mysql_engine.begin() as connection:
        for table in reversed(Base.metadata.sorted_tables):
            connection.execute(table.delete())
    return mysql_engine


def plan_for(payload):
    return ImplementationPlan.model_validate(
        {
            "summary": "Implement and test the API",
            "tasks": [
                {
                    "id": "T1",
                    "description": "Implement all specified behavior",
                    "requirement_ids": [r["id"] for r in payload["requirements"]],
                    "acceptance_ids": [a["id"] for a in payload["acceptance_criteria"]],
                    "files": ["app.py", "test_app.py"],
                }
            ],
        }
    )


def create(engine, payload):
    return RunRepository(engine).create("alice", uuid4().hex, CoreSpec.model_validate(payload))[0]


def claim(engine, run_id):
    with Session(engine) as session:
        run = session.get(Run, run_id)
        event = session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.run_id == run_id)
            .order_by(OutboxEvent.created_at.desc())
        )
        args = (event.id, run_id, run.version, run.generation, uuid4().hex)
    outcome, lease = ExecutionRepository(engine).claim(*args)
    assert outcome == "claimed"
    return lease


def decision(repo, run_id, approve=True):
    review = repo.review("alice", run_id)
    plan = review["plans"][-1]
    return ApprovalDecision(
        revision=plan["revision"],
        spec_hash=plan["spec_hash"],
        plan_hash=plan["plan_hash"],
        approve=approve,
    )


@pytest.mark.mysql
def test_approval_is_durable_bound_to_plan_and_concurrent_idempotent(agent_db, spec_payload):
    run = create(agent_db, spec_payload)
    lease = claim(agent_db, run["id"])
    repo = AgentRepository(agent_db)
    repo.propose(lease, plan_for(spec_payload))
    with pytest.raises(LeaseLost):
        repo.execution.heartbeat(lease)
    approve = decision(repo, run["id"])
    wrong = approve.model_copy(update={"plan_hash": "f" * 64})
    with pytest.raises(LifecycleError):
        repo.decide("alice", run["id"], wrong)
    # New repository objects simulate losing all in-process approval state.
    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(
            pool.map(
                lambda _: AgentRepository(agent_db).decide("alice", run["id"], approve),
                range(2),
            )
        )
    assert all(r["status"] == "QUEUED" for r in result)
    with Session(agent_db) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 2
        assert session.get(PlanVersion, (run["id"], 1)).decision == "APPROVED"
    with pytest.raises(LifecycleError):
        repo.decide("alice", run["id"], approve.model_copy(update={"approve": False}))


@pytest.mark.mysql
def test_approval_rejection_cancels_without_requeue(agent_db, spec_payload):
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    repo.propose(claim(agent_db, run["id"]), plan_for(spec_payload))
    reject = decision(repo, run["id"], False)
    assert repo.decide("alice", run["id"], reject)["status"] == "CANCELLED"
    assert repo.decide("alice", run["id"], reject)["status"] == "CANCELLED"
    with Session(agent_db) as session:
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.mysql
def test_clarification_versions_spec_invalidates_old_approval_preserves_budget(
    agent_db,
    spec_payload,
):
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    lease = claim(agent_db, run["id"])
    repo.reserve(lease, 100, 1)
    repo.propose(lease, plan_for(spec_payload))
    approve = decision(repo, run["id"])
    repo.decide("alice", run["id"], approve)
    implementing = claim(agent_db, run["id"])
    repo.ask(implementing, ["Which conflict behavior should the API use?"])
    waiting = RunRepository(agent_db).get("alice", run["id"])
    spec_payload["version"] += 1
    spec_payload["constraints"] = [
        *spec_payload.get("constraints", []),
        "Reject conflicting writes",
    ]
    answer = ClarificationDecision(
        expected_version=waiting["version"],
        spec=CoreSpec.model_validate(spec_payload),
        reason="Owner supplied the conflict rule",
    )
    repo.clarify("alice", run["id"], answer)
    with pytest.raises(LifecycleError):
        repo.clarify("alice", run["id"], answer)
    with pytest.raises(LifecycleError):
        repo.decide("alice", run["id"], approve)
    with Session(agent_db) as session:
        state = session.get(AgentState, run["id"])
        assert state.phase == "PLAN" and state.calls == 1
        assert session.scalar(select(func.count()).select_from(SpecRevision)) == 2


@pytest.mark.mysql
def test_budget_reservation_survives_recovery_and_normal_handoffs_are_not_losses(
    agent_db,
    spec_payload,
):
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    lease = claim(agent_db, run["id"])
    repo.reserve(lease, 200, 150)
    repo.propose(lease, plan_for(spec_payload))
    repo.decide("alice", run["id"], decision(repo, run["id"]))
    second = claim(agent_db, run["id"])
    with agent_db.begin() as connection:
        connection.execute(
            text("UPDATE runs SET lease_until=UTC_TIMESTAMP(6)-INTERVAL 1 SECOND WHERE id=:id"),
            {"id": run["id"]},
        )
    assert repo.execution.recover() == 1
    third = claim(agent_db, run["id"])
    assert third.generation == 3
    assert AgentRepository(agent_db).context(third)["calls"] == 1
    with pytest.raises(LeaseLost):
        repo.reserve(second, 100, 1)
    with pytest.raises(BudgetExceeded):
        repo.reserve(third, repo.MAX_RESERVED_TOKENS, 1)
    with Session(agent_db) as session:
        assert session.get(Attempt, (run["id"], 1)).status == "WAITING_APPROVAL"


def test_review_clarification_auth_and_initial_question(db_engine, api_settings, spec_payload):
    spec_payload["open_questions"] = [
        {"id": "Q1", "question": "Choose semantics", "blocking": True}
    ]
    with TestClient(create_app(api_settings, engine=db_engine)) as client:
        headers = {"X-API-Key": api_settings.api_keys["alice"], "Idempotency-Key": uuid4().hex}
        run = client.post("/api/runs", json=spec_payload, headers=headers).json()
        base = f"/api/runs/{run['id']}"
        assert client.get(base + "/review", headers=headers).json()["questions"]
        spec_payload["version"] += 1
        spec_payload["open_questions"][0]["answer"] = "Chosen behavior"
        body = {"expected_version": 0, "spec": spec_payload, "reason": "Answered"}
        bob = {"X-API-Key": api_settings.api_keys["bob"]}
        assert client.get(base + "/review", headers=bob).status_code == 404
        assert client.post(base + "/clarification", headers=bob, json=body).status_code == 404
        approve = {"revision": 1, "spec_hash": "a" * 64, "plan_hash": "b" * 64, "approve": True}
        assert client.post(base + "/approval", headers=bob, json=approve).status_code == 404
        result = client.post(base + "/clarification", headers=headers, json=body)
        assert result.status_code == 200 and result.json()["status"] == "QUEUED"
        assert client.post(base + "/clarification", headers=headers, json=body).status_code == 409
        assert client.post(base + "/approval", headers=headers, json=approve).status_code == 409


class ScriptedModel:
    MAX_OUTPUT = 1000
    model = "scripted-test"

    def __init__(self, actions):
        self.actions = iter(actions)
        self.messages = []

    def complete(self, messages, tools, *, timeout, session_id=None):
        self.messages.append(json.loads(json.dumps(messages)))
        name, args = next(self.actions)
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": uuid4().hex,
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(args)},
                }
            ],
        }, None


@pytest.mark.mysql
def test_context_trims_complete_pairs_but_keeps_progress_and_budget(
    agent_db, spec_payload, tmp_path
):
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    repo.propose(claim(agent_db, run["id"]), plan_for(spec_payload))
    repo.decide("alice", run["id"], decision(repo, run["id"]))

    class OutputOnlySandbox:
        def run(self, *_args, authorized, **_kwargs):
            authorized()
            return "synthetic tool output"

    actions = [("write_file", {"path": f"module{i}.py", "content": "x" * 7000}) for i in range(5)]
    actions.append(("finish", {"summary": "Context policy experiment"}))
    model = ScriptedModel(actions)
    loop = AgentLoop(repo, model, Workspaces(tmp_path), OutputOnlySandbox())
    assert loop.run(claim(agent_db, run["id"]), threading.Event()) == "verification"
    final = model.messages[-1]
    pinned = json.loads(final[1]["content"])
    assert pinned["remaining_calls"] < 12
    assert pinned["completed_tools"][0]["path"] == "module0.py"
    assert len(final) < 12  # At least one old call/result pair was removed.
    for position in range(2, len(final), 2):
        assert final[position]["role"] == "assistant"
        assert final[position + 1]["role"] == "tool"
        assert final[position]["tool_calls"][0]["id"] == final[position + 1]["tool_call_id"]


@pytest.mark.mysql
def test_plan_validation_feedback_and_no_unapproved_workspace(agent_db, spec_payload, tmp_path):
    run = create(agent_db, spec_payload)
    valid = plan_for(spec_payload).model_dump(mode="json")
    invalid = json.loads(json.dumps(valid))
    invalid["tasks"][0]["requirement_ids"] = ["not_a_requirement"]
    model = ScriptedModel([("propose_plan", {"plan": invalid}), ("propose_plan", {"plan": valid})])
    spaces = Workspaces(tmp_path)
    repo = AgentRepository(agent_db)
    loop = AgentLoop(repo, model, spaces, None)
    assert loop.run(claim(agent_db, run["id"]), threading.Event()) == "approval"
    assert model.messages[1][-1]["role"] == "tool"
    assert "false" in model.messages[1][-1]["content"]
    assert list(spaces.attempts.iterdir()) == []


@pytest.mark.mysql
def test_unavailable_tool_cannot_execute_before_approval(agent_db, spec_payload, tmp_path):
    run = create(agent_db, spec_payload)
    model = ScriptedModel([("terminal", {"argv": ["sh", "-c", "touch hacked"]})])
    loop = AgentLoop(AgentRepository(agent_db), model, Workspaces(tmp_path), None)
    with pytest.raises(ModelError):
        loop.run(claim(agent_db, run["id"]), threading.Event())


@pytest.mark.mysql
def test_budget_stops_before_any_model_request(agent_db, spec_payload, tmp_path, monkeypatch):
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    monkeypatch.setattr(repo, "MAX_CALLS", 0)
    model = ScriptedModel([])
    loop = AgentLoop(repo, model, Workspaces(tmp_path), None)
    with pytest.raises(BudgetExceeded):
        loop.run(claim(agent_db, run["id"]), threading.Event())
    assert model.messages == []


@pytest.mark.mysql
def test_cancel_during_model_request_discards_late_plan(agent_db, spec_payload, tmp_path):
    run = create(agent_db, spec_payload)
    scripted = ScriptedModel(
        [("propose_plan", {"plan": plan_for(spec_payload).model_dump(mode="json")})]
    )

    class CancellingModel:
        MAX_OUTPUT, model = 1000, "cancel-test"

        def complete(self, *args, **kwargs):
            RunRepository(agent_db).cancel("alice", run["id"])
            return scripted.complete(*args, **kwargs)

    repo = AgentRepository(agent_db)
    loop = AgentLoop(repo, CancellingModel(), Workspaces(tmp_path), None)
    with pytest.raises(LeaseLost):
        loop.run(claim(agent_db, run["id"]), threading.Event())
    with Session(agent_db) as session:
        assert session.scalar(select(func.count()).select_from(PlanVersion)) == 0
        assert session.get(AgentState, run["id"]).calls == 1


@pytest.mark.mysql
def test_internal_plan_revision_preserves_business_contract(agent_db, spec_payload):
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    repo.propose(claim(agent_db, run["id"]), plan_for(spec_payload))
    repo.decide("alice", run["id"], decision(repo, run["id"]))
    lease = claim(agent_db, run["id"])
    revised = plan_for(spec_payload).model_dump(mode="json")
    revised["tasks"][0]["files"] = ["app/routes.py", "app/database.py", "tests/test_app.py"]
    repo.propose(
        lease,
        ImplementationPlan.model_validate(revised),
        reason="Separate API routes from persistence",
        internal=True,
    )
    review = repo.review("alice", run["id"])
    assert review["run"]["status"] == "RUNNING"
    assert review["plans"][-1]["decision"] == "INTERNAL"
    assert review["plans"][-1]["spec_hash"] == run["spec_hash"]
    revised["tasks"][0]["requirement_ids"] = ["invented"]
    with pytest.raises(ValueError):
        repo.propose(
            lease, ImplementationPlan.model_validate(revised), reason="Bad reference", internal=True
        )


@pytest.mark.mysql
def test_approval_state_event_and_outbox_rollback_together(agent_db, spec_payload, monkeypatch):
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    repo.propose(claim(agent_db, run["id"]), plan_for(spec_payload))
    approve = decision(repo, run["id"])

    def broken_event(*_args):
        raise RuntimeError("Injected event failure")

    monkeypatch.setattr(repo, "event", broken_event)
    with pytest.raises(RuntimeError):
        repo.decide("alice", run["id"], approve)
    with Session(agent_db) as session:
        assert session.get(Run, run["id"]).status == "WAITING_APPROVAL"
        assert session.get(PlanVersion, (run["id"], 1)).decision == "PENDING"
        assert session.get(AgentState, run["id"]).phase == "PLAN"
        assert session.scalar(select(func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.mysql
def test_cancelled_run_cannot_be_approved(agent_db, spec_payload):
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    repo.propose(claim(agent_db, run["id"]), plan_for(spec_payload))
    approve = decision(repo, run["id"])
    RunRepository(agent_db).cancel("alice", run["id"])
    with pytest.raises(LifecycleError):
        repo.decide("alice", run["id"], approve)


@pytest.mark.mysql
@pytest.mark.redis
@pytest.mark.docker
def test_agent_tool_feedback_repair_and_submission_is_not_success(
    agent_db,
    execution_streams,
    sandbox,
    spec_payload,
):
    spaces, docker = sandbox
    run = create(agent_db, spec_payload)
    repo = AgentRepository(agent_db)
    model = ScriptedModel(
        [
            ("propose_plan", {"plan": plan_for(spec_payload).model_dump(mode="json")}),
            ("write_file", {"path": "answer.py", "content": "answer = 1\n"}),
            ("terminal", {"argv": ["python", "-c", "import answer; assert answer.answer == 2"]}),
            ("write_file", {"path": "answer.py", "content": "answer = 2\n"}),
            ("terminal", {"argv": ["python", "-c", "import answer; assert answer.answer == 2"]}),
            ("finish", {"summary": "Implemented and checked"}),
        ]
    )
    relay = Relay(agent_db, execution_streams)
    worker = AgentWorker(agent_db, execution_streams, "agent-test", spaces, docker, model)
    relay.publish_one()
    assert worker.tick() == ["waiting"]
    assert repo.review("alice", run["id"])["run"]["status"] == "WAITING_APPROVAL"
    repo.decide("alice", run["id"], decision(repo, run["id"]))
    relay.publish_one()
    assert worker.tick() == ["waiting"]
    review = repo.review("alice", run["id"])
    assert review["run"]["status"] == "WAITING_VERIFICATION"
    assert review["run"]["generation"] == 2
    spaces.verify(review["snapshot_hash"])
    assert any(
        message["role"] == "tool" and "AssertionError" in message["content"]
        for call in model.messages
        for message in call
    )
    with Session(agent_db) as session:
        rows = session.scalars(
            select(RunEvent).where(
                RunEvent.run_id == run["id"],
                RunEvent.event_type == "agent.tool_returned",
            )
        ).all()
        assert [row.payload["ok"] for row in rows] == [True, False, True, True]
    assert docker.inspect(run["id"], 2) is None
