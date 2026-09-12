from __future__ import annotations

from paper2repo_agent.schemas import (
    DataStrategy,
    DependencyStrategy,
    FilePlan,
    HumanDecisions,
    ImplementationStrategy,
    PackageRequirement,
    PaperSpec,
    PlanStep,
    ReproductionObjective,
    ReproductionPlan,
    RiskItem,
    SpecValidationResult,
    SyntheticDataSpec,
    TargetScope,
    UserConstraints,
    VerificationCheck,
    VerificationStrategy,
)
from paper2repo_agent.utils import stable_id, utc_now


def draft_reproduction_plan(
    spec: PaperSpec,
    validation: SpecValidationResult,
    constraints: UserConstraints,
) -> ReproductionPlan:
    title = str(spec.metadata.title.value or "Untitled Paper")
    package_name = _slug(title) or "paper_impl"
    objective_goal = _objective_goal(constraints.reproduction_goal)
    dataset_mode = _dataset_mode(constraints.dataset_policy)
    device_target = _device_target(constraints.compute_target)
    runtime_packages = _runtime_packages(constraints, dataset_mode)
    execution_steps = _execution_steps(constraints)
    verification_strategy = _verification_strategy(constraints)

    return ReproductionPlan(
        id=stable_id("plan", spec.id, constraints.reproduction_goal, utc_now()),
        paper_title=title,
        created_at=utc_now(),
        status="awaiting_review",
        objective=ReproductionObjective(
            goal=objective_goal,
            description=_objective_description(constraints),
            success_criteria=_success_criteria(constraints),
            non_goals=_non_goals(constraints),
        ),
        user_constraints=constraints,
        selected_claims=[claim.id for claim in spec.claims_to_reproduce],
        scope=TargetScope(
            framework="pytorch",
            device_target=device_target,
            dataset_mode=dataset_mode,
            metric_reproduction=constraints.reproduction_goal == "full_metric_attempt",
            max_training_minutes=constraints.max_runtime_minutes,
        ),
        paper_spec_ref="planning/extracted_spec.json",
        implementation_strategy=ImplementationStrategy(
            repo_name=f"{package_name}_reproduction",
            package_name=package_name,
            architecture_summary=str(
                spec.model.architecture_name.value or "PaperModel architecture from PaperSpec"
            ),
            files=_build_file_plans(spec),
            generation_policy="hybrid",
        ),
        dependency_strategy=DependencyStrategy(
            package_manager=constraints.package_manager,
            python_version=constraints.python_version,
            runtime_packages=runtime_packages,
            dev_packages=[PackageRequirement(name="pytest", reason="Smoke tests")],
        ),
        data_strategy=DataStrategy(
            mode=dataset_mode,
            datasets=[
                str(dataset.name.value)
                for dataset in spec.datasets
                if dataset.name.value is not None
            ],
            synthetic_data_spec=(
                SyntheticDataSpec(task_type=str(spec.task.task_type.value or "classification"))
                if dataset_mode == "synthetic"
                else None
            ),
            user_data_requirements=_user_data_requirements(spec, constraints, dataset_mode),
            download_steps=_download_steps(spec, constraints, dataset_mode),
        ),
        verification_strategy=verification_strategy,
        execution_steps=execution_steps,
        required_confirmations=validation.required_confirmations,
        risks=[
            RiskItem(
                id=stable_id("risk", spec.id, "missing_details"),
                description="Paper implementation details may be missing or inferred.",
                severity="medium",
                mitigation="Review HumanReview and approve or override inferred settings.",
                related_fields=[item.field_path for item in validation.missing_details],
            )
        ],
    )


def apply_decisions_to_constraints(
    constraints: UserConstraints,
    decisions: HumanDecisions,
) -> UserConstraints:
    payload = constraints.model_dump(mode="json")
    for decision in decisions.decisions:
        if decision.action != "override":
            continue
        if not isinstance(decision.item_id, str):
            continue
        field_path = decision.item_id
        if field_path.startswith("user_constraints."):
            field_path = field_path[len("user_constraints.") :]
        if "." in field_path:
            continue
        if field_path not in payload:
            continue
        payload[field_path] = decision.value
    return UserConstraints.model_validate(payload)


