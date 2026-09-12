import json
from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage

from paper2repo_agent.artifacts import prepare_run_dirs
from paper2repo_agent.graphs.execute import agent as agent_mod
from paper2repo_agent.graphs.execute.agent import (
    _detect_smoke_failure_low_conf,
    _hitl_gate,
    _latest_verdict,
    _llm_attribute_plan_invalid,
    _stall_warning_message,
    _terminate_decision,
    run_file_generation_agent,
)
from paper2repo_agent.graphs.execute.tools import ToolExecutor
from paper2repo_agent.llm import extract_json, extract_json_value, generate_planning_trace
from paper2repo_agent.schemas import (
    AgentAction,
    AgentTrace,
    ContractSymbol,
    FileContract,
    FileGenerationTask,
    HITLGateConfig,
    PaperSpec,
    SpecValidationResult,
    UserConstraints,
)


def _tool_state(tmp_path, package_name: str = "agent_pkg"):
    paths = prepare_run_dirs(tmp_path / "run")
    package_dir = paths.generated_repo / "src" / package_name
    package_dir.mkdir(parents=True, exist_ok=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    return {
        "paths": paths,
        "manifest": SimpleNamespace(run_id="run_1"),
        "execution_phase": "generation",
        "execution_policy": SimpleNamespace(package_name=package_name),
        "tool_calls": [],
        "file_tasks": [],
        "generated_file_contents": {},
    }


def test_agent_binds_structured_tools_before_invoke(tmp_path, monkeypatch):
    state = _tool_state(tmp_path)
    task = FileGenerationTask(
        id="task_model",
        path="src/agent_pkg/model.py",
        purpose="model",
        generation_mode="hybrid",
        plan_fields=["model"],
        layer=1,
    )
    state["file_tasks"] = [task]

    class BoundLLM:
        def __init__(self) -> None:
            self.invoke_kwargs = None

        def invoke(self, messages, **kwargs):
            self.invoke_kwargs = kwargs
            return AIMessage(content="done")

    class FakeLLM:
        def __init__(self) -> None:
            self.bound_tools = None
            self.bound = BoundLLM()

        def bind_tools(self, tools):
            self.bound_tools = tools
            return self.bound

    fake_llm = FakeLLM()
    monkeypatch.setattr(agent_mod.Settings, "load", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(agent_mod, "build_model", lambda _settings: fake_llm)

    trace = run_file_generation_agent(state, task)

    assert fake_llm.bound_tools
    assert fake_llm.bound.invoke_kwargs == {}
    assert trace.final_status == "stall"


def test_extract_json_value_accepts_top_level_arrays():
    payload = extract_json_value('```json\n[{"step_id": "step_1", "sequence": 1}]\n```')

    assert payload == [{"step_id": "step_1", "sequence": 1}]
    with pytest.raises(ValueError):
        extract_json('[{"step_id": "step_1"}]')


def test_generate_planning_trace_uses_run_and_plan_ids(monkeypatch):
    steps = [
        {
            "step_id": "step_1",
            "sequence": 1,
            "reasoning_type": "understand_architecture",
            "reasoning": "The model consumes smoke tensors.",
            "hypothesis": "Forward accepts x with shape (B, D).",
            "falsifiable_via": "run_smoke_forward reports a shape mismatch.",
            "confidence": 0.8,
            "alternatives_considered": [],
        }
    ]

    class FakeModel:
        def invoke(self, _messages):
            return SimpleNamespace(content=json.dumps(steps))

    monkeypatch.setattr("paper2repo_agent.llm.build_model", lambda _settings: FakeModel())

    trace = generate_planning_trace(
        PaperSpec(id="spec_1"),
        SpecValidationResult(id="validation_1"),
        UserConstraints(),
        SimpleNamespace(model_name="fake"),
        run_id="run_1",
        plan_id="plan_1",
    )

    assert trace is not None
    assert trace.run_id == "run_1"
    assert trace.plan_id == "plan_1"
    assert trace.steps[0].step_id == "step_1"


def test_agent_write_repo_file_runs_generated_guardrails(tmp_path):
    state = _tool_state(tmp_path)
    task = FileGenerationTask(
        id="task_model",
        path="src/agent_pkg/model.py",
        purpose="model",
        generation_mode="hybrid",
        file_contract=FileContract(
            path="src/agent_pkg/model.py",
            required_symbols=[ContractSymbol(name="PaperModel", kind="class")],
        ),
    )
    state["current_agent_task"] = task
    state["file_tasks"] = [task]
    executor = ToolExecutor(state)

    with pytest.raises(RuntimeError, match="structured contract"):
        executor.call(
            "write_repo_file",
            rel_path="src/agent_pkg/model.py",
            content="class OtherModel:\n    pass\n",
        )

    assert not (state["paths"].generated_repo / "src/agent_pkg/model.py").exists()
    assert state["tool_calls"][-1].status == "failed"


def test_agent_write_repo_file_rejects_wrong_target(tmp_path):
    state = _tool_state(tmp_path)
    state["current_agent_task"] = FileGenerationTask(
        id="task_model",
        path="src/agent_pkg/model.py",
        purpose="model",
    )
    executor = ToolExecutor(state)

    with pytest.raises(RuntimeError, match="target mismatch"):
        executor.call(
            "write_repo_file",
            rel_path="src/agent_pkg/losses.py",
            content="def compute_loss():\n    return None\n",
        )


def test_generated_file_read_and_import_tools_reject_escape_inputs(tmp_path):
    state = _tool_state(tmp_path)
    executor = ToolExecutor(state)

    with pytest.raises(RuntimeError, match="escapes generated_repo"):
        executor._tool_read_generated_file(path="../outside.py")
    with pytest.raises(RuntimeError, match="generated package"):
        executor._tool_get_import_error_detail(module_name="os")
    with pytest.raises(RuntimeError, match="Invalid module name"):
        executor._tool_get_import_error_detail(module_name="agent_pkg.model;import os")


def test_run_smoke_forward_executes_forward_and_catches_shape_bug(tmp_path):
    pytest.importorskip("torch")
    state = _tool_state(tmp_path)
    package_dir = state["paths"].generated_repo / "src/agent_pkg"
    (package_dir / "config.py").write_text(
        "from types import SimpleNamespace\n"
        "DEFAULT_CONFIG = SimpleNamespace(input_shape=(4,), num_classes=3, batch_size=2, hidden_dim=4, hash_dim=4, text_input_dim=4)\n",
        encoding="utf-8",
    )
    (package_dir / "model.py").write_text(
        "import torch\n"
        "from torch import nn\n\n"
        "class BadModel(nn.Module):\n"
        "    def __init__(self, input_shape=(4,), num_classes=3, hidden_dim=4, hash_dim=4, text_input_dim=4):\n"
        "        super().__init__()\n"
        "        self.fc = nn.Linear(input_shape[0], num_classes)\n"
        "        self.num_classes = num_classes\n"
        "    def forward(self, x):\n"
        "        logits = self.fc(x)\n"
        "        s_init = torch.eye(self.num_classes)\n"
        "        delta = logits @ logits.T\n"
        "        return s_init + delta\n",
        encoding="utf-8",
    )
    executor = ToolExecutor(state)

    result = executor._tool_run_smoke_forward(module="agent_pkg.model", target="BadModel")

    assert result["ok"] is False
    assert result["error_type"] in {"RuntimeError", "ValueError"}
    assert "size" in result["error_message"].lower() or "shape" in result["error_message"].lower()


# ═════════════════════════════════════════════════════════════════════
# Loop robustness and agentic HITL
# ═════════════════════════════════════════════════════════════════════


def test_stall_warning_message_includes_reason():
    msg = _stall_warning_message("cyclic_calls: run_smoke_forward called 3x")
    assert "cyclic_calls" in msg
    assert "ONE CHANCE" in msg


def test_terminate_decision_only_handles_budget_gate():
    """_terminate_decision no longer handles success/stall —
    those are in the loop now."""
    trace = AgentTrace(
        id="trace_1",
        run_id="r1",
        task_id="t1",
        file_path="test.py",
        actions=[],
        total_steps=0,
        final_status="success",
        created_at="2025-01-01",
    )
    # With low counts → continue.
    assert (
        _terminate_decision(
            trace=trace, step_count=2, tool_call_count=3, wall_elapsed=10, verdict=None
        )
        == "continue"
    )
    # Budget exceeded → timeout.
    assert (
        _terminate_decision(
            trace=trace, step_count=10, tool_call_count=3, wall_elapsed=10, verdict=None
        )
        == "timeout"
    )
    # success/stall are NOT in the return type anymore.
    result = _terminate_decision(
        trace=trace, step_count=2, tool_call_count=3, wall_elapsed=10, verdict={"ok": True}
    )
    assert result == "continue"  # success detection moved to loop


def test_detect_smoke_failure_low_conf_two_or_more():
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[
            AgentAction(
                step=1,
                action_type="tool_result",
                tool_name="run_smoke_forward",
                reasoning='{"ok": false, "error": "shape"}',
            ),
            AgentAction(
                step=2,
                action_type="tool_result",
                tool_name="run_smoke_forward",
                reasoning='{"ok": false, "error": "shape again"}',
            ),
        ],
        total_steps=2,
        final_status="success",
        created_at="2025-01-01",
    )
    assert _detect_smoke_failure_low_conf(trace) is True


def test_detect_smoke_failure_low_conf_single_failure_not_enough():
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[
            AgentAction(
                step=1,
                action_type="tool_result",
                tool_name="run_smoke_forward",
                reasoning='{"ok": false, "error": "shape"}',
            ),
        ],
        total_steps=1,
        final_status="success",
        created_at="2025-01-01",
    )
    assert _detect_smoke_failure_low_conf(trace) is False


