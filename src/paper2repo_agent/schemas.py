from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.2.0"


class AgentModel(BaseModel):
    model_config = ConfigDict(extra="ignore")


SourceType = Literal["paper", "appendix", "table", "user", "inferred", "default", "missing"]


class Evidence(AgentModel):
    id: str
    run_id: str
    paper_id: str
    source: Literal["paper", "appendix", "table", "figure", "equation", "user", "inferred"]
    section: str | None = None
    page: int | None = None
    paragraph_id: str | None = None
    chunk_id: str | None = None
    span_start: int | None = None
    span_end: int | None = None
    text: str
    confidence: float = 1.0


class EvidenceRef(AgentModel):
    id: str
    evidence_id: str
    artifact_type: Literal["paper_spec", "validation_result", "plan", "human_review", "report"]
    artifact_id: str
    field_path: str
    relation: Literal["supports", "explains", "assumes", "contradicts", "summarizes"] = "supports"
    created_by: Literal["parser", "llm", "validator", "user"] = "parser"


class EvidenceStore(AgentModel):
    run_id: str
    paper_id: str
    schema_version: str = SCHEMA_VERSION
    items: list[Evidence] = Field(default_factory=list)
    refs: list[EvidenceRef] = Field(default_factory=list)
    storage_backend: Literal["jsonl", "sqlite", "mysql", "postgres"] = "jsonl"
    database_uri: str | None = None


class ExtractedField(AgentModel):
    value: Any | None = None
    value_type: str = "unknown"
    source_type: SourceType = "missing"
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0
    notes: str | None = None


def missing_field(value_type: str = "unknown", notes: str | None = None) -> ExtractedField:
    return ExtractedField(value=None, value_type=value_type, source_type="missing", notes=notes)


class Assumption(AgentModel):
    id: str
    field_path: str
    assumed_value: Any
    reason: str
    risk: Literal["low", "medium", "high"] = "medium"
    requires_confirmation: bool = True
    alternatives: list[Any] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class MissingDetail(AgentModel):
    id: str
    field_path: str
    description: str
    why_required: str
    severity: Literal["blocking", "important", "optional"] = "important"
    suggested_resolution: str | None = None
    blocks_execution: bool = False
    evidence_ids: list[str] = Field(default_factory=list)


class PaperInput(AgentModel):
    source_type: Literal["pdf", "arxiv", "latex", "markdown", "text"]
    source_path: str | None = None
    arxiv_id: str | None = None
    raw_text_path: str | None = None
    output_dir: str
    user_constraints: "UserConstraints"
    source_policy: "SourcePolicy"


class PaperSection(AgentModel):
    id: str
    title: str
    level: int = 1
    page_start: int | None = None
    page_end: int | None = None
    text: str
    parent_id: str | None = None


class PaperTable(AgentModel):
    id: str
    caption: str | None = None
    page: int | None = None
    markdown: str | None = None
    raw_text: str = ""
    section_id: str | None = None


class PaperEquation(AgentModel):
    id: str
    latex: str | None = None
    surrounding_text: str = ""
    page: int | None = None
    section_id: str | None = None


class PaperFigure(AgentModel):
    id: str
    caption: str | None = None
    page: int | None = None
    section_id: str | None = None
    image_path: str | None = None


class ParsedPaper(AgentModel):
    paper_id: str
    title: str | None = None
    authors: list[str] = Field(default_factory=list)
    abstract: str | None = None
    raw_text: str
    sections: list[PaperSection] = Field(default_factory=list)
    tables: list[PaperTable] = Field(default_factory=list)
    equations: list[PaperEquation] = Field(default_factory=list)
    figures: list[PaperFigure] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class PaperMetadataSpec(AgentModel):
    title: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    authors: ExtractedField = Field(default_factory=lambda: missing_field("list[str]"))
    year: ExtractedField = Field(default_factory=lambda: missing_field("int"))
    venue: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    paper_url: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    code_url: ExtractedField = Field(default_factory=lambda: missing_field("str"))


class TaskSpec(AgentModel):
    domain: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    task_type: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    input_modality: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    output_modality: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    problem_formulation: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    supported_by_v1: bool = True


class TensorSpec(AgentModel):
    name: str
    shape: list[str | int | None] = Field(default_factory=list)
    dtype: str | None = None
    description: str = ""
    source_type: Literal["paper", "inferred", "user", "missing"] = "missing"
    evidence_ids: list[str] = Field(default_factory=list)


class ModelComponent(AgentModel):
    name: str
    component_type: str = "custom"
    description: str = ""
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    hyperparameters: dict[str, ExtractedField] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class LossSpec(AgentModel):
    name: str
    loss_type: str = "custom"
    formula: str | None = None
    weight: ExtractedField = Field(
        default_factory=lambda: ExtractedField(
            value=1.0,
            value_type="float",
            source_type="default",
            confidence=0.5,
        )
    )
    evidence_ids: list[str] = Field(default_factory=list)


class EquationSpec(AgentModel):
    id: str
    latex: str
    meaning: str = ""
    implementation_relevance: Literal["required", "helpful", "not_needed"] = "helpful"
    evidence_ids: list[str] = Field(default_factory=list)


class ImplementationNote(AgentModel):
    id: str
    target: str
    note: str
    source_type: SourceType = "paper"
    priority: Literal["low", "medium", "high"] = "medium"
    evidence_ids: list[str] = Field(default_factory=list)


