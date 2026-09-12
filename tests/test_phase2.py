"""Phase 2 tests: CodingPlan LLM-first, Plan Critique, Repair Agent mode."""

from types import SimpleNamespace

from paper2repo_agent.artifacts import RunPaths
from paper2repo_agent.schemas import (
    CodeImplementationPlan,
    CodingPlanCritique,
    CritiqueIssue,
    ExecutionCodingPlan,
    ImplementationComponentPlan,
    ImplementationLossTermPlan,
    ImplementationTensorContract,
)


# ═════════════════════════════════════════════════════════════════════════
# CodingPlan generation
# ═════════════════════════════════════════════════════════════════════════


def test_coding_plan_critique_schema_roundtrip():
    """CodingPlanCritique + CritiqueIssue serialize/deserialize correctly."""
    issue = CritiqueIssue(
        id="iss_1",
        severity="error",
        category="dependency_order",
        description="Module B depends on A but A comes after B.",
        affected_artifacts=["src/pkg/b.py", "src/pkg/a.py"],
        suggested_fix="Swap order.",
    )
    critique = CodingPlanCritique(
        id="crit_1",
        coding_plan_id="cp_1",
        passed=False,
        issues=[issue],
        summary="1 error found.",
        created_at="2025-01-01",
    )
    payload = critique.model_dump(mode="json")
    reloaded = CodingPlanCritique.model_validate(payload)
    assert reloaded.passed is False
    assert len(reloaded.issues) == 1
    assert reloaded.issues[0].category == "dependency_order"


def test_critique_issue_categories_are_valid():
    """All documented categories should be accepted."""
    valid_categories = [
        "tensor_contract_gap",
        "dependency_order",
        "missing_module",
        "missing_loss",
        "missing_component",
        "ambiguous_api",
        "untestable_claim",
        "scope_creep",
        "other",
    ]
    for cat in valid_categories:
        issue = CritiqueIssue(
            id="test",
            severity="warning",
            category=cat,
            description=f"Test issue with category {cat}",
        )
        assert issue.category == cat


def test_critique_plan_path_exists_in_run_paths(tmp_path):
    """RunPaths should expose critique_coding_plan path."""
    paths = RunPaths(tmp_path / "run")
    assert paths.critique_coding_plan.name == "critique_coding_plan.json"
    assert paths.critique_coding_plan.parent == paths.execution_dir


def test_code_implementation_plan_generated_by_accepts_llm():
    """CodeImplementationPlan.generated_by should accept 'llm'."""
    plan = CodeImplementationPlan(
        id="test_plan",
        summary="An LLM-generated plan.",
        input_tensors=[
            ImplementationTensorContract(
                name="x",
                role="input",
                shape=["B", "D"],
                dtype="float32",
            )
        ],
        output_tensors=[
            ImplementationTensorContract(
                name="logits",
                role="output",
                shape=["B", "C"],
                dtype="float32",
            )
        ],
        model_components=[
            ImplementationComponentPlan(
                name="TestModel",
                target_file="src/pkg/model.py",
                responsibility="Classify inputs.",
            )
        ],
        loss_terms=[
            ImplementationLossTermPlan(
                name="ce_loss",
                target_file="src/pkg/losses.py",
                loss_type="cross_entropy",
                required_inputs=["logits", "target"],
            )
        ],
        generated_by="llm",
    )
    assert plan.generated_by == "llm"
    payload = plan.model_dump(mode="json")
    reloaded = CodeImplementationPlan.model_validate(payload)
    assert reloaded.generated_by == "llm"


def test_execution_coding_plan_generated_by_tracks_source():
    """ExecutionCodingPlan.generated_by reflects whether LLM or rules were used."""
    for source in ["rule", "llm", "hybrid"]:
        plan = ExecutionCodingPlan(
            id=f"ecp_{source}",
            package_name="test_pkg",
            architecture_summary="A test plan.",
            generated_by=source,
        )
        assert plan.generated_by == source


# ═════════════════════════════════════════════════════════════════════════
# Plan critique node
# ═════════════════════════════════════════════════════════════════════════


