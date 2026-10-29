import json
import threading
from uuid import uuid4

import pytest
from test_agent import (
    agent_db as agent_db,  # noqa: PLC0414
)
from test_agent import (
    claim,
    create,
    decision,
    plan_for,
)
from test_execution import sandbox as sandbox  # noqa: PLC0414

from spec2repo.harness.loop import AgentLoop
from spec2repo.harness.model import ModelError
from spec2repo.harness.scheduling import batch, trim_oldest
from spec2repo.harness.tools import TOOLS
from spec2repo.sandbox.docker import CommandFailed
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.workspaces import Workspaces


def tool(name, **args):
    return {"id": uuid4().hex, "type": "function",
            "function": {"name": name, "arguments": json.dumps(args)}}


class BatchModel:
    MAX_OUTPUT, model = 1000, "scripted-parallel"

    def __init__(self, batches):
        self.batches = iter(batches)
        self.messages = []

    def complete(self, messages, *_args, **_kwargs):
        self.messages.append(json.loads(json.dumps(messages)))
        return {"role": "assistant", "content": None, "tool_calls": next(self.batches)}, None


def implementation(engine, payload):
    run = create(engine, payload)
    repo = AgentRepository(engine)
    repo.propose(claim(engine, run["id"]), plan_for(payload))
    repo.decide("alice", run["id"], decision(repo, run["id"]))
    return repo, claim(engine, run["id"])


def test_dependencies_cover_aliases_explicit_edges_and_terminal_barriers(tmp_path):
    calls = [tool("write_file", path="a.py", content="a"),
             tool("write_file", path="b.py", content="b"),
             tool("read_file", path="./a.py"),
             tool("read_file", path="c.py", depends_on=[2]),
             tool("terminal", argv=["pytest"]), tool("finish", summary="done")]
    parsed = batch(calls, TOOLS, tmp_path)
    assert parsed[1].dependencies == set()
    assert parsed[2].dependencies == {0}
    assert parsed[3].dependencies == {1}
    assert parsed[4].dependencies == {0, 1, 2, 3}
    assert parsed[5].dependencies == {0, 1, 2, 3, 4}


@pytest.mark.parametrize("calls", [
    [tool("read_file", path="a", depends_on=[1])],
    [tool("read_file", path="a", depends_on=[2]), tool("read_file", path="b")],
    [tool("finish", summary="done"), tool("read_file", path="a")],
    [tool("request_clarification", questions=["What API?"]), tool("read_file", path="a")],
])
def test_invalid_dependencies_or_control_order_rejected_before_effects(calls, tmp_path):
    with pytest.raises(ModelError):
        batch(calls, TOOLS, tmp_path)


def test_duplicate_ids_rejected_and_whole_batch_trimmed(tmp_path):
    first = tool("read_file", path="a")
    with pytest.raises(ModelError):
        batch([first, first], TOOLS, tmp_path)
    history = [{"role": "system"}, {"role": "user"},
               {"role": "assistant", "tool_calls": [first, tool("read_file", path="b")]},
               {"role": "tool"}, {"role": "tool"},
               {"role": "assistant"}, {"role": "tool"}]
    assert trim_oldest(history)
    assert [m["role"] for m in history] == ["system", "user", "assistant", "tool"]
    assert not trim_oldest(history)


@pytest.mark.mysql
def test_independent_tools_overlap_results_pair_and_failed_dependents_skip(
    agent_db, spec_payload, tmp_path,
):
    repo, lease = implementation(agent_db, spec_payload)
    rendezvous = threading.Barrier(2)
    seen = []

    class Sandbox:
        def run(self, _lease, argv, *, authorized, invocation=None, **_kwargs):
            authorized()
            assert invocation is not None
            args = json.loads(argv[-1])
            seen.append(args["path"])
            rendezvous.wait(timeout=5)  # Would fail if execution were secretly serial.
            if args["path"] == "a":
                raise CommandFailed(1, "expected fixture failure")
            return "written"

    calls = [tool("write_file", path="a", content="a"),
             tool("write_file", path="b", content="b"),
             tool("read_file", path="a"), tool("finish", summary="must be skipped")]
    model = BatchModel([calls, [tool("request_clarification", questions=["Resolve failure?"])]])
    assert AgentLoop(repo, model, Workspaces(tmp_path), Sandbox()).run(
        lease, threading.Event()) == "clarification"
    assert set(seen) == {"a", "b"}
    replies = model.messages[1][-4:]
    assert [r["tool_call_id"] for r in replies] == [c["id"] for c in calls]
    assert [json.loads(r["content"])["ok"] for r in replies] == [False, True, False, False]


@pytest.mark.mysql
@pytest.mark.docker
def test_real_parallel_containers_then_terminal_then_snapshot(agent_db, spec_payload, sandbox):
    repo, lease = implementation(agent_db, spec_payload)
    spaces, docker = sandbox
    model = BatchModel([[
        tool("write_file", path="a.txt", content="alpha"),
        tool("write_file", path="b.txt", content="beta"),
        tool("terminal", argv=["python", "-c",
             ("from pathlib import Path; assert Path('a.txt').read_text()=='alpha'; "
              "assert Path('b.txt').read_text()=='beta'")]),
        tool("finish", summary="Files written and checked"),
    ]])
    assert AgentLoop(repo, model, spaces, docker).run(lease, threading.Event()) == "verification"
    review = repo.review("alice", lease.run_id)
    assert review["budget"]["calls"] == 1
    assert review["budget"]["reserved_seconds"] == 210  # model + 3 tool waves
    assert spaces.verify(review["snapshot_hash"]).is_file()
    assert not docker.command(["ps", "-aq", "--filter", f"label=spec2repo.run={lease.run_id}"]).strip()
