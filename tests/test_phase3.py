"""Phase 3 tests: Plan feedback loop, rollback gate, PlanningTrace revision."""

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

from paper2repo_agent.artifacts import RunPaths
from paper2repo_agent.schemas import (
    AgentAction,
    AgentTrace,
    PlanFeedbackItem,
    PlanFeedbackReport,
    PlanTraceRevision,
    PlanningReasoningStep,
    PlanningTrace,
    VerificationResult,
)


# ═════════════════════════════════════════════════════════════════════════
# Plan feedback schemas
# ═════════════════════════════════════════════════════════════════════════


def test_plan_feedback_item_roundtrip():
    """PlanFeedbackItem serializes/deserializes correctly."""
    item = PlanFeedbackItem(
        planning_step_id="step_5",
        hypothesis="S_init shape is (C,C)",
        falsifiable_via="run_smoke_forward reports shape mismatch",
        verdict="falsified",
        actual_result="Runtime error: shapes (4,4) and (16,16) cannot be added",
    )
    payload = item.model_dump(mode="json")
    reloaded = PlanFeedbackItem.model_validate(payload)
    assert reloaded.verdict == "falsified"
    assert reloaded.planning_step_id == "step_5"
    assert reloaded.actual_result is not None


def test_plan_feedback_report_aggregates_counts():
    """PlanFeedbackReport correctly tracks confirmed/falsified/untested counts."""
    items = [
        PlanFeedbackItem(
            planning_step_id="s1",
            hypothesis="H1",
            falsifiable_via="F1",
            verdict="confirmed",
        ),
        PlanFeedbackItem(
            planning_step_id="s2",
            hypothesis="H2",
            falsifiable_via="F2",
            verdict="falsified",
            actual_result="Shape mismatch",
        ),
        PlanFeedbackItem(
            planning_step_id="s3",
            hypothesis="H3",
            falsifiable_via="F3",
            verdict="falsified",
            actual_result="Import error",
        ),
        PlanFeedbackItem(
            planning_step_id="s4",
            hypothesis="H4",
            falsifiable_via="F4",
            verdict="untested",
        ),
        PlanFeedbackItem(
            planning_step_id="s5",
            hypothesis="H5",
            falsifiable_via="F5",
            verdict="inconclusive",
            actual_result="Verification partial",
        ),
    ]
    report = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_1",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        items=items,
        confirmed_count=1,
        falsified_count=2,
        inconclusive_count=1,
        untested_count=1,
        rollback_triggered=True,
        summary="2 falsified.",
        created_at="2025-01-01",
    )
    assert report.falsified_count == 2
    assert report.confirmed_count == 1
    assert report.inconclusive_count == 1
    assert report.untested_count == 1
    assert report.rollback_triggered is True


def test_plan_feedback_item_verdict_literals():
    """Only valid verdict values are accepted."""
    valid = ["confirmed", "falsified", "untested", "inconclusive"]
    for v in valid:
        item = PlanFeedbackItem(
            planning_step_id="s",
            hypothesis="h",
            falsifiable_via="f",
            verdict=v,
        )
        assert item.verdict == v


def test_plan_feedback_paths_exist_in_run_paths(tmp_path):
    """RunPaths exposes plan_feedback and plan_trace_revision paths."""
    paths = RunPaths(tmp_path / "run")
    assert paths.plan_feedback.name == "plan_feedback.json"
    assert paths.plan_feedback.parent == paths.execution_dir
    assert paths.plan_trace_revision.name == "plan_trace_revision.json"
    assert paths.plan_trace_revision.parent == paths.execution_dir


def test_plan_trace_revision_records_diff():
    """PlanTraceRevision captures before/after step sets."""
    before = [
        PlanningReasoningStep(
            step_id="step_3",
            sequence=3,
            reasoning_type="design_tensor_contract",
            reasoning="S is (C,C)",
            hypothesis="S_init = eye(C)",
            falsifiable_via="smoke shape check",
            confidence=0.8,
        ),
    ]
    after = [
        PlanningReasoningStep(
            step_id="step_3",
            sequence=3,
            reasoning_type="design_tensor_contract",
            reasoning="S is (B,C) sample-level",
            hypothesis="S_init computes per-sample Jaccard in (B,C)",
            falsifiable_via="smoke shape check confirms (B,C) output",
            confidence=0.9,
        ),
    ]
    revision = PlanTraceRevision(
        id="ptr_1",
        run_id="run_1",
        plan_id="plan_1",
        trigger_step_ids=["step_3"],
        original_trace_id="pt_1",
        revised_trace_id="pt_1_revised",
        steps_before=before,
        steps_after=after,
        revision_rationale="Shape mismatch (C,C) vs (B,B) — revised to (B,C).",
        evidence_from_execution=["File: model.py, Status: plan_assumption_invalid"],
        created_at="2025-01-01",
    )
    payload = revision.model_dump(mode="json")
    reloaded = PlanTraceRevision.model_validate(payload)
    assert len(reloaded.steps_before) == 1
    assert len(reloaded.steps_after) == 1
    assert reloaded.steps_before[0].hypothesis == "S_init = eye(C)"
    assert reloaded.steps_after[0].hypothesis != reloaded.steps_before[0].hypothesis
    assert reloaded.trigger_step_ids == ["step_3"]