def test_hitl_gate_master_kill_switch_off():
    """When interactive=False, all triggers are no-ops."""
    state = {"hitl_gate_config": HITLGateConfig(interactive=False, on_stall=True)}
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[],
        total_steps=0,
        final_status="success",
        created_at="2025-01-01",
    )
    result = _hitl_gate(state, trace, "on_stall", {"stall_reason": "cycle"})
    assert result is None


def test_hitl_gate_trigger_disabled_by_default():
    """Default config has all triggers off."""
    state = {"hitl_gate_config": HITLGateConfig(interactive=True)}
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[],
        total_steps=0,
        final_status="success",
        created_at="2025-01-01",
    )
    result = _hitl_gate(state, trace, "on_stall")
    assert result is None


def test_hitl_gate_fires_when_enabled():
    """When trigger is on and consult_fn is set, gate returns response."""
    calls = []

    def fake_consult(trigger, prompt, context):
        calls.append((trigger, context))
        return "change_approach"

    state = {
        "hitl_gate_config": HITLGateConfig(interactive=True, on_stall=True),
        "hitl_consult_fn": fake_consult,
    }
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[],
        total_steps=0,
        final_status="success",
        created_at="2025-01-01",
    )
    result = _hitl_gate(state, trace, "on_stall", {"stall_reason": "cycle"})
    assert result is not None
    assert "change_approach" in result
    assert len(calls) == 1
    assert calls[0][0] == "on_stall"