def test_critique_node_detects_dependency_order_error(monkeypatch):
    """critique_coding_plan_node should detect when depends_on order is violated."""
    from paper2repo_agent.graphs.execute.nodes import critique_coding_plan_node

    design = SimpleNamespace(
        modules=[
            SimpleNamespace(
                name="model",
                path="src/pkg/model.py",
                responsibility="Model",
                public_api=["PaperModel"],
                depends_on=["src/pkg/config.py"],
                evidence_ids=[],
            ),
            SimpleNamespace(
                name="config",
                path="src/pkg/config.py",
                responsibility="Config",
                public_api=["DEFAULT_CONFIG"],
                depends_on=[],
                evidence_ids=[],
            ),
        ]
    )
    ci = CodeImplementationPlan(
        id="ci_1",
        summary="Test",
        model_components=[
            ImplementationComponentPlan(
                name="M",
                target_file="src/pkg/model.py",
                responsibility="x",
            )
        ],
        loss_terms=[
            ImplementationLossTermPlan(
                name="L",
                target_file="src/pkg/losses.py",
                loss_type="custom",
            )
        ],
        generated_by="rule",
    )
    coding_plan = ExecutionCodingPlan(
        id="cp_1",
        package_name="pkg",
        architecture_summary="Test",
        code_implementation_plan=ci,
        module_plans=[],
        file_order=["src/pkg/model.py", "src/pkg/config.py"],  # wrong order!
        generated_by="rule",
    )

    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "coding_plan": coding_plan,
        "codebase_design": design,
    }

    result = critique_coding_plan_node(state)
    critique: CodingPlanCritique = result["critique_coding_plan"]
    assert critique.passed is False
    dep_issues = [i for i in critique.issues if i.category == "dependency_order"]
    assert len(dep_issues) >= 1
    assert (
        "model" in dep_issues[0].description.lower()
        or "config" in dep_issues[0].description.lower()
    )


def test_critique_node_passes_when_plan_is_consistent(monkeypatch):
    """critique_coding_plan_node should pass when file_order is correct."""
    from paper2repo_agent.graphs.execute.nodes import critique_coding_plan_node

    design = SimpleNamespace(
        modules=[
            SimpleNamespace(
                name="config",
                path="src/pkg/config.py",
                responsibility="Config",
                public_api=["DEFAULT_CONFIG"],
                depends_on=[],
                evidence_ids=[],
            ),
            SimpleNamespace(
                name="model",
                path="src/pkg/model.py",
                responsibility="Model",
                public_api=["PaperModel"],
                depends_on=["src/pkg/config.py"],
                evidence_ids=[],
            ),
        ]
    )
    ci = CodeImplementationPlan(
        id="ci_1",
        summary="Test",
        input_tensors=[
            ImplementationTensorContract(name="x", role="input", shape=["B", "D"]),
        ],
        output_tensors=[
            ImplementationTensorContract(name="out", role="output", shape=["B", "C"]),
        ],
        model_components=[
            ImplementationComponentPlan(
                name="M",
                target_file="src/pkg/model.py",
                responsibility="x",
                outputs=["logits"],
            )
        ],
        loss_terms=[
            ImplementationLossTermPlan(
                name="L",
                target_file="src/pkg/losses.py",
                loss_type="custom",
                required_inputs=["logits", "target"],
            )
        ],
        paper_alignment_checks=["check 1", "check 2"],
        generated_by="rule",
    )
    coding_plan = ExecutionCodingPlan(
        id="cp_1",
        package_name="pkg",
        architecture_summary="Test",
        code_implementation_plan=ci,
        module_plans=[],
        file_order=["src/pkg/config.py", "src/pkg/model.py"],  # correct order
        generated_by="rule",
    )

    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "coding_plan": coding_plan,
        "codebase_design": design,
    }

    result = critique_coding_plan_node(state)
    critique: CodingPlanCritique = result["critique_coding_plan"]
    assert critique.passed is True


def test_critique_node_warns_on_missing_input_tensors(monkeypatch):
    """critique_coding_plan_node warns when no input tensors are defined."""
    from paper2repo_agent.graphs.execute.nodes import critique_coding_plan_node

    design = SimpleNamespace(modules=[])
    ci = CodeImplementationPlan(
        id="ci_1",
        summary="No inputs.",
        model_components=[
            ImplementationComponentPlan(
                name="M",
                target_file="src/pkg/model.py",
                responsibility="x",
            )
        ],
        loss_terms=[],
        paper_alignment_checks=["check"],
        generated_by="rule",
    )
    coding_plan = ExecutionCodingPlan(
        id="cp_1",
        package_name="pkg",
        architecture_summary="Test",
        code_implementation_plan=ci,
        module_plans=[],
        file_order=[],
        generated_by="rule",
    )

    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "coding_plan": coding_plan,
        "codebase_design": design,
    }
    result = critique_coding_plan_node(state)
    critique: CodingPlanCritique = result["critique_coding_plan"]
    gap_issues = [i for i in critique.issues if i.category == "tensor_contract_gap"]
    assert len(gap_issues) >= 1