def align_plan_confirmations(
    plan: ReproductionPlan,
    validation: SpecValidationResult,
) -> ReproductionPlan:
    plan.required_confirmations = [
        item.model_copy(deep=True) for item in validation.required_confirmations
    ]
    for item in plan.required_confirmations:
        if item.field_path == "user_constraints.compute_target":
            item.current_value = plan.user_constraints.compute_target
            item.suggested_value = plan.user_constraints.compute_target
        if item.field_path == "training.batch_size":
            batch_size = item.suggested_value
            synthetic = plan.data_strategy.synthetic_data_spec
            if synthetic is not None and isinstance(batch_size, int):
                synthetic.batch_size = batch_size
    return plan


def summarize_decisions(decisions: HumanDecisions) -> list[str]:
    lines: list[str] = []
    for decision in decisions.decisions:
        if decision.action == "override":
            lines.append(f"{decision.item_id} => {decision.value}")
        elif decision.action in {"reject", "defer"}:
            lines.append(f"{decision.item_id} => {decision.action}")
    if decisions.user_notes:
        lines.append(decisions.user_notes)
    return lines


def _objective_goal(goal: str) -> str:
    mapping = {
        "spec_only": "extract_spec",
        "scaffold_only": "generate_scaffold",
        "smoke_test": "run_smoke_test",
        "small_scale_result": "small_scale_reproduction",
        "full_metric_attempt": "full_reproduction_attempt",
    }
    return mapping.get(goal, "run_smoke_test")


def _objective_description(constraints: UserConstraints) -> str:
    descriptions = {
        "spec_only": "Extract an evidence-grounded implementation specification for the paper.",
        "scaffold_only": "Prepare a scaffold-focused reproduction plan aligned with the paper.",
        "smoke_test": "Prepare a smoke-testable reproduction plan grounded in the paper and user constraints.",
        "small_scale_result": "Prepare a limited-scale reproduction plan that can produce directional results.",
        "full_metric_attempt": "Prepare a metric-oriented reproduction plan within the stated runtime and hardware constraints.",
    }
    return descriptions.get(constraints.reproduction_goal, descriptions["smoke_test"])


def _success_criteria(constraints: UserConstraints) -> list[str]:
    lines = [
        "PaperSpec is extracted with evidence references.",
        "HumanReview lists missing settings, assumptions, and approval items.",
        "ApprovedPlan can be produced from user decisions.",
    ]
    if constraints.reproduction_goal in {"small_scale_result", "full_metric_attempt"}:
        lines.append(
            "Execution planning includes data access, training scope, and verification checks."
        )
    return lines


def _non_goals(constraints: UserConstraints) -> list[str]:
    lines = ["V1.1 does not generate the final PyTorch repository."]
    if constraints.reproduction_goal != "full_metric_attempt":
        lines.append("V1.1 does not target paper metric matching.")
    lines.append("V1.1 focuses on planning and human approval.")
    return lines


def _dataset_mode(policy: str) -> str:
    mapping = {
        "synthetic_only": "synthetic",
        "user_provided": "user_provided",
        "download_allowed": "download",
    }
    return mapping.get(policy, "synthetic")


def _device_target(compute_target: str) -> str:
    if compute_target == "cpu_smoke":
        return "cpu"
    return "cuda"


def _runtime_packages(constraints: UserConstraints, dataset_mode: str) -> list[PackageRequirement]:
    packages = [
        PackageRequirement(name="torch", reason="Target framework is PyTorch"),
        PackageRequirement(name="numpy", reason="Tensor and array utilities"),
        PackageRequirement(name="pyyaml", reason="Config file parsing"),
    ]
    if dataset_mode in {"download", "user_provided"}:
        packages.append(PackageRequirement(name="pandas", reason="Dataset metadata handling"))
    if constraints.allow_network:
        packages.append(
            PackageRequirement(name="requests", reason="Dataset download or API integration")
        )
    return packages