class ModelSpec(AgentModel):
    architecture_name: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    architecture_family: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    framework_hint: ExtractedField = Field(
        default_factory=lambda: ExtractedField(
            value="pytorch",
            value_type="str",
            source_type="default",
            confidence=0.5,
        )
    )
    components: list[ModelComponent] = Field(default_factory=list)
    inputs: list[TensorSpec] = Field(default_factory=list)
    outputs: list[TensorSpec] = Field(default_factory=list)
    losses: list[LossSpec] = Field(default_factory=list)
    equations: list[EquationSpec] = Field(default_factory=list)
    initialization: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    implementation_hints: list[ImplementationNote] = Field(default_factory=list)


class PreprocessingStep(AgentModel):
    name: str
    description: str = ""
    parameters: dict[str, ExtractedField] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class DatasetSpec(AgentModel):
    name: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    task_role: Literal["train", "validation", "test", "benchmark", "unknown"] = "unknown"
    modality: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    input_shape: ExtractedField = Field(default_factory=lambda: missing_field("list"))
    output_shape: ExtractedField = Field(default_factory=lambda: missing_field("list"))
    num_classes: ExtractedField = Field(default_factory=lambda: missing_field("int"))
    preprocessing: list[PreprocessingStep] = Field(default_factory=list)
    splits: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    availability: Literal["public", "private", "unknown", "synthetic_fallback"] = "unknown"
    download_url: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    license: ExtractedField = Field(default_factory=lambda: missing_field("str"))


class TrainingSpec(AgentModel):
    optimizer: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    learning_rate: ExtractedField = Field(default_factory=lambda: missing_field("float"))
    batch_size: ExtractedField = Field(default_factory=lambda: missing_field("int"))
    epochs: ExtractedField = Field(default_factory=lambda: missing_field("int"))
    max_steps: ExtractedField = Field(default_factory=lambda: missing_field("int"))
    scheduler: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    warmup_steps: ExtractedField = Field(default_factory=lambda: missing_field("int"))
    weight_decay: ExtractedField = Field(default_factory=lambda: missing_field("float"))
    dropout: ExtractedField = Field(default_factory=lambda: missing_field("float"))
    gradient_clip: ExtractedField = Field(default_factory=lambda: missing_field("float"))
    seed: ExtractedField = Field(default_factory=lambda: missing_field("int"))
    precision: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    hardware: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    distributed: ExtractedField = Field(default_factory=lambda: missing_field("bool"))
    augmentation: list[PreprocessingStep] = Field(default_factory=list)


class MetricSpec(AgentModel):
    name: str
    metric_type: str = "custom"
    direction: Literal["higher_is_better", "lower_is_better"] = "higher_is_better"
    evidence_ids: list[str] = Field(default_factory=list)


class BaselineSpec(AgentModel):
    name: str
    description: str = ""
    reported_result: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class TargetResult(AgentModel):
    table_id: str | None = None
    metric_name: str
    value: float | str
    dataset: str | None = None
    setting: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class EvaluationSpec(AgentModel):
    metrics: list[MetricSpec] = Field(default_factory=list)
    primary_metric: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    evaluation_protocol: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    baselines: list[BaselineSpec] = Field(default_factory=list)
    target_results: list[TargetResult] = Field(default_factory=list)


class PackageRequirement(AgentModel):
    name: str
    version_spec: str | None = None
    reason: str = ""
    source_type: Literal["paper", "inferred", "default", "user"] = "default"
    optional: bool = False


class SystemRequirement(AgentModel):
    name: str
    requirement_type: str = "software"
    description: str = ""
    source_type: SourceType = "paper"
    evidence_ids: list[str] = Field(default_factory=list)


class DependencySpec(AgentModel):
    framework: ExtractedField = Field(
        default_factory=lambda: ExtractedField(
            value="pytorch",
            value_type="str",
            source_type="default",
            confidence=0.5,
        )
    )
    framework_version: ExtractedField = Field(default_factory=lambda: missing_field("str"))
    python_version: ExtractedField = Field(
        default_factory=lambda: ExtractedField(
            value="3.10+",
            value_type="str",
            source_type="default",
            confidence=0.6,
        )
    )
    packages: list[PackageRequirement] = Field(default_factory=list)
    system_requirements: list[SystemRequirement] = Field(default_factory=list)


class ReproductionClaim(AgentModel):
    id: str
    description: str
    claim_type: Literal[
        "metric",
        "ablation",
        "architecture",
        "speed",
        "memory",
        "qualitative",
    ] = "architecture"
    required_data: list[str] = Field(default_factory=list)
    required_compute: str | None = None
    feasibility: Literal["high", "medium", "low", "unsupported"] = "medium"
    evidence_ids: list[str] = Field(default_factory=list)


class ExtractionGap(AgentModel):
    id: str
    field_path: str
    description: str
    candidate_resolution: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class PaperSpec(AgentModel):
    id: str
    metadata: PaperMetadataSpec = Field(default_factory=PaperMetadataSpec)
    task: TaskSpec = Field(default_factory=TaskSpec)
    model: ModelSpec = Field(default_factory=ModelSpec)
    datasets: list[DatasetSpec] = Field(default_factory=list)
    training: TrainingSpec = Field(default_factory=TrainingSpec)
    evaluation: EvaluationSpec = Field(default_factory=EvaluationSpec)
    dependencies: DependencySpec = Field(default_factory=DependencySpec)
    implementation_notes: list[ImplementationNote] = Field(default_factory=list)
    claims_to_reproduce: list[ReproductionClaim] = Field(default_factory=list)
    extraction_gaps: list[ExtractionGap] = Field(default_factory=list)
    source_policy_ref: str | None = None