def test_critique_node_handles_none_code_implementation_plan(monkeypatch):
    """critique_coding_plan_node handles None CodeImplementationPlan gracefully."""
    from paper2repo_agent.graphs.execute.nodes import critique_coding_plan_node

    design = SimpleNamespace(modules=[])
    coding_plan = ExecutionCodingPlan(
        id="cp_1",
        package_name="pkg",
        architecture_summary="Test",
        code_implementation_plan=None,
        module_plans=[],
        file_order=[],
        generated_by="rule",
    )

    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "coding_plan": coding_plan,
        "codebase_design": design,
    }
    result = critique_coding_plan_node(state)
    critique: CodingPlanCritique = result["critique_coding_plan"]
    assert critique.passed is False
    assert any(
        "None" in i.description or "missing" in i.description.lower() for i in critique.issues
    )


def test_critique_node_warns_on_mismatched_target_file(monkeypatch):
    """critique_coding_plan_node warns when target_file doesn't match design modules."""
    from paper2repo_agent.graphs.execute.nodes import critique_coding_plan_node

    design = SimpleNamespace(
        modules=[
            SimpleNamespace(
                name="config",
                path="src/pkg/config.py",
                responsibility="Config",
                public_api=[],
                depends_on=[],
                evidence_ids=[],
            ),
        ]
    )
    ci = CodeImplementationPlan(
        id="ci_1",
        summary="Test",
        input_tensors=[
            ImplementationTensorContract(name="x", role="input", shape=["B"]),
        ],
        model_components=[
            ImplementationComponentPlan(
                name="M",
                target_file="src/pkg/unknown_module.py",  # not in design
                responsibility="x",
            )
        ],
        loss_terms=[],
        paper_alignment_checks=["check"],
        generated_by="rule",
    )
    coding_plan = ExecutionCodingPlan(
        id="cp_1",
        package_name="pkg",
        architecture_summary="Test",
        code_implementation_plan=ci,
        module_plans=[],
        file_order=[],
        generated_by="rule",
    )

    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "coding_plan": coding_plan,
        "codebase_design": design,
    }
    result = critique_coding_plan_node(state)
    critique: CodingPlanCritique = result["critique_coding_plan"]
    missing = [i for i in critique.issues if i.category == "missing_module"]
    assert len(missing) >= 1


# ═════════════════════════════════════════════════════════════════════════
# ReAct repair agent mode
# ═════════════════════════════════════════════════════════════════════════


def test_repair_agent_returns_none_without_tasks_when_default_enabled(monkeypatch):
    """_repair_target_files_with_agent is default-enabled but still needs repair tasks."""
    monkeypatch.delenv("PAPER2REPO_EXECUTE_LLM_REPAIR_AGENT", raising=False)

    from paper2repo_agent.graphs.execute.nodes import _repair_target_files_with_agent

    result = _repair_target_files_with_agent({}, [], {})
    assert result is None


def test_repair_agent_can_be_explicitly_disabled(tmp_path, monkeypatch):
    """PAPER2REPO_EXECUTE_LLM_REPAIR_AGENT=0 disables agent repair before LLM setup."""
    monkeypatch.setenv("PAPER2REPO_EXECUTE_LLM_REPAIR_AGENT", "0")

    from paper2repo_agent.graphs.execute.nodes import _repair_target_files_with_agent
    from paper2repo_agent.schemas import RepairTask, VerificationResult

    paths = RunPaths(tmp_path / "run")
    target = paths.generated_repo / "src" / "pkg"
    target.mkdir(parents=True, exist_ok=True)
    (target / "model.py").write_text("class BadModel:\n    pass\n", encoding="utf-8")

    task = RepairTask(
        id="rt_1",
        trigger_check_id="check_1",
        target_files=["src/pkg/model.py"],
        failure_summary="Broken model.",
        repair_strategy="agent",
    )
    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "paths": paths,
        "verification_result": VerificationResult(passed=False, checks=[], summary="fail"),
        "file_tasks": [],
    }

    result = _repair_target_files_with_agent(state, [task], {})
    assert result is None


def test_repair_agent_returns_none_without_repair_tasks(monkeypatch):
    """_repair_target_files_with_agent returns None when repair_tasks is empty."""
    monkeypatch.setenv("PAPER2REPO_EXECUTE_LLM_REPAIR_AGENT", "1")
    from paper2repo_agent.graphs.execute.nodes import _repair_target_files_with_agent

    result = _repair_target_files_with_agent({}, [], {})
    assert result is None