def _user_data_requirements(
    spec: PaperSpec,
    constraints: UserConstraints,
    dataset_mode: str,
) -> list[str]:
    if dataset_mode != "user_provided":
        return []
    dataset_names = [
        str(dataset.name.value) for dataset in spec.datasets if dataset.name.value not in (None, "")
    ]
    if dataset_names:
        return [f"Provide access to dataset: {name}" for name in dataset_names]
    return ["Provide the dataset files or prepared dataloader inputs required by the paper."]


def _download_steps(
    spec: PaperSpec,
    constraints: UserConstraints,
    dataset_mode: str,
) -> list[str]:
    if dataset_mode != "download":
        return []
    dataset_names = [
        str(dataset.name.value) for dataset in spec.datasets if dataset.name.value not in (None, "")
    ]
    lines = []
    for name in dataset_names:
        lines.append(f"Locate or confirm download procedure for dataset {name}.")
    if not lines:
        lines.append("Confirm dataset download source before execution.")
    if not constraints.allow_large_downloads:
        lines.append("Constrain download scope to metadata or a reduced sample.")
    return lines


def _verification_strategy(constraints: UserConstraints) -> VerificationStrategy:
    checks = [
        VerificationCheck(
            id="import_check",
            check_type="import",
            command='python -c "import paper_impl"',
            expected_result="Generated package imports successfully.",
        )
    ]
    pass_criteria = ["Generated files and configuration outputs are internally consistent."]
    if constraints.reproduction_goal != "spec_only":
        checks.append(
            VerificationCheck(
                id="pytest_smoke",
                check_type="pytest",
                command="python -m pytest tests/test_smoke.py",
                expected_result="Smoke tests pass.",
            )
        )
        pass_criteria.append("Core forward pass and loss computation run successfully.")
    if constraints.reproduction_goal in {"small_scale_result", "full_metric_attempt"}:
        checks.append(
            VerificationCheck(
                id="train_smoke",
                check_type="train_smoke",
                command="python -m paper_impl.train --max-steps 2",
                expected_result="A short training run completes with expected artifacts.",
                timeout_seconds=min(max(constraints.max_runtime_minutes * 60, 120), 1800),
            )
        )
        pass_criteria.append("Training scope matches the approved runtime envelope.")
    return VerificationStrategy(checks=checks, pass_criteria=pass_criteria)


def _execution_steps(constraints: UserConstraints) -> list[PlanStep]:
    steps: list[PlanStep] = [
        PlanStep(
            id="plan_review",
            name="Confirm planning assumptions",
            step_type="write_report",
            description="Review the extracted paper facts, missing details, and proposed scope.",
            outputs=["planning/human_review.md"],
            requires_human_approval=True,
        )
    ]
    if constraints.reproduction_goal == "spec_only":
        steps.append(
            PlanStep(
                id="export_spec",
                name="Export implementation spec",
                step_type="write_report",
                description="Publish a structured implementation specification and approval record.",
                depends_on=["plan_review"],
                outputs=["planning/extracted_spec.json", "approval/approved_plan.json"],
            )
        )
        return steps

    steps.append(
        PlanStep(
            id="generate_scaffold",
            name="Generate PyTorch scaffold",
            step_type="generate_file",
            description="Create model, dataset, training, config, and smoke-test files.",
            depends_on=["plan_review"],
            outputs=["generated_repo/"],
            requires_human_approval=True,
        )
    )

    if constraints.dataset_policy in {"user_provided", "download_allowed"}:
        steps.append(
            PlanStep(
                id="prepare_data",
                name="Prepare dataset access",
                step_type="generate_config",
                description="Define dataset paths, download steps, or adapter requirements for the approved scope.",
                depends_on=["generate_scaffold"],
                outputs=["configs/data.yaml"],
            )
        )

    if constraints.reproduction_goal in {"smoke_test", "small_scale_result", "full_metric_attempt"}:
        steps.append(
            PlanStep(
                id="run_smoke_tests",
                name="Run smoke tests",
                step_type="run_verification",
                description="Run import and pytest smoke checks.",
                depends_on=["generate_scaffold"],
            )
        )

    if constraints.reproduction_goal in {"small_scale_result", "full_metric_attempt"}:
        steps.append(
            PlanStep(
                id="limited_train",
                name="Run limited training",
                step_type="run_verification",
                description="Run a reduced training job consistent with runtime and hardware limits.",
                depends_on=["run_smoke_tests"],
            )
        )

    if constraints.reproduction_goal == "full_metric_attempt":
        steps.append(
            PlanStep(
                id="metric_compare",
                name="Compare reported metrics",
                step_type="write_report",
                description="Record metric deltas between the approved run and paper targets.",
                depends_on=["limited_train"],
                outputs=["reports/metric_comparison.md"],
            )
        )
    return steps