class UnsupportedFeature(AgentModel):
    feature: str
    reason: str
    workaround: str | None = None
    severity: Literal["blocking", "degrades_fidelity", "minor"] = "degrades_fidelity"
    evidence_ids: list[str] = Field(default_factory=list)


class ConfirmationItem(AgentModel):
    id: str
    field_path: str
    question: str
    current_value: Any | None = None
    suggested_value: Any | None = None
    reason: str
    severity: Literal["required", "recommended", "optional"] = "recommended"
    options: list[Any] | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    blocks_execution_if_unanswered: bool = False


class ConfirmationProposal(AgentModel):
    field_path: str
    question: str
    current_value: Any | None = None
    suggested_value: Any | None = None
    options: list[Any] = Field(default_factory=list)
    reason: str
    risk: Literal["low", "medium", "high"] = "medium"
    severity: Literal["required", "recommended", "optional"] = "recommended"
    blocks_execution_if_unanswered: bool = False
    decision_effect: str
    applies_to: Literal[
        "user_constraints",
        "paper_spec",
        "reproduction_plan",
        "execution_policy",
        "custom",
    ] = "reproduction_plan"
    evidence_ids: list[str] = Field(default_factory=list)
    confidence: float = 0.0


class ConfirmationProposalSet(AgentModel):
    proposals: list[ConfirmationProposal] = Field(default_factory=list)


class ValidationIssue(AgentModel):
    id: str
    severity: Literal["error", "warning", "info"] = "warning"
    field_path: str | None = None
    message: str
    suggested_action: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class SpecValidationResult(AgentModel):
    id: str
    supported: bool = True
    support_level: Literal["full", "partial", "scaffold_only", "unsupported"] = "partial"
    blocking_issues: list[ValidationIssue] = Field(default_factory=list)
    warnings: list[ValidationIssue] = Field(default_factory=list)
    missing_details: list[MissingDetail] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    unsupported_features: list[UnsupportedFeature] = Field(default_factory=list)
    required_confirmations: list[ConfirmationItem] = Field(default_factory=list)


class UserConstraints(AgentModel):
    initial_request: str | None = None
    reproduction_goal: Literal[
        "spec_only",
        "scaffold_only",
        "smoke_test",
        "small_scale_result",
        "full_metric_attempt",
    ] = "smoke_test"
    target_framework: Literal["pytorch"] = "pytorch"
    compute_target: Literal["cpu_smoke", "single_gpu", "multi_gpu"] = "cpu_smoke"
    gpu_model: str | None = None
    gpu_memory_gb: int | None = None
    max_runtime_minutes: int = 10
    os: Literal["windows", "linux", "macos", "unknown"] = "unknown"
    python_version: str = "3.10"
    package_manager: Literal["pip", "uv", "poetry", "conda"] = "pip"
    allow_network: bool = False
    allow_large_downloads: bool = False
    dataset_policy: Literal[
        "synthetic_only",
        "user_provided",
        "download_allowed",
    ] = "synthetic_only"
    dataset_paths: dict[str, str] = Field(default_factory=dict)
    allowed_dependencies: list[str] = Field(default_factory=list)
    blocked_dependencies: list[str] = Field(default_factory=list)


class SourcePolicy(AgentModel):
    id: str
    paper_url: str | None = None
    official_code_url: str | None = None
    paper_license: str | None = None
    official_code_license: str | None = None
    dataset_license_notes: str | None = None
    code_usage_mode: Literal[
        "metadata_only",
        "reference_allowed",
        "user_provided",
    ] = "metadata_only"
    generated_code_license: str = "user-owned"
    citation_policy: str = "cite original paper in generated reports"


class ReproductionObjective(AgentModel):
    goal: Literal[
        "extract_spec",
        "generate_scaffold",
        "run_smoke_test",
        "small_scale_reproduction",
        "full_reproduction_attempt",
    ] = "run_smoke_test"
    description: str
    success_criteria: list[str] = Field(default_factory=list)
    non_goals: list[str] = Field(default_factory=list)


class TargetScope(AgentModel):
    framework: Literal["pytorch"] = "pytorch"
    device_target: Literal["cpu", "cuda", "mps"] = "cpu"
    dataset_mode: Literal["synthetic", "user_provided", "download"] = "synthetic"
    metric_reproduction: bool = False
    max_training_minutes: int = 10


class FilePlan(AgentModel):
    path: str
    purpose: str
    source: Literal["template", "llm_generated", "programmatic", "copied_from_user"] = "template"
    depends_on: list[str] = Field(default_factory=list)
    must_export: list[str] = Field(default_factory=list)


class ImplementationStrategy(AgentModel):
    repo_name: str
    package_name: str
    architecture_summary: str
    files: list[FilePlan] = Field(default_factory=list)
    generation_policy: Literal["template_first", "llm_first", "hybrid"] = "template_first"
    coding_style: Literal["minimal", "research", "production_like"] = "research"