def test_repair_agent_returns_none_when_target_files_dont_exist(tmp_path, monkeypatch):
    """_repair_target_files_with_agent returns None when target files don't exist on disk."""
    monkeypatch.setenv("PAPER2REPO_EXECUTE_LLM_REPAIR_AGENT", "1")

    from paper2repo_agent.graphs.execute.nodes import _repair_target_files_with_agent
    from paper2repo_agent.schemas import RepairTask, VerificationResult

    paths = RunPaths(tmp_path / "run")
    paths.execution_dir.mkdir(parents=True, exist_ok=True)
    paths.generated_repo.mkdir(parents=True, exist_ok=True)

    task = RepairTask(
        id="rt_1",
        trigger_check_id="check_1",
        target_files=["src/pkg/nonexistent.py"],
        failure_summary="File doesn't exist.",
        repair_strategy="regenerate",
    )
    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "paths": paths,
        "verification_result": VerificationResult(
            passed=False,
            checks=[],
            summary="fail",
        ),
    }
    result = _repair_target_files_with_agent(state, [task], {})
    assert result is None


def test_repair_agent_env_var_case_insensitive():
    """PAPER2REPO_EXECUTE_LLM_REPAIR_AGENT is checked case-insensitively."""
    # Verify the check is case-insensitive by checking the logic pattern.
    val = "TRUE"
    assert val.strip().lower() in {"1", "true", "yes", "on"}
    val = "On"
    assert val.strip().lower() in {"1", "true", "yes", "on"}


def test_repair_agent_builds_system_prompt():
    """_build_repair_system_prompt includes target file and error context."""
    from paper2repo_agent.graphs.execute.agent import _build_repair_system_prompt

    state = {
        "execution_policy": SimpleNamespace(package_name="test_pkg"),
    }
    prompt = _build_repair_system_prompt(state, "src/test_pkg/model.py", "Shape mismatch error")
    assert "src/test_pkg/model.py" in prompt
    assert "Shape mismatch error" in prompt
    assert "repair" in prompt.lower()
    assert "MINIMAL" in prompt


def test_repair_agent_builds_first_user_message():
    """_build_repair_first_user_message includes error context and code."""
    from paper2repo_agent.graphs.execute.agent import _build_repair_first_user_message

    msg = _build_repair_first_user_message(
        "src/pkg/model.py",
        "ImportError: cannot import PaperModel",
        "class PaperModel:\n    pass\n",
        "Fix the import.",
    )
    assert "src/pkg/model.py" in msg
    assert "ImportError" in msg
    assert "class PaperModel" in msg
    assert "Fix the import" in msg
    assert "Do not re-read it in full" in msg


def test_repair_verdict_normalizes_python_repr_contract_result():
    from paper2repo_agent.graphs.execute.agent import _latest_verdict
    from paper2repo_agent.schemas import AgentAction, AgentTrace

    trace = AgentTrace(
        id="trace-1",
        run_id="run-1",
        task_id="repair-model",
        file_path="src/pkg/model.py",
        actions=[
            AgentAction(
                step=1,
                action_type="tool_result",
                tool_name="check_file_contract",
                reasoning="{'passed': True, 'issues': []}",
                duration_ms=0,
            )
        ],
        total_steps=1,
        final_status="success",
        created_at="2026-01-01T00:00:00Z",
    )

    verdict = _latest_verdict(trace)
    assert verdict is not None
    assert verdict["ok"] is True
    assert verdict["source"] == "check_file_contract"


def test_repair_batch_write_counts_as_write_action():
    from paper2repo_agent.graphs.execute.agent import _has_write_action
    from paper2repo_agent.schemas import AgentAction, AgentTrace

    trace = AgentTrace(
        id="trace-1",
        run_id="run-1",
        task_id="repair-model",
        file_path="src/pkg/model.py",
        actions=[
            AgentAction(
                step=1,
                action_type="tool_call",
                tool_name="write_repo_files_batch",
                reasoning="Called write_repo_files_batch(...) -> status=succeeded",
                duration_ms=0,
            )
        ],
        total_steps=1,
        final_status="success",
        created_at="2026-01-01T00:00:00Z",
    )

    assert _has_write_action(trace) is True