def test_agent_trace_iteration_tag_and_merge():
    """Agent traces are kept per rollback iteration and de-duplicated within a round."""
    from paper2repo_agent.graphs.execute.nodes import (
        _agent_trace_for_current_iteration,
        _merge_agent_traces,
    )

    trace = AgentTrace(
        id="at_1",
        run_id="run_test",
        task_id="task_model",
        file_path="src/test_pkg/model.py",
        final_status="plan_assumption_invalid",
        created_at="2025-01-01",
    )
    round_1 = _agent_trace_for_current_iteration(trace, 1)
    updated_round_1 = round_1.model_copy(update={"final_status": "fallback"})

    assert round_1.rollback_iteration == 1
    assert len(_merge_agent_traces([trace], [round_1])) == 2
    merged_same_round = _merge_agent_traces([round_1], [updated_round_1])
    assert len(merged_same_round) == 1
    assert merged_same_round[0].final_status == "fallback"


# ═════════════════════════════════════════════════════════════════════════
# Plan feedback node
# ═════════════════════════════════════════════════════════════════════════


def _make_minimal_state(**overrides):
    """Build a minimal ExecuteGraphState for node testing."""
    from paper2repo_agent.schemas import (
        ApprovedPlan,
        DataStrategy,
        DependencyStrategy,
        ExecutionPolicy,
        HumanDecisions,
        ImplementationStrategy,
        ReproductionObjective,
        ReproductionPlan,
        RunManifest,
        TargetScope,
        UserConstraints,
        VerificationResult,
        VerificationStrategy,
    )

    plan = ReproductionPlan(
        id="plan_1",
        paper_title="Test Paper",
        created_at="2025-01-01",
        objective=ReproductionObjective(description="Reproduce test"),
        user_constraints=UserConstraints(),
        scope=TargetScope(),
        paper_spec_ref="planning/extracted_spec.json",
        implementation_strategy=ImplementationStrategy(
            repo_name="test_repo",
            package_name="test_pkg",
            architecture_summary="CNN model",
        ),
        dependency_strategy=DependencyStrategy(),
        data_strategy=DataStrategy(),
        verification_strategy=VerificationStrategy(),
    )
    approved = ApprovedPlan(
        plan=plan,
        human_decisions=HumanDecisions(review_id="r1"),
        frozen_at="2025-01-01",
        plan_hash="abc123",
        execution_allowed=True,
    )
    manifest = RunManifest(
        run_id="run_test",
        created_at="2025-01-01",
        updated_at="2025-01-01",
        status="executing",
    )
    policy = ExecutionPolicy(
        run_id="run_test",
        repo_root="/tmp/test",
        package_name="test_pkg",
    )
    state = {
        "manifest": manifest,
        "approved_plan": approved,
        "execution_policy": policy,
        "agent_traces": [],
        "verification_result": VerificationResult(passed=True, summary="OK"),
        "paths": RunPaths(Path("/tmp/test_run")),
        "verbose": False,
        "rollback_count": 0,
        **overrides,
    }
    return state


def _sample_planning_trace() -> PlanningTrace:
    return PlanningTrace(
        id="pt_1",
        run_id="run_test",
        plan_id="plan_1",
        steps=[
            PlanningReasoningStep(
                step_id="step_1",
                sequence=1,
                reasoning_type="understand_architecture",
                reasoning="CNN model",
                hypothesis="Standard CNN architecture",
                falsifiable_via="smoke forward",
                confidence=0.9,
            ),
            PlanningReasoningStep(
                step_id="step_2",
                sequence=2,
                reasoning_type="design_tensor_contract",
                reasoning="S is (C,C)",
                hypothesis="S_init = eye(num_classes)",
                falsifiable_via="run_smoke_forward shape check",
                confidence=0.8,
            ),
        ],
        created_at="2025-01-01",
    )