class DependencyStrategy(AgentModel):
    package_manager: Literal["pip", "uv", "poetry", "conda"] = "pip"
    python_version: str = "3.10"
    runtime_packages: list[PackageRequirement] = Field(default_factory=list)
    dev_packages: list[PackageRequirement] = Field(default_factory=list)
    install_command: str = "pip install -e ."
    lockfile_expected: bool = False


class SyntheticDataSpec(AgentModel):
    input_shapes: list[TensorSpec] = Field(default_factory=list)
    num_samples: int = 8
    num_classes: int | None = None
    batch_size: int = 2
    task_type: str = "classification"


class DataStrategy(AgentModel):
    mode: Literal["synthetic", "user_provided", "download"] = "synthetic"
    datasets: list[str] = Field(default_factory=list)
    synthetic_data_spec: SyntheticDataSpec | None = None
    user_data_requirements: list[str] = Field(default_factory=list)
    download_steps: list[str] = Field(default_factory=list)


class VerificationCheck(AgentModel):
    id: str
    check_type: Literal["file_exists", "import", "pytest", "train_smoke", "lint"]
    command: str | None = None
    expected_result: str
    timeout_seconds: int = 120


class VerificationStrategy(AgentModel):
    checks: list[VerificationCheck] = Field(default_factory=list)
    max_repair_attempts: int = 2
    pass_criteria: list[str] = Field(default_factory=list)


class RiskItem(AgentModel):
    id: str
    description: str
    severity: Literal["low", "medium", "high"] = "medium"
    mitigation: str
    related_fields: list[str] = Field(default_factory=list)


class PlanStep(AgentModel):
    id: str
    name: str
    step_type: Literal[
        "generate_file",
        "generate_config",
        "generate_test",
        "write_report",
        "install_dependencies",
        "run_verification",
        "package_output",
    ]
    description: str
    inputs: dict[str, Any] = Field(default_factory=dict)
    outputs: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    requires_human_approval: bool = False


class ReproductionPlan(AgentModel):
    id: str
    paper_title: str
    created_at: str
    status: Literal["draft", "awaiting_review", "superseded"] = "draft"
    objective: ReproductionObjective
    user_constraints: UserConstraints
    selected_claims: list[str] = Field(default_factory=list)
    scope: TargetScope
    paper_spec_ref: str
    implementation_strategy: ImplementationStrategy
    dependency_strategy: DependencyStrategy
    data_strategy: DataStrategy
    verification_strategy: VerificationStrategy
    execution_steps: list[PlanStep] = Field(default_factory=list)
    required_confirmations: list[ConfirmationItem] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)


class PlanningReasoningStep(AgentModel):
    """规划推理单步 — 每步产出可证伪假设，而非死结论。"""

    step_id: str
    sequence: int
    reasoning_type: Literal[
        "understand_architecture",
        "decompose_components",
        "identify_dependencies",
        "resolve_ambiguity",
        "estimate_effort",
        "identify_risk",
        "propose_file_structure",
        "design_tensor_contract",
        "validate_assumption",
    ]
    input_evidence_ids: list[str] = Field(default_factory=list)
    reasoning: str
    hypothesis: str
    falsifiable_via: str
    confidence: float = 0.0
    alternatives_considered: list[str] = Field(default_factory=list)


class PlanningTrace(AgentModel):
    """完整规划推理链 — 可证伪假设集，execute 的回灌目标。"""

    id: str
    run_id: str
    plan_id: str
    steps: list[PlanningReasoningStep] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    decisions_made: list[str] = Field(default_factory=list)
    invalidated_steps: list[str] = Field(default_factory=list)
    created_at: str


class PlanFeedbackItem(AgentModel):
    """A single feedback entry comparing a planning hypothesis against execution reality."""

    planning_step_id: str
    hypothesis: str
    falsifiable_via: str
    verdict: Literal["confirmed", "falsified", "untested", "inconclusive"] = "untested"
    evidence_refs: list[str] = Field(default_factory=list)
    actual_result: str | None = None


class PlanFeedbackReport(AgentModel):
    """Post-execution feedback report — compares PlanningTrace hypotheses to
    execution results so the plan can be revised for future runs."""

    id: str
    run_id: str
    plan_id: str
    planning_trace_id: str
    items: list[PlanFeedbackItem] = Field(default_factory=list)
    confirmed_count: int = 0
    falsified_count: int = 0
    inconclusive_count: int = 0
    untested_count: int = 0
    rollback_triggered: bool = False
    summary: str = ""
    created_at: str


class PlanTraceRevision(AgentModel):
    """Records a partial re-plan triggered by falsified assumptions.
    Captures the before/after diff of PlanningTrace steps so the revision
    is auditable and HITL-reviewable."""

    id: str
    run_id: str
    plan_id: str
    trigger_step_ids: list[str] = Field(default_factory=list)
    original_trace_id: str
    revised_trace_id: str
    steps_before: list[PlanningReasoningStep] = Field(default_factory=list)
    steps_after: list[PlanningReasoningStep] = Field(default_factory=list)
    revision_rationale: str = ""
    evidence_from_execution: list[str] = Field(default_factory=list)
    created_at: str


class HumanReview(AgentModel):
    id: str
    plan_id: str
    summary: str
    plan_overview: list[str] = Field(default_factory=list)
    scope_summary: dict[str, Any] = Field(default_factory=dict)
    decision_requirements: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    confirmation_items: list[ConfirmationItem] = Field(default_factory=list)
    inferred_assumptions: list[Assumption] = Field(default_factory=list)
    missing_details: list[MissingDetail] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    recommended_action: Literal["approve", "revise", "downgrade_scope", "cancel"] = "approve"