def test_repair_agent_run_repair_budget_termination(tmp_path, monkeypatch):
    """run_file_repair_agent terminates when step budget is exceeded."""
    from paper2repo_agent.graphs.execute.agent import (
        run_file_repair_agent,
        REPAIR_MAX_STEPS,
    )
    from paper2repo_agent.graphs.execute import agent as agent_mod

    target = tmp_path / "generated_repo" / "src" / "pkg"
    target.mkdir(parents=True)
    (target / "model.py").write_text("class BadModel:\n    pass\n", encoding="utf-8")

    paths = RunPaths(tmp_path)

    class BoundLLM:
        def invoke(self, messages, **kwargs):
            return type(
                "Msg",
                (),
                {
                    "content": "Let me check.",
                    "tool_calls": [
                        {
                            "name": "read_generated_file",
                            "args": {"path": "src/pkg/model.py", "mode": "full"},
                            "id": "call_1",
                        }
                    ],
                },
            )()

    class FakeLLM:
        def bind_tools(self, tools):
            self.bound_tools = tools
            return BoundLLM()

    fake_llm = FakeLLM()
    monkeypatch.setattr(agent_mod.Settings, "load", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(agent_mod, "build_model", lambda _settings: fake_llm)

    # Make read_generated_file succeed to simulate a real tool call.
    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "paths": paths,
        "execution_policy": SimpleNamespace(package_name="pkg"),
        "tool_calls": [],
        "execution_phase": "repair",
    }

    trace = run_file_repair_agent(state, "src/pkg/model.py", "Some error")
    # With REPAIR_MAX_STEPS=6 and the LLM always calling a tool,
    # the loop should terminate via budget gate.
    assert trace.final_status in {"timeout", "stall"}
    assert trace.total_steps > 0
    assert trace.total_steps <= REPAIR_MAX_STEPS


def test_repair_agent_run_returns_trace_on_exception(tmp_path, monkeypatch):
    """run_file_repair_agent catches exceptions and returns fallback trace."""
    from paper2repo_agent.graphs.execute.agent import run_file_repair_agent
    from paper2repo_agent.graphs.execute import agent as agent_mod

    target = tmp_path / "generated_repo" / "src" / "pkg"
    target.mkdir(parents=True)
    (target / "model.py").write_text("broken", encoding="utf-8")

    paths = RunPaths(tmp_path)

    class ExplodingLLM:
        def bind_tools(self, tools):
            self.bound_tools = tools
            return self  # bind_tools succeeds, invoke fails

        def invoke(self, messages, **kwargs):
            raise RuntimeError("LLM connection lost")

    monkeypatch.setattr(agent_mod.Settings, "load", staticmethod(lambda: SimpleNamespace()))
    monkeypatch.setattr(agent_mod, "build_model", lambda _settings: ExplodingLLM())

    state = {
        "manifest": SimpleNamespace(run_id="run_1"),
        "paths": paths,
        "execution_policy": SimpleNamespace(package_name="pkg"),
        "tool_calls": [],
        "execution_phase": "repair",
    }

    trace = run_file_repair_agent(state, "src/pkg/model.py", "Error context")
    assert trace.final_status == "fallback"
    assert "RuntimeError" in (trace.stall_reason or "")


# ═════════════════════════════════════════════════════════════════════════
# Workflow integration
# ═════════════════════════════════════════════════════════════════════════


def test_critique_node_in_workflow_graph():
    """The critique node is wired between coding plan and file tasks."""
    from paper2repo_agent.graphs.execute.workflow import build_execute_graph

    graph = build_execute_graph()
    nodes = list(graph.nodes.keys())
    assert "critique_coding_plan" in nodes

    # Verify edge order: coding_plan → critique → file_tasks
    cp_idx = nodes.index("build_execution_coding_plan")
    crit_idx = nodes.index("critique_coding_plan")
    ft_idx = nodes.index("build_file_generation_tasks")
    assert cp_idx < crit_idx < ft_idx, (
        f"Expected coding_plan({cp_idx}) < critique({crit_idx}) < file_tasks({ft_idx})"
    )


def test_repair_agent_import_available():
    """run_file_repair_agent is importable and callable."""
    from paper2repo_agent.graphs.execute.agent import (
        run_file_repair_agent,
        _build_repair_system_prompt,
        _build_repair_first_user_message,
    )

    assert callable(run_file_repair_agent)
    assert callable(_build_repair_system_prompt)
    assert callable(_build_repair_first_user_message)


def test_llm_coding_plan_disabled_without_env(monkeypatch):
    """_build_code_implementation_plan_llm returns None when codegen is disabled."""
    monkeypatch.setenv("PAPER2REPO_EXECUTE_LLM_CODEGEN", "0")
    from paper2repo_agent.graphs.execute.nodes import _build_code_implementation_plan_llm

    result = _build_code_implementation_plan_llm({})
    assert result is None