def _sample_plan_trace_revision() -> PlanTraceRevision:
    return PlanTraceRevision(
        id="ptr_1",
        run_id="run_test",
        plan_id="plan_1",
        trigger_step_ids=["step_2"],
        original_trace_id="pt_1",
        revised_trace_id="pt_1_revised",
        steps_before=[
            PlanningReasoningStep(
                step_id="step_2",
                sequence=2,
                reasoning_type="design_tensor_contract",
                reasoning="S is (C,C)",
                hypothesis="S_init = eye(num_classes)",
                falsifiable_via="run_smoke_forward shape check",
                confidence=0.8,
            ),
        ],
        steps_after=[
            PlanningReasoningStep(
                step_id="step_2",
                sequence=2,
                reasoning_type="design_tensor_contract",
                reasoning="S is batch-aware",
                hypothesis="S_init produces per-sample scores",
                falsifiable_via="run_smoke_forward confirms batch shape",
                confidence=0.9,
            ),
        ],
        revision_rationale="Execution showed the old tensor contract was wrong.",
        evidence_from_execution=["shape mismatch"],
        created_at="2025-01-01",
    )


def test_plan_feedback_node_without_planning_trace():
    """When no PlanningTrace is available, produce a minimal report."""
    from paper2repo_agent.graphs.execute.nodes import build_plan_feedback_node

    state = _make_minimal_state()
    # No planning_trace key
    result = build_plan_feedback_node(state)
    assert "plan_feedback" in result
    report = result["plan_feedback"]
    assert isinstance(report, PlanFeedbackReport)
    assert report.planning_trace_id == ""
    assert report.rollback_triggered is False
    assert len(report.items) == 0


def test_plan_feedback_node_confirms_passing_trace():
    """When verification passes, all steps are confirmed."""
    from paper2repo_agent.graphs.execute.nodes import build_plan_feedback_node

    trace = PlanningTrace(
        id="pt_1",
        run_id="run_test",
        plan_id="plan_1",
        steps=[
            PlanningReasoningStep(
                step_id="step_1",
                sequence=1,
                reasoning_type="understand_architecture",
                reasoning="CNN-based model",
                hypothesis="Architecture is a standard CNN",
                falsifiable_via="smoke forward pass",
                confidence=0.9,
            ),
            PlanningReasoningStep(
                step_id="step_2",
                sequence=2,
                reasoning_type="design_tensor_contract",
                reasoning="Input is (B,3,H,W), output is (B,C)",
                hypothesis="Tensor shapes are standard",
                falsifiable_via="smoke shape check",
                confidence=0.85,
            ),
        ],
        created_at="2025-01-01",
    )
    state = _make_minimal_state(planning_trace=trace)
    result = build_plan_feedback_node(state)

    report = result["plan_feedback"]
    assert report.confirmed_count == 2
    assert report.falsified_count == 0
    assert report.rollback_triggered is False
    assert all(item.verdict == "confirmed" for item in report.items)


def test_plan_feedback_node_detects_falsified_from_agent_traces():
    """Agent traces with plan_assumption_invalid mark steps as falsified."""
    from paper2repo_agent.graphs.execute.nodes import build_plan_feedback_node

    trace = PlanningTrace(
        id="pt_1",
        run_id="run_test",
        plan_id="plan_1",
        steps=[
            PlanningReasoningStep(
                step_id="step_1",
                sequence=1,
                reasoning_type="understand_architecture",
                reasoning="CNN model",
                hypothesis="Standard CNN architecture",
                falsifiable_via="smoke forward",
                confidence=0.9,
            ),
            PlanningReasoningStep(
                step_id="step_2",
                sequence=2,
                reasoning_type="design_tensor_contract",
                reasoning="S is (C,C)",
                hypothesis="S_init = eye(num_classes)",
                falsifiable_via="run_smoke_forward shape check",
                confidence=0.8,
            ),
        ],
        created_at="2025-01-01",
    )
    agent_trace = AgentTrace(
        id="at_1",
        run_id="run_test",
        task_id="task_model",
        file_path="src/test_pkg/model.py",
        actions=[
            AgentAction(step=1, action_type="think", reasoning="Checking shapes"),
            AgentAction(step=2, action_type="verify", tool_name="run_smoke_forward"),
        ],
        total_steps=2,
        final_status="plan_assumption_invalid",
        stall_reason="Shape mismatch: (4,4) vs (16,16) cannot be broadcast",
        last_verdict={"ok": False, "error": "shape mismatch"},
        invalidated_planning_step_id="step_2",
        created_at="2025-01-01",
    )

    state = _make_minimal_state(
        planning_trace=trace,
        agent_traces=[agent_trace],
        verification_result=VerificationResult(passed=False, summary="Verification failed"),
    )
    result = build_plan_feedback_node(state)

    report = result["plan_feedback"]
    assert report.falsified_count == 1
    assert report.rollback_triggered is True
    # step_1 should be inconclusive (verification failed), step_2 should be falsified
    step2 = next(i for i in report.items if i.planning_step_id == "step_2")
    assert step2.verdict == "falsified"
    assert step2.actual_result is not None