class HumanDecision(AgentModel):
    item_id: str
    action: Literal["accept", "override", "reject", "defer"]
    value: Any | None = None
    note: str | None = None


class HumanDecisions(AgentModel):
    review_id: str
    decisions: list[HumanDecision] = Field(default_factory=list)
    global_action: Literal["approve", "revise", "cancel"] = "approve"
    user_notes: str | None = None


class ApprovedPlan(AgentModel):
    plan: ReproductionPlan
    human_decisions: HumanDecisions
    frozen_at: str
    approved_by: str | None = None
    plan_hash: str
    execution_allowed: bool


class ExecutionPolicy(AgentModel):
    run_id: str
    repo_root: str
    package_name: str
    package_manager: Literal["pip", "uv", "poetry", "conda"] = "pip"
    python_version: str = "3.10"
    allow_network: bool = False
    allow_large_downloads: bool = False
    max_runtime_minutes: int = 10
    max_repair_attempts: int = 2
    allowed_commands: list[str] = Field(default_factory=list)
    blocked_commands: list[str] = Field(default_factory=list)


class HITLGateConfig(AgentModel):
    """Loop-level HITL intervention triggers for agentic ReAct loop.

    Each trigger corresponds to a decision point where the agent can
    consult a human before proceeding. All default **off** so batch/CI
    runs are fully unattended — flip individual toggles for interactive
    debugging or high-stakes decisions.

    The human response is injected as a
    ToolMessage observation so the loop continues without stage-switch.
    """

    on_stall: bool = False
    """T1: Agent self-detects progress stall — ask human whether to
    change approach, give a hint, or force fallback."""

    on_smoke_failure_low_conf: bool = False
    """T2: run_smoke_forward reports an error AND the agent's last
    reasoning expresses low confidence — ask human for diagnosis help."""

    before_plan_invalid: bool = False
    """T3: Agent is about to emit plan_assumption_invalid (layer 4
    upgrade) — confirm with human before跨阶段 signalling."""

    before_critical_write: bool = False
    """T4: Agent is about to call write_repo_file — preview the code
    and let human approve the write (optional high-security gate)."""

    on_plan_assumption_falsified: bool = False
    """T5: A plan assumption has been falsified for the first time
    (most information-dense moment) — ask human to accept auto-rollback,
    manually revise the assumption, or continue as-is."""

    interactive: bool = False
    """Master kill-switch. When False, all triggers are no-ops regardless
    of individual toggles. Set True to enable the person-as-tool loop."""

    cooldown_rounds: int = 2
    """Minimum loop rounds between successive HITL firings of the same
    trigger type — prevents spamming the human."""


class CodebaseModule(AgentModel):
    name: str
    path: str
    responsibility: str
    public_api: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class CodebaseDesign(AgentModel):
    id: str
    package_name: str
    architecture_summary: str
    modules: list[CodebaseModule] = Field(default_factory=list)
    entrypoints: list[str] = Field(default_factory=list)
    test_strategy: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    generated_by: Literal["rule", "llm", "hybrid"] = "rule"


class ModuleImplementationPlan(AgentModel):
    module_name: str
    path: str
    responsibilities: list[str] = Field(default_factory=list)
    public_api: list[str] = Field(default_factory=list)
    depends_on: list[str] = Field(default_factory=list)
    implementation_steps: list[str] = Field(default_factory=list)
    invariants: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ImplementationTensorContract(AgentModel):
    name: str
    role: Literal["input", "output", "intermediate", "target"] = "intermediate"
    shape: list[str | int | None] = Field(default_factory=list)
    dtype: str | None = None
    description: str = ""
    evidence_ids: list[str] = Field(default_factory=list)


class ImplementationComponentPlan(AgentModel):
    name: str
    component_type: str = "custom"
    target_file: str = "model.py"
    responsibility: str = ""
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    implementation_notes: list[str] = Field(default_factory=list)
    hyperparameters: dict[str, Any] = Field(default_factory=dict)
    evidence_ids: list[str] = Field(default_factory=list)


class ImplementationLossTermPlan(AgentModel):
    name: str
    target_file: str = "losses.py"
    loss_type: str = "custom"
    formula: str | None = None
    required_inputs: list[str] = Field(default_factory=list)
    implementation_notes: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class ImplementationInterfaceField(AgentModel):
    name: str
    type: str = "Tensor"
    shape: list[str | int | None] = Field(default_factory=list)
    dtype: str | None = None
    required: bool = True
    semantics: str = ""
    aliases: list[str] = Field(default_factory=list)


class ImplementationDatasetInterface(AgentModel):
    producer: str = "dataset.build_train_dataloader"
    fields: list[ImplementationInterfaceField] = Field(default_factory=list)


class ImplementationModelForwardInterface(AgentModel):
    call: str = "outputs = model(batch)"
    output_type: Literal["tensor", "dict", "tuple", "custom"] = "tensor"
    fields: list[ImplementationInterfaceField] = Field(default_factory=list)


class ImplementationCallableInterface(AgentModel):
    call: str = ""
    primary: bool = True
    consumes: list[str] = Field(default_factory=list)
    produces: list[str] = Field(default_factory=list)