def test_hitl_gate_cooldown_prevents_spam():
    """Same trigger won't fire twice within one task."""
    calls = []

    def fake_consult(trigger, prompt, context):
        calls.append(trigger)
        return "ok"

    state = {
        "hitl_gate_config": HITLGateConfig(interactive=True, on_stall=True),
        "hitl_consult_fn": fake_consult,
    }
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[],
        total_steps=0,
        final_status="success",
        created_at="2025-01-01",
    )

    fired: set[str] = set()
    r1 = _hitl_gate(state, trace, "on_stall", fired_set=fired)
    assert r1 is not None
    r2 = _hitl_gate(state, trace, "on_stall", fired_set=fired)
    assert r2 is None  # cooldown
    assert len(calls) == 1


def test_hitl_gate_needs_consult_fn():
    """Without consult_fn, gate is silent."""
    state = {
        "hitl_gate_config": HITLGateConfig(interactive=True, on_stall=True),
    }
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[],
        total_steps=0,
        final_status="success",
        created_at="2025-01-01",
    )
    assert _hitl_gate(state, trace, "on_stall") is None


def test_latest_verdict_parses_smoke_ok():
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[
            AgentAction(
                step=1,
                action_type="tool_result",
                tool_name="run_smoke_forward",
                reasoning='{"ok": true, "shapes": {"output": [2,3]}}',
            ),
        ],
        total_steps=1,
        final_status="success",
        created_at="2025-01-01",
    )
    v = _latest_verdict(trace)
    assert v is not None
    assert v.get("ok") is True


def test_latest_verdict_parses_smoke_fail():
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="x.py",
        actions=[
            AgentAction(
                step=1,
                action_type="tool_result",
                tool_name="run_smoke_forward",
                reasoning='{"ok": false, "error_type": "RuntimeError"}',
            ),
        ],
        total_steps=1,
        final_status="success",
        created_at="2025-01-01",
    )
    v = _latest_verdict(trace)
    assert v is not None
    assert v.get("ok") is False