def test_plan_feedback_node_matches_reasoning_type_invalidation():
    """Agent traces may invalidate a reasoning_type such as design_tensor_contract."""
    from paper2repo_agent.graphs.execute.nodes import build_plan_feedback_node

    agent_trace = AgentTrace(
        id="at_1",
        run_id="run_test",
        task_id="task_model",
        file_path="src/test_pkg/model.py",
        total_steps=2,
        final_status="plan_assumption_invalid",
        stall_reason="Shape mismatch",
        invalidated_planning_step_id="design_tensor_contract",
        rollback_iteration=0,
        created_at="2025-01-01",
    )
    state = _make_minimal_state(
        planning_trace=_sample_planning_trace(),
        agent_traces=[agent_trace],
        verification_result=VerificationResult(passed=False, summary="Verification failed"),
    )
    result = build_plan_feedback_node(state)

    report = result["plan_feedback"]
    step2 = next(i for i in report.items if i.planning_step_id == "step_2")
    assert step2.verdict == "falsified"
    assert report.rollback_triggered is True


def test_plan_feedback_node_ignores_previous_rollback_iteration_traces():
    """Old plan_assumption_invalid traces should not trigger rollback in a later iteration."""
    from paper2repo_agent.graphs.execute.nodes import build_plan_feedback_node

    old_trace = AgentTrace(
        id="at_old",
        run_id="run_test",
        task_id="task_model",
        file_path="src/test_pkg/model.py",
        total_steps=2,
        final_status="plan_assumption_invalid",
        invalidated_planning_step_id="step_2",
        rollback_iteration=0,
        created_at="2025-01-01",
    )
    state = _make_minimal_state(
        planning_trace=_sample_planning_trace(),
        agent_traces=[old_trace],
        rollback_count=1,
        verification_result=VerificationResult(passed=False, summary="Still checking"),
    )
    result = build_plan_feedback_node(state)

    report = result["plan_feedback"]
    step2 = next(i for i in report.items if i.planning_step_id == "step_2")
    assert step2.verdict == "inconclusive"
    assert report.falsified_count == 0
    assert report.inconclusive_count == 2
    assert report.rollback_triggered is False


def test_plan_feedback_node_handles_missing_verification():
    """When no verification result is available, steps are untested."""
    from paper2repo_agent.graphs.execute.nodes import build_plan_feedback_node

    trace = PlanningTrace(
        id="pt_1",
        run_id="run_test",
        plan_id="plan_1",
        steps=[
            PlanningReasoningStep(
                step_id="step_1",
                sequence=1,
                reasoning_type="understand_architecture",
                reasoning="Test",
                hypothesis="Test hypothesis",
                falsifiable_via="test signal",
                confidence=0.5,
            ),
        ],
        created_at="2025-01-01",
    )
    state = _make_minimal_state(planning_trace=trace)
    # Remove verification result
    state.pop("verification_result", None)
    result = build_plan_feedback_node(state)

    report = result["plan_feedback"]
    assert report.untested_count == 1
    assert report.items[0].verdict == "untested"


def test_plan_feedback_node_uses_trace_invalidated_steps():
    """Steps in planning_trace.invalidated_steps are also marked falsified."""
    from paper2repo_agent.graphs.execute.nodes import build_plan_feedback_node

    trace = PlanningTrace(
        id="pt_1",
        run_id="run_test",
        plan_id="plan_1",
        steps=[
            PlanningReasoningStep(
                step_id="step_1",
                sequence=1,
                reasoning_type="understand_architecture",
                reasoning="Test",
                hypothesis="Test H1",
                falsifiable_via="test F1",
                confidence=0.5,
            ),
        ],
        invalidated_steps=["step_1"],
        created_at="2025-01-01",
    )
    state = _make_minimal_state(planning_trace=trace)
    result = build_plan_feedback_node(state)

    report = result["plan_feedback"]
    assert report.falsified_count == 1


# ═════════════════════════════════════════════════════════════════════════
# Plan rollback gate node
# ═════════════════════════════════════════════════════════════════════════


def test_rollback_gate_no_feedback():
    """Without plan_feedback in state, rollback is not triggered."""
    from paper2repo_agent.graphs.execute.nodes import plan_rollback_gate_node

    state = _make_minimal_state()
    result = plan_rollback_gate_node(state)
    assert result["rollback_triggered"] is False