class ImplementationInterfaces(AgentModel):
    dataset_batch: ImplementationDatasetInterface | None = None
    model_forward: ImplementationModelForwardInterface | None = None
    loss: ImplementationCallableInterface | None = None
    evaluation: ImplementationCallableInterface | None = None
    train_step: list[str] = Field(default_factory=list)


class CodeImplementationPlan(AgentModel):
    id: str
    summary: str
    input_tensors: list[ImplementationTensorContract] = Field(default_factory=list)
    output_tensors: list[ImplementationTensorContract] = Field(default_factory=list)
    model_components: list[ImplementationComponentPlan] = Field(default_factory=list)
    loss_terms: list[ImplementationLossTermPlan] = Field(default_factory=list)
    equation_notes: list[str] = Field(default_factory=list)
    data_flow: list[str] = Field(default_factory=list)
    training_flow: list[str] = Field(default_factory=list)
    interfaces: ImplementationInterfaces = Field(default_factory=ImplementationInterfaces)
    paper_alignment_checks: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    generated_by: Literal["rule", "llm", "hybrid"] = "hybrid"


class ExecutionCodingPlan(AgentModel):
    id: str
    package_name: str
    architecture_summary: str
    code_implementation_plan: CodeImplementationPlan | None = None
    module_plans: list[ModuleImplementationPlan] = Field(default_factory=list)
    file_order: list[str] = Field(default_factory=list)
    global_invariants: list[str] = Field(default_factory=list)
    context_strategy: list[str] = Field(default_factory=list)
    generated_by: Literal["rule", "llm", "hybrid"] = "hybrid"


class CritiqueIssue(AgentModel):
    """Single issue found by the plan critique node."""

    id: str
    severity: Literal["error", "warning", "info"] = "warning"
    category: Literal[
        "tensor_contract_gap",
        "dependency_order",
        "missing_module",
        "missing_loss",
        "missing_component",
        "ambiguous_api",
        "untestable_claim",
        "scope_creep",
        "other",
    ] = "other"
    description: str
    affected_artifacts: list[str] = Field(default_factory=list)
    suggested_fix: str | None = None


class CodingPlanCritique(AgentModel):
    """Result of the plan critique node — validates CodingPlan self-consistency.

    Runs after CodingPlan generation, before FileGenerationDAG.
    Checks tensor contract closure, dependency ordering, missing modules,
    and other structural invariants.
    """

    id: str
    coding_plan_id: str
    passed: bool = True
    issues: list[CritiqueIssue] = Field(default_factory=list)
    summary: str = ""
    created_at: str


class RetrievedContextItem(AgentModel):
    source_type: Literal[
        "evidence",
        "plan",
        "validation",
        "coding_plan",
        "implementation_plan",
        "failure",
        "contract",
    ] = "evidence"
    source_id: str
    title: str
    content: str
    field_path: str | None = None
    score: float = 0.0


class RetrievalReport(AgentModel):
    id: str
    task_id: str
    file_path: str
    mode: Literal["generation", "repair"] = "generation"
    query_terms: list[str] = Field(default_factory=list)
    items: list[RetrievedContextItem] = Field(default_factory=list)
    selected_evidence_ids: list[str] = Field(default_factory=list)
    truncated: bool = False
    summary: str = ""


class ContractSymbol(AgentModel):
    name: str
    kind: Literal["class", "function", "constant", "variable"] = "function"
    required: bool = True
    description: str | None = None


class ContractFunction(AgentModel):
    name: str
    required_params: list[str] = Field(default_factory=list)
    return_contract: str | None = None


class IntegrationContract(AgentModel):
    provider_path: str
    required_symbols: list[str] = Field(default_factory=list)
    relationship: str = ""


class FileContract(AgentModel):
    path: str
    required_symbols: list[ContractSymbol] = Field(default_factory=list)
    required_functions: list[ContractFunction] = Field(default_factory=list)
    imports_allowed: list[str] = Field(default_factory=list)
    imports_forbidden: list[str] = Field(default_factory=list)
    runtime_constraints: list[str] = Field(default_factory=list)
    integration_contracts: list[IntegrationContract] = Field(default_factory=list)


class FileGenerationTask(AgentModel):
    id: str
    path: str
    purpose: str
    generation_mode: Literal["template", "llm", "hybrid"] = "template"
    depends_on: list[str] = Field(default_factory=list)
    relevant_evidence_ids: list[str] = Field(default_factory=list)
    plan_fields: list[str] = Field(default_factory=list)
    contract: list[str] = Field(default_factory=list)
    file_contract: FileContract | None = None
    layer: int = 0
    max_context_tokens: int = 6000


class FileGenerationLayer(AgentModel):
    layer: int
    task_ids: list[str] = Field(default_factory=list)
    rationale: str = ""


class FileGenerationDAG(AgentModel):
    layers: list[FileGenerationLayer] = Field(default_factory=list)
    edges: list[tuple[str, str]] = Field(default_factory=list)


class ContextReport(AgentModel):
    id: str
    task_id: str
    file_path: str
    evidence_ids: list[str] = Field(default_factory=list)
    included_plan_fields: list[str] = Field(default_factory=list)
    retrieval_report_id: str | None = None
    context_hash: str
    token_budget: int
    truncated: bool = False


class LLMCallRecord(AgentModel):
    id: str
    run_id: str
    task_id: str
    file_path: str
    model_name: str
    prompt_hash: str
    response_hash: str | None = None
    status: Literal["succeeded", "failed", "skipped", "fallback"] = "skipped"
    estimated_input_chars: int = 0
    estimated_output_chars: int = 0
    error: str | None = None
    created_at: str