def test_agent_loop_soft_warn_then_hard_stop_integration(tmp_path, monkeypatch):
    """Full loop: stall → soft-warn → one more round → hard stop."""
    state = _tool_state(tmp_path)
    task = FileGenerationTask(
        id="task_model",
        path="src/agent_pkg/model.py",
        purpose="model",
        generation_mode="hybrid",
        plan_fields=["model"],
        layer=1,
    )
    state["file_tasks"] = [task]
    state["current_agent_task"] = task
    state["hitl_gate_config"] = HITLGateConfig()  # all off

    call_count = [0]
    # Round 1: LLM calls run_smoke_forward with a failure
    # Round 2: LLM calls run_smoke_forward again (same tool → cycle)
    # Round 3: LLM calls it again → triggers stall detection
    # After soft-warn: LLM still calls same tool → hard stop

    class BoundLLM:
        def __init__(self) -> None:
            self.invoke_kwargs = None

        def invoke(self, messages, **kwargs):
            self.invoke_kwargs = kwargs
            call_count[0] += 1
            # Each "round" the LLM calls run_smoke_forward with identical args.
            return AIMessage(
                content="Let me check the smoke test.",
                tool_calls=[
                    {
                        "name": "run_smoke_forward",
                        "args": {"module": "agent_pkg.model", "target": "TestModel"},
                        "id": f"call_{call_count[0]}",
                    }
                ],
            )

    class FakeLLM:
        def __init__(self) -> None:
            self.bound_tools = None
            self.bound = BoundLLM()

        def bind_tools(self, tools):
            self.bound_tools = tools
            return self.bound

    fake_llm = FakeLLM()
    monkeypatch.setattr(agent_mod.Settings, "load", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(agent_mod, "build_model", lambda _settings: fake_llm)

    # Make run_smoke_forward fail every time.
    def fake_smoke(**kwargs):
        state.setdefault("tool_calls", []).append(
            type(
                "FakeRecord",
                (),
                {
                    "id": "tc_smoke",
                    "run_id": "run_1",
                    "phase": "generation",
                    "tool_name": "run_smoke_forward",
                    "input_hash": "abc",
                    "output_hash": "def",
                    "status": "succeeded",
                    "started_at": "2025-01-01",
                    "ended_at": "2025-01-01",
                    "error": None,
                    "artifacts_written": [],
                },
            )()
        )
        return {"ok": False, "error_type": "RuntimeError", "error_message": "some runtime failure"}

    # Patch ToolExecutor.call only for run_smoke_forward
    original_call = ToolExecutor.call

    def patched_call(self, name, **kwargs):
        if name == "run_smoke_forward":
            return fake_smoke(**kwargs)
        return original_call(self, name, **kwargs)

    monkeypatch.setattr(ToolExecutor, "call", patched_call)

    trace = run_file_generation_agent(state, task)

    # With run_smoke_forward always failing + always being called,
    # the loop should detect stall (cyclic calls), soft-warn, then hard stop.
    assert trace.final_status in {"stall", "timeout"}
    # The trace should have a think action recording the stall warning.
    stall_warnings = [
        a
        for a in trace.actions
        if a.action_type == "think" and "Stall warning" in (a.reasoning or "")
    ]
    assert len(stall_warnings) >= 1


def test_llm_attribute_plan_invalid_is_best_effort(monkeypatch):
    """LLM attribution returns None on parse failure — non-blocking."""
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="model.py",
        actions=[
            AgentAction(
                step=1,
                action_type="tool_result",
                tool_name="run_smoke_forward",
                reasoning='{"ok": false, "error": "shape mismatch"}',
            ),
        ],
        total_steps=1,
        final_status="timeout",
        stall_reason="budget exceeded",
        created_at="2025-01-01",
    )

    class FakeLLM:
        def invoke(self, messages):
            return SimpleNamespace(content="not json at all")

    task = FileGenerationTask(
        id="t1",
        path="model.py",
        purpose="model",
        generation_mode="hybrid",
        plan_fields=["model"],
        layer=1,
    )

    result = _llm_attribute_plan_invalid(trace, FakeLLM(), {}, task)
    assert result is None  # best-effort, no crash


def test_llm_attribute_plan_invalid_returns_step_on_match(monkeypatch):
    """When LLM returns a valid attribution, the step_id is returned."""
    trace = AgentTrace(
        id="t1",
        run_id="r1",
        task_id="t1",
        file_path="model.py",
        actions=[
            AgentAction(
                step=1,
                action_type="tool_result",
                tool_name="run_smoke_forward",
                reasoning='{"ok": false, "error": "mat1 and mat2 shapes cannot be multiplied"}',
            ),
        ],
        total_steps=1,
        final_status="timeout",
        stall_reason="budget exceeded",
        created_at="2025-01-01",
    )

    class FakeLLM:
        def invoke(self, messages):
            return SimpleNamespace(
                content='{"is_plan_fault": true, "plan_step": "design_tensor_contract", '
                '"explanation": "Tensor contract was wrong"}'
            )

    task = FileGenerationTask(
        id="t1",
        path="model.py",
        purpose="model",
        generation_mode="hybrid",
        plan_fields=["model"],
        layer=1,
    )

    result = _llm_attribute_plan_invalid(trace, FakeLLM(), {}, task)
    assert result == "design_tensor_contract"