def test_rollback_gate_feedback_no_falsified():
    """When plan_feedback has no falsified items, no rollback."""
    from paper2repo_agent.graphs.execute.nodes import plan_rollback_gate_node

    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_test",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        confirmed_count=3,
        falsified_count=0,
        rollback_triggered=False,
        summary="All good.",
        created_at="2025-01-01",
    )
    state = _make_minimal_state(plan_feedback=feedback)
    result = plan_rollback_gate_node(state)
    assert result["rollback_triggered"] is False


def test_rollback_gate_max_rollback_limit():
    """When rollback_count reaches _MAX_ROLLBACK, no further rollback."""
    from paper2repo_agent.graphs.execute.nodes import plan_rollback_gate_node

    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_test",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        falsified_count=1,
        rollback_triggered=True,
        summary="Still broken.",
        created_at="2025-01-01",
    )
    state = _make_minimal_state(plan_feedback=feedback, rollback_count=2)
    result = plan_rollback_gate_node(state)
    assert result["rollback_triggered"] is False


def test_rollback_gate_triggers_when_feedback_has_falsified():
    """When feedback has falsified items and revision succeeds, trigger rollback."""
    from paper2repo_agent.graphs.execute.nodes import plan_rollback_gate_node

    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_test",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        items=[
            PlanFeedbackItem(
                planning_step_id="step_2",
                hypothesis="S_init = eye(num_classes)",
                falsifiable_via="smoke",
                verdict="falsified",
            ),
        ],
        falsified_count=1,
        rollback_triggered=True,
        summary="1 hypothesis falsified.",
        created_at="2025-01-01",
    )
    state = _make_minimal_state(
        plan_feedback=feedback,
        planning_trace=_sample_planning_trace(),
        rollback_count=0,
    )
    with patch(
        "paper2repo_agent.llm.revise_planning_trace",
        return_value=_sample_plan_trace_revision(),
    ):
        result = plan_rollback_gate_node(state)

    assert result["rollback_triggered"] is True
    assert result["rollback_count"] == 1
    assert result["planning_trace"].id == "pt_1_revised"
    assert result["planning_trace"].invalidated_steps == []
    assert result["plan_trace_revisions"][0].id == "ptr_1"


def test_rollback_gate_skips_when_revision_unavailable():
    """A falsified feedback report should not loop if no revised trace is available."""
    from paper2repo_agent.graphs.execute.nodes import plan_rollback_gate_node

    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_test",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        items=[
            PlanFeedbackItem(
                planning_step_id="step_2",
                hypothesis="S_init = eye(num_classes)",
                falsifiable_via="smoke",
                verdict="falsified",
            ),
        ],
        falsified_count=1,
        rollback_triggered=True,
        summary="1 hypothesis falsified.",
        created_at="2025-01-01",
    )
    state = _make_minimal_state(
        plan_feedback=feedback,
        planning_trace=_sample_planning_trace(),
        rollback_count=0,
    )
    with patch("paper2repo_agent.llm.revise_planning_trace", return_value=None):
        result = plan_rollback_gate_node(state)

    assert result["rollback_triggered"] is False
    assert "rollback_count" not in result


def test_rollback_gate_hitl_master_kill_switch_off():
    """When HITL interactive=False, rollback can proceed without consultation."""
    from paper2repo_agent.graphs.execute.nodes import plan_rollback_gate_node
    from paper2repo_agent.schemas import HITLGateConfig

    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_test",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        items=[
            PlanFeedbackItem(
                planning_step_id="step_2",
                hypothesis="S_init = eye(num_classes)",
                falsifiable_via="smoke",
                verdict="falsified",
            ),
        ],
        falsified_count=1,
        rollback_triggered=True,
        summary="1 falsified.",
        created_at="2025-01-01",
    )
    hitl = HITLGateConfig(
        interactive=False,
        before_plan_invalid=True,  # Would trigger if interactive=True
    )
    state = _make_minimal_state(
        plan_feedback=feedback,
        planning_trace=_sample_planning_trace(),
        hitl_gate_config=hitl,
    )
    with patch(
        "paper2repo_agent.llm.revise_planning_trace",
        return_value=_sample_plan_trace_revision(),
    ):
        result = plan_rollback_gate_node(state)

    assert result["rollback_triggered"] is True


def test_rollback_gate_hitl_rejects_rollback():
    """When HITL consult_fn returns reject, rollback is stopped."""
    from paper2repo_agent.graphs.execute.nodes import plan_rollback_gate_node
    from paper2repo_agent.schemas import HITLGateConfig

    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_test",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        falsified_count=1,
        rollback_triggered=True,
        summary="1 falsified.",
        created_at="2025-01-01",
    )
    hitl = HITLGateConfig(
        interactive=True,
        before_plan_invalid=True,
    )
    consult_fn = MagicMock(return_value={"action": "reject"})
    state = _make_minimal_state(
        plan_feedback=feedback,
        hitl_gate_config=hitl,
        hitl_consult_fn=consult_fn,
    )
    result = plan_rollback_gate_node(state)
    assert result["rollback_triggered"] is False
    consult_fn.assert_called_once()