class CostReport(AgentModel):
    llm_call_count: int = 0
    generated_file_count: int = 0
    repaired_file_count: int = 0
    estimated_input_chars: int = 0
    estimated_output_chars: int = 0
    cache_hits: int = 0
    notes: list[str] = Field(default_factory=list)


class FailureDigestItem(AgentModel):
    check_id: str
    failure_category: str
    summary: str
    target_files: list[str] = Field(default_factory=list)
    root_cause_hypotheses: list[str] = Field(default_factory=list)
    stderr_excerpt: str | None = None
    stdout_excerpt: str | None = None
    truncated: bool = False


class FailureDigest(AgentModel):
    id: str
    primary_check_id: str | None = None
    primary_failure_category: str | None = None
    repo_health: Literal["healthy", "degraded", "broken"] = "healthy"
    items: list[FailureDigestItem] = Field(default_factory=list)
    recommended_repair_order: list[str] = Field(default_factory=list)
    created_at: str


class PatchPlanStep(AgentModel):
    path: str
    change_goal: str
    constraints: list[str] = Field(default_factory=list)
    focus_symbols: list[str] = Field(default_factory=list)


class PatchPlan(AgentModel):
    id: str
    repair_task_id: str
    failure_digest_id: str | None = None
    strategy_summary: str
    target_files: list[str] = Field(default_factory=list)
    steps: list[PatchPlanStep] = Field(default_factory=list)
    verification_focus: list[str] = Field(default_factory=list)
    created_at: str


class CommandSpec(AgentModel):
    id: str
    command: list[str]
    cwd: str
    timeout_seconds: int = 120
    purpose: str
    check_type: Literal[
        "compile",
        "import",
        "pytest",
        "smoke_train",
        "artifact_check",
    ]
    allowed_failure: bool = False


class CommandResult(AgentModel):
    command_id: str
    command: list[str]
    cwd: str
    status: Literal["passed", "failed", "skipped", "timeout"]
    exit_code: int | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    duration_seconds: float = 0.0


class ExecutionToolSpec(AgentModel):
    name: str
    description: str
    input_schema: str
    output_schema: str
    input_json_schema: dict | None = None
    output_json_schema: dict | None = None
    risk_level: Literal["low", "medium", "high"] = "low"
    requires_approval: bool = False
    allowed_phases: list[str] = Field(default_factory=list)
    writes_state: bool = False
    writes_files: bool = False


class ToolCallRecord(AgentModel):
    id: str
    run_id: str
    phase: str
    tool_name: str
    input_hash: str
    output_hash: str | None = None
    status: Literal["succeeded", "failed", "blocked", "skipped"]
    started_at: str
    ended_at: str
    error: str | None = None
    artifacts_written: list[str] = Field(default_factory=list)


class AgentAction(AgentModel):
    """Agent 单步动作记录（语义层，引用底层 record，不重复存 hash/时间）。"""

    step: int
    action_type: Literal[
        "think",
        "tool_call",
        "tool_result",
        "generate",
        "verify",
        "fix",
        "done",
    ]
    tool_name: str | None = None
    tool_call_record_id: str | None = None
    llm_call_record_id: str | None = None
    reasoning: str | None = None
    duration_ms: int = 0


class AgentTrace(AgentModel):
    """Agent 单次任务的完整执行轨迹 — 五态终止。"""

    id: str
    run_id: str
    task_id: str
    file_path: str
    actions: list[AgentAction] = Field(default_factory=list)
    total_steps: int = 0
    final_status: Literal[
        "success",
        "stall",
        "timeout",
        "plan_assumption_invalid",
        "fallback",
    ] = "success"
    stall_reason: str | None = None
    last_verdict: dict | None = None
    invalidated_planning_step_id: str | None = None
    rollback_iteration: int = 0
    created_at: str


class ExecutionUserDecision(AgentModel):
    id: str
    reason: str
    decision_type: Literal[
        "allow_network",
        "allow_install",
        "allow_large_download",
        "provide_dataset_path",
        "allow_long_run",
        "allow_gpu_run",
    ]
    status: Literal["approved", "rejected", "deferred"]
    value: Any | None = None
    decided_at: str
    decided_by: str | None = None


class PlanRevisionRecord(AgentModel):
    id: str
    iteration: int
    input_plan_ref: str | None = None
    input_plan_hash: str | None = None
    decision_ref: str | None = None
    output_plan_ref: str
    output_plan_hash: str
    model_name: str | None = None
    created_at: str
    status: Literal["drafted", "revised", "approved", "cancelled"] = "revised"
    notes: str | None = None


class GeneratedFile(AgentModel):
    path: str
    purpose: str
    source: Literal["template", "llm_generated", "programmatic", "patched"] = "programmatic"
    sha256: str
    task_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)
    context_report_id: str | None = None
    llm_call_id: str | None = None


class CheckResult(AgentModel):
    check_id: str
    status: Literal["passed", "failed", "skipped", "timeout"]
    command: str | None = None
    exit_code: int | None = None
    stdout_tail: str | None = None
    stderr_tail: str | None = None
    duration_seconds: float = 0.0
    failure_category: str | None = None
    target_files: list[str] = Field(default_factory=list)
    repair_hint: str | None = None


