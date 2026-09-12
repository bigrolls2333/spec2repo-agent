from __future__ import annotations

from typing import Any, TypedDict

from paper2repo_agent.artifacts import RunPaths
from paper2repo_agent.schemas import (
    AgentTrace,
    ApprovedPlan,
    CodebaseDesign,
    CodingPlanCritique,
    CommandResult,
    CommandSpec,
    ContractVerificationReport,
    ContextReport,
    CostReport,
    EvidenceStore,
    ExecutionCodingPlan,
    ExecutionPolicy,
    ExecutionResult,
    ExecutionToolSpec,
    FailureDigest,
    FileGenerationDAG,
    FileGenerationTask,
    FinalReport,
    GeneratedFile,
    HITLGateConfig,
    LLMCallRecord,
    RetrievalReport,
    PaperSpec,
    PlanningTrace,
    ProvenanceRecord,
    PatchPlan,
    PlanFeedbackReport,
    PlanTraceRevision,
    RepairAttempt,
    RepairRecord,
    RepairTask,
    RunManifest,
    SourcePolicy,
    SpecValidationResult,
    ToolCallRecord,
    VerificationResult,
)


class ExecuteGraphState(TypedDict, total=False):
    run_dir: str
    repo_dir: str
    verbose: bool
    progress_callback: Any
    paths: RunPaths
    manifest: RunManifest
    approved_plan: ApprovedPlan
    paper_spec: PaperSpec
    validation_result: SpecValidationResult
    evidence_store: EvidenceStore
    source_policy: SourcePolicy
    execution_policy: ExecutionPolicy
    planning_trace: PlanningTrace
    tool_specs: dict[str, ExecutionToolSpec]
    execution_phase: str
    codebase_design: CodebaseDesign
    coding_plan: ExecutionCodingPlan
    critique_coding_plan: CodingPlanCritique
    file_tasks: list[FileGenerationTask]
    file_generation_dag: FileGenerationDAG
    generated_files: list[GeneratedFile]
    retrieval_reports: list[RetrievalReport]
    repair_context_reports: list[RetrievalReport]
    context_reports: list[ContextReport]
    llm_calls: list[LLMCallRecord]
    cost_report: CostReport
    provenance_records: list[ProvenanceRecord]
    command_specs: list[CommandSpec]
    tool_calls: list[ToolCallRecord]
    command_results: list[CommandResult]
    verification_result: VerificationResult
    static_verification_result: VerificationResult
    contract_verification_result: VerificationResult
    contract_verification_report: ContractVerificationReport
    failure_digest: FailureDigest
    repair_tasks: list[RepairTask]
    patch_plan: list[PatchPlan]
    repair_records: list[RepairRecord]
    repair_history: list[RepairAttempt]
    agent_traces: list[AgentTrace]
    hitl_gate_config: HITLGateConfig
    hitl_consult_fn: Any
    execution_result: ExecutionResult
    final_report: FinalReport
    execution_status: str
    execution_summary: str
    plan_feedback: PlanFeedbackReport
    plan_trace_revisions: list[PlanTraceRevision]
    rollback_count: int
    rollback_triggered: bool


def progress(state: ExecuteGraphState, message: str) -> None:
    if state.get("verbose", True):
        print(f"[paper2repo] {message}", flush=True)
    callback = state.get("progress_callback")
    if callback is not None:
        callback(message)
        return

    # Runtime callbacks are process-local capabilities. Keeping them outside
    # graph state makes every checkpoint serializable and safe to resume.
    from paper2repo_agent.runtime.context import emit_progress

    emit_progress(message)