# ═════════════════════════════════════════════════════════════════════════
# Planning trace revision
# ═════════════════════════════════════════════════════════════════════════


def test_revise_planning_trace_import_available():
    """revise_planning_trace is importable from llm module."""
    from paper2repo_agent.llm import revise_planning_trace

    assert callable(revise_planning_trace)


def test_revise_planning_trace_returns_none_for_empty_trace():
    """Empty PlanningTrace returns None (nothing to revise)."""
    from paper2repo_agent.llm import revise_planning_trace

    trace = PlanningTrace(
        id="pt_1",
        run_id="run_1",
        plan_id="plan_1",
        steps=[],
        created_at="2025-01-01",
    )
    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_1",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        falsified_count=1,
        rollback_triggered=True,
        summary="test",
        created_at="2025-01-01",
    )
    result = revise_planning_trace(trace, feedback, [])
    assert result is None


def test_revise_planning_trace_returns_none_when_no_falsified():
    """When feedback has no falsified items, no revision needed."""
    from paper2repo_agent.llm import revise_planning_trace

    trace = PlanningTrace(
        id="pt_1",
        run_id="run_1",
        plan_id="plan_1",
        steps=[
            PlanningReasoningStep(
                step_id="step_1",
                sequence=1,
                reasoning_type="understand_architecture",
                reasoning="Test",
                hypothesis="H1",
                falsifiable_via="F1",
                confidence=0.9,
            ),
        ],
        created_at="2025-01-01",
    )
    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_1",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        items=[
            PlanFeedbackItem(
                planning_step_id="step_1",
                hypothesis="H1",
                falsifiable_via="F1",
                verdict="confirmed",
            ),
        ],
        falsified_count=0,
        rollback_triggered=False,
        summary="test",
        created_at="2025-01-01",
    )
    result = revise_planning_trace(trace, feedback, [])
    assert result is None


def test_revise_planning_trace_finds_first_falsified():
    """When steps are falsified, revise_planning_trace produces a revision."""
    from paper2repo_agent.llm import revise_planning_trace

    trace = PlanningTrace(
        id="pt_1",
        run_id="run_1",
        plan_id="plan_1",
        steps=[
            PlanningReasoningStep(
                step_id="step_1",
                sequence=1,
                reasoning_type="understand_architecture",
                reasoning="Good step",
                hypothesis="H1",
                falsifiable_via="F1",
                confidence=0.9,
            ),
            PlanningReasoningStep(
                step_id="step_2",
                sequence=2,
                reasoning_type="design_tensor_contract",
                reasoning="Bad step",
                hypothesis="H2_wrong",
                falsifiable_via="run_smoke_forward shape check",
                confidence=0.7,
            ),
            PlanningReasoningStep(
                step_id="step_3",
                sequence=3,
                reasoning_type="propose_file_structure",
                reasoning="Depends on step_2",
                hypothesis="H3",
                falsifiable_via="F3",
                confidence=0.5,
            ),
        ],
        created_at="2025-01-01",
    )
    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_1",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        items=[
            PlanFeedbackItem(
                planning_step_id="step_1",
                hypothesis="H1",
                falsifiable_via="F1",
                verdict="confirmed",
            ),
            PlanFeedbackItem(
                planning_step_id="step_2",
                hypothesis="H2_wrong",
                falsifiable_via="run_smoke_forward shape check",
                verdict="falsified",
                actual_result="Shape (C,C) vs (B,B) mismatch",
            ),
            PlanFeedbackItem(
                planning_step_id="step_3",
                hypothesis="H3",
                falsifiable_via="F3",
                verdict="untested",
            ),
        ],
        falsified_count=1,
        rollback_triggered=True,
        summary="1 falsified",
        created_at="2025-01-01",
    )

    # Mock the LLM response + Settings
    with (
        patch("paper2repo_agent.llm.build_model") as mock_build,
        patch("paper2repo_agent.llm.Settings.load") as mock_settings_load,
    ):
        mock_settings_load.return_value = MagicMock(model_name="test-model")
        mock_llm = MagicMock()
        mock_llm.invoke.return_value = MagicMock(
            content=json.dumps(
                [
                    {
                        "step_id": "step_2",
                        "sequence": 2,
                        "reasoning_type": "design_tensor_contract",
                        "reasoning": "S must be (B, C) for per-sample Jaccard",
                        "hypothesis": "S_init produces (B, C) per-sample scores",
                        "falsifiable_via": "run_smoke_forward confirms (B, C) output",
                        "confidence": 0.9,
                        "alternatives_considered": ["(C, C) — falsified"],
                    },
                    {
                        "step_id": "step_3",
                        "sequence": 3,
                        "reasoning_type": "propose_file_structure",
                        "reasoning": "No change needed",
                        "hypothesis": "H3 unchanged",
                        "falsifiable_via": "F3",
                        "confidence": 0.5,
                    },
                ]
            ),
        )
        mock_build.return_value = mock_llm

        revision = revise_planning_trace(trace, feedback, [])

    assert revision is not None
    assert isinstance(revision, PlanTraceRevision)
    assert revision.trigger_step_ids == ["step_2"]
    assert len(revision.steps_before) == 2  # steps 2 and 3
    assert len(revision.steps_after) == 2
    # step_2 hypothesis should have changed
    assert revision.steps_after[0].hypothesis != revision.steps_before[0].hypothesis