def _slug(value: str) -> str:
    chars = [char.lower() if char.isalnum() else "_" for char in value]
    slug = "".join(chars).strip("_")
    while "__" in slug:
        slug = slug.replace("__", "_")
    return slug[:48]


# Core code files that should always be LLM-generated from the paper spec.
# Infrastructure files (config/__init__/pyproject/requirements/scripts) stay
# template-generated and are appended by the execute graph, not declared here.
_LLM_CORE_FILES: tuple[tuple[str, str], ...] = (
    ("src/paper_impl/model.py", "Model architecture implementation"),
    ("src/paper_impl/losses.py", "Loss functions implementation"),
    ("src/paper_impl/dataset.py", "Synthetic dataset and adapters"),
    ("src/paper_impl/train.py", "Training loop and runtime entry"),
    ("src/paper_impl/metrics.py", "Evaluation metrics implementation"),
    ("src/paper_impl/evaluate.py", "Evaluation protocol and hash-code helpers"),
)

# Optional component files derived from the paper's model components. Each entry
# maps a keyword set (matched against component name/type) to a file.
_COMPONENT_FILE_RULES: tuple[tuple[tuple[str, ...], str, str], ...] = (
    (
        ("encoder", "embedding", "clip", "image_encoder", "text_encoder"),
        "src/paper_impl/encoders.py",
        "Modality encoders",
    ),
    (
        ("teacher", "residual", "distillation"),
        "src/paper_impl/teacher.py",
        "Teacher / distillation network",
    ),
    (
        ("center", "semantic_center", "normalization"),
        "src/paper_impl/centers.py",
        "Shared semantic centers",
    ),
    (
        ("calibration", "bilevel", "calibrat"),
        "src/paper_impl/bilevel.py",
        "Bilevel calibration mechanism",
    ),
)


def _component_file_plans(spec: PaperSpec) -> list[FilePlan]:
    """Derive optional component files from the paper's model components.

    Only files whose keyword is matched by at least one extracted component are
    emitted, so simple papers are not padded with meaningless empty modules.
    """
    haystack_parts: list[str] = []
    for component in spec.model.components:
        haystack_parts.append(str(component.name or "").lower())
        haystack_parts.append(str(component.component_type or "").lower())
    haystack = " ".join(haystack_parts)
    plans: list[FilePlan] = []
    seen: set[str] = set()
    for keywords, path, purpose in _COMPONENT_FILE_RULES:
        if path in seen:
            continue
        if any(keyword in haystack for keyword in keywords):
            plans.append(FilePlan(path=path, purpose=purpose, source="llm_generated"))
            seen.add(path)
    return plans


def _build_file_plans(spec: PaperSpec) -> list[FilePlan]:
    """Build the plan file list from the PaperSpec.

    Paper-specific code files (and the smoke test, which must match the real
    generated interfaces) are marked ``llm_generated`` so the execute graph
    routes them through contract-driven LLM generation. The smoke test is
    generated last so the LLM can read the already-generated model/train/dataset
    interfaces via the dependency-file context.
    """
    files: list[FilePlan] = [
        FilePlan(path=path, purpose=purpose, source="llm_generated")
        for path, purpose in _LLM_CORE_FILES
    ]
    files.extend(_component_file_plans(spec))
    files.append(
        FilePlan(
            path="tests/test_smoke.py",
            purpose="Smoke tests exercising the package end-to-end via train.run_smoke",
            source="template",
        )
    )
    return files