class VerificationResult(AgentModel):
    passed: bool
    checks: list[CheckResult] = Field(default_factory=list)
    summary: str = ""


class ContractVerificationIssue(AgentModel):
    path: str
    category: Literal[
        "contract_failure",
        "missing_symbol",
        "signature_mismatch",
        "unsafe_code",
        "integration_mismatch",
    ] = "contract_failure"
    message: str
    symbol: str | None = None
    provider_path: str | None = None
    consumer_path: str | None = None


class ContractVerificationRecord(AgentModel):
    path: str
    passed: bool = True
    required_symbols_checked: list[str] = Field(default_factory=list)
    required_functions_checked: list[str] = Field(default_factory=list)
    issues: list[ContractVerificationIssue] = Field(default_factory=list)


class ContractVerificationReport(AgentModel):
    id: str
    records: list[ContractVerificationRecord] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    summary: str = ""


class PlanStepResult(AgentModel):
    step_id: str
    status: Literal["passed", "failed", "skipped"]
    started_at: str
    ended_at: str
    output: str | None = None
    error: str | None = None


class RepairAttempt(AgentModel):
    attempt: int
    trigger_check_id: str
    error_type: str
    diagnosis: str
    files_changed: list[str] = Field(default_factory=list)
    patch_summary: str = ""
    verification_after_repair: VerificationResult | None = None


class RepairTask(AgentModel):
    id: str
    trigger_check_id: str
    target_files: list[str] = Field(default_factory=list)
    failure_summary: str
    repair_strategy: str
    priority: Literal["low", "medium", "high"] = "medium"
    failure_category: str | None = None
    repair_prompt: str | None = None


class RepairRecord(AgentModel):
    id: str
    repair_task_id: str
    files_changed: list[str] = Field(default_factory=list)
    input_file_hashes: dict[str, str] = Field(default_factory=dict)
    output_file_hashes: dict[str, str] = Field(default_factory=dict)
    llm_call_ids: list[str] = Field(default_factory=list)
    status: Literal["succeeded", "failed", "skipped"] = "skipped"
    created_at: str


class ProvenanceRecord(AgentModel):
    artifact_id: str
    artifact_path: str
    artifact_type: str
    generated_by: str
    input_refs: list[str] = Field(default_factory=list)
    source_policy_ref: str | None = None
    license: str | None = None
    sha256: str


class ExecutionResult(AgentModel):
    approved_plan_hash: str
    output_dir: str
    generated_files: list[GeneratedFile] = Field(default_factory=list)
    step_results: list[PlanStepResult] = Field(default_factory=list)
    verification_result: VerificationResult | None = None
    repair_history: list[RepairAttempt] = Field(default_factory=list)
    provenance_records: list[ProvenanceRecord] = Field(default_factory=list)
    final_status: Literal["passed", "failed", "partial", "cancelled"] = "partial"


class FinalReport(AgentModel):
    title: str
    status: Literal["success", "partial_success", "failed", "unsupported"] = "partial_success"
    reproduction_goal: str
    generated_repo_path: str | None = None
    paper_backed_facts: list[str] = Field(default_factory=list)
    assumptions: list[Assumption] = Field(default_factory=list)
    missing_details: list[MissingDetail] = Field(default_factory=list)
    unsupported_features: list[UnsupportedFeature] = Field(default_factory=list)
    verification_summary: str = ""
    commands_to_run: list[str] = Field(default_factory=list)
    next_steps: list[str] = Field(default_factory=list)


class RunManifest(AgentModel):
    run_id: str
    schema_version: str = SCHEMA_VERSION
    created_at: str
    updated_at: str
    status: str
    approval_status: Literal[
        "not_started",
        "awaiting_review",
        "revision_requested",
        "approved",
        "cancelled",
    ] = "not_started"
    approval_iteration_count: int = 0
    paper_input_ref: str | None = None
    parsed_paper_ref: str | None = None
    evidence_store_ref: str | None = None
    evidence_refs_ref: str | None = None
    paper_spec_ref: str | None = None
    validation_result_ref: str | None = None
    draft_plan_ref: str | None = None
    human_review_ref: str | None = None
    latest_plan_ref: str | None = None
    latest_review_ref: str | None = None
    human_decisions_ref: str | None = None
    approved_plan_ref: str | None = None
    planning_trace_ref: str | None = None
    agent_traces_ref: str | None = None
    plan_feedback_ref: str | None = None
    plan_trace_revision_ref: str | None = None
    generated_repo_ref: str | None = None
    execution_policy_ref: str | None = None
    coding_plan_ref: str | None = None
    retrieval_reports_ref: str | None = None
    repair_context_reports_ref: str | None = None
    file_contracts_ref: str | None = None
    file_generation_tasks_ref: str | None = None
    file_generation_dag_ref: str | None = None
    generated_files_ref: str | None = None
    failure_digest_ref: str | None = None
    patch_plan_ref: str | None = None
    provenance_ref: str | None = None
    command_specs_ref: str | None = None
    tool_calls_ref: str | None = None
    command_results_ref: str | None = None
    verification_result_ref: str | None = None
    contract_verification_ref: str | None = None
    execution_result_ref: str | None = None
    final_report_ref: str | None = None
    final_report_markdown_ref: str | None = None
    artifact_hashes: dict[str, str] = Field(default_factory=dict)
    database_refs: dict[str, str] = Field(default_factory=dict)


PaperInput.model_rebuild()