def test_revise_planning_trace_handles_llm_error():
    """When LLM call fails, returns None gracefully."""
    from paper2repo_agent.llm import revise_planning_trace

    trace = PlanningTrace(
        id="pt_1",
        run_id="run_1",
        plan_id="plan_1",
        steps=[
            PlanningReasoningStep(
                step_id="step_1",
                sequence=1,
                reasoning_type="understand_architecture",
                reasoning="Test",
                hypothesis="H1",
                falsifiable_via="F1",
                confidence=0.5,
            ),
        ],
        created_at="2025-01-01",
    )
    feedback = PlanFeedbackReport(
        id="pfr_1",
        run_id="run_1",
        plan_id="plan_1",
        planning_trace_id="pt_1",
        items=[
            PlanFeedbackItem(
                planning_step_id="step_1",
                hypothesis="H1",
                falsifiable_via="F1",
                verdict="falsified",
            ),
        ],
        falsified_count=1,
        rollback_triggered=True,
        summary="test",
        created_at="2025-01-01",
    )

    with (
        patch("paper2repo_agent.llm.build_model") as mock_build,
        patch("paper2repo_agent.llm.Settings.load") as mock_settings_load,
    ):
        mock_settings_load.return_value = MagicMock(model_name="test-model")
        mock_llm = MagicMock()
        mock_llm.invoke.side_effect = RuntimeError("LLM connection lost")
        mock_build.return_value = mock_llm

        revision = revise_planning_trace(trace, feedback, [])
    assert revision is None


# ═════════════════════════════════════════════════════════════════════════
# P3: Workflow graph integration
# ═════════════════════════════════════════════════════════════════════════


def test_p3_nodes_in_workflow_graph():
    """The execute workflow graph includes build_plan_feedback and plan_rollback_gate."""
    from paper2repo_agent.graphs.execute.workflow import build_execute_graph

    graph = build_execute_graph()
    nodes = graph.get_graph().nodes
    node_names = {node for node in nodes}

    assert "build_plan_feedback" in node_names
    assert "plan_rollback_gate" in node_names


def test_workflow_conditional_edges_present():
    """The workflow has conditional edges from plan_rollback_gate."""
    from paper2repo_agent.graphs.execute.workflow import build_execute_graph

    graph = build_execute_graph()
    graph_spec = graph.get_graph()

    # plan_rollback_gate should have edges to both:
    #   - build_file_generation_tasks (rollback path)
    #   - write_final_report (forward path)
    edges = list(graph_spec.edges)
    edge_sources = {e[0] for e in edges}
    edge_targets = {e[1] for e in edges}

    assert "plan_rollback_gate" in edge_sources
    # At least one of the conditional targets should be present
    assert "build_file_generation_tasks" in edge_targets or any(
        "build_file_generation_tasks" in str(e) for e in edges
    )


def test_rollback_route_function():
    """_route_after_rollback_gate returns correct target based on state."""
    from paper2repo_agent.graphs.execute.workflow import _route_after_rollback_gate

    state_rollback = {"rollback_triggered": True}
    state_no_rollback = {"rollback_triggered": False}
    state_missing = {}

    assert _route_after_rollback_gate(state_rollback) == "build_file_generation_tasks"
    assert _route_after_rollback_gate(state_no_rollback) == "write_final_report"
    assert _route_after_rollback_gate(state_missing) == "write_final_report"


# ═════════════════════════════════════════════════════════════════════════
# P3: Execution context loads planning_trace
# ═════════════════════════════════════════════════════════════════════════


def test_load_execution_context_includes_planning_trace_key():
    """_tool_load_execution_context returns planning_trace key in result."""
    import tempfile
    from paper2repo_agent.graphs.execute.tools import ToolExecutor

    # Create a minimal run directory
    with tempfile.TemporaryDirectory() as tmpdir:
        run_dir = Path(tmpdir) / "run"
        run_dir.mkdir()
        (run_dir / "planning").mkdir()
        (run_dir / "approval").mkdir()
        (run_dir / "execution").mkdir()

        # Write minimal required files
        from paper2repo_agent.utils import write_json
        from paper2repo_agent.schemas import (
            ApprovedPlan,
            DataStrategy,
            DependencyStrategy,
            HumanDecisions,
            ImplementationStrategy,
            ReproductionObjective,
            ReproductionPlan,
            RunManifest,
            SourcePolicy,
            TargetScope,
            UserConstraints,
            VerificationStrategy,
        )

        manifest = RunManifest(
            run_id="test_run",
            created_at="2025-01-01",
            updated_at="2025-01-01",
            status="executing",
        )
        write_json(run_dir / "run_manifest.json", manifest.model_dump(mode="json"))

        plan = ReproductionPlan(
            id="plan_1",
            paper_title="Test",
            created_at="2025-01-01",
            objective=ReproductionObjective(description="Test goal"),
            user_constraints=UserConstraints(),
            scope=TargetScope(),
            paper_spec_ref="planning/extracted_spec.json",
            implementation_strategy=ImplementationStrategy(
                repo_name="test_repo",
                package_name="test",
                architecture_summary="Test arch",
            ),
            dependency_strategy=DependencyStrategy(),
            data_strategy=DataStrategy(),
            verification_strategy=VerificationStrategy(),
        )
        approved = ApprovedPlan(
            plan=plan,
            human_decisions=HumanDecisions(review_id="r1"),
            frozen_at="2025-01-01",
            plan_hash="abc",
            execution_allowed=True,
        )
        write_json(run_dir / "approval" / "approved_plan.json", approved.model_dump(mode="json"))
        write_json(run_dir / "planning" / "extracted_spec.json", {"id": "spec_1"})
        write_json(run_dir / "planning" / "validation_result.json", {"id": "val_1"})
        # Write empty JSONL files
        (run_dir / "planning" / "evidence_store.jsonl").write_text("", encoding="utf-8")
        (run_dir / "planning" / "evidence_refs.jsonl").write_text("", encoding="utf-8")
        write_json(
            run_dir / "planning" / "source_policy.json",
            SourcePolicy(id="sp").model_dump(mode="json"),
        )

        state = {"tool_calls": [], "paths": type(sys.modules[__name__])}
        executor = ToolExecutor(state)
        result = executor.call("load_execution_context", run_dir=str(run_dir))

        # planning_trace key should be present (even if None)
        assert "planning_trace" in result


# ═════════════════════════════════════════════════════════════════════════
# Feature flag: plan feedback always runs (no env var gate)
# ═════════════════════════════════════════════════════════════════════════


def test_plan_feedback_runs_in_workflow_without_env_var():
    """Plan feedback is always active — it's a structural node, not LLM-gated."""
    from paper2repo_agent.graphs.execute.nodes import build_plan_feedback_node

    state = _make_minimal_state()
    result = build_plan_feedback_node(state)
    assert "plan_feedback" in result
    assert result["plan_feedback"] is not None


def test_run_manifest_includes_plan_feedback_ref():
    """RunManifest schema accepts plan_feedback_ref and plan_trace_revision_ref."""
    from paper2repo_agent.schemas import RunManifest

    manifest = RunManifest(
        run_id="test",
        created_at="2025-01-01",
        updated_at="2025-01-01",
        status="executed",
        plan_feedback_ref="execution/plan_feedback.json",
        plan_trace_revision_ref="execution/plan_trace_revision.json",
    )
    assert manifest.plan_feedback_ref == "execution/plan_feedback.json"
    assert manifest.plan_trace_revision_ref == "execution/plan_trace_revision.json"


def test_write_manifest_hashes_plan_feedback_artifacts(tmp_path):
    """P3 artifacts should be included in artifact_hashes when present."""
    from paper2repo_agent.artifacts import write_manifest

    paths = RunPaths(tmp_path / "run")
    paths.execution_dir.mkdir(parents=True)
    paths.plan_feedback.write_text("{}", encoding="utf-8")
    paths.plan_trace_revision.write_text("[]", encoding="utf-8")

    manifest = write_manifest(paths=paths, run_id="run_test", status="executed")

    assert "execution/plan_feedback.json" in manifest.artifact_hashes
    assert "execution/plan_trace_revision.json" in manifest.artifact_hashes
