from __future__ import annotations

from typing import Any

from paper2repo_agent.schemas import (
    Assumption,
    ConfirmationItem,
    MissingDetail,
    PaperSpec,
    SpecValidationResult,
    UnsupportedFeature,
    UserConstraints,
    ValidationIssue,
)
from paper2repo_agent.utils import stable_id

SUPPORTED_TASKS = {
    "classification",
    "image_classification",
    "text_classification",
    "tabular_classification",
    "regression",
    "sequence_classification",
    "retrieval",
    "cross_modal_retrieval",
    "cross_modal_hashing",
}


def _missing(value: Any) -> bool:
    return value is None or value == "" or value == []


def validate_spec(spec: PaperSpec, constraints: UserConstraints) -> SpecValidationResult:
    missing_details: list[MissingDetail] = []
    assumptions: list[Assumption] = []
    confirmations: list[ConfirmationItem] = []
    warnings: list[ValidationIssue] = []
    unsupported: list[UnsupportedFeature] = []

    task_type = spec.task.task_type.value
    if _missing(task_type):
        missing_details.append(
            MissingDetail(
                id=stable_id("missing", spec.id, "task.task_type"),
                field_path="task.task_type",
                description="Task type is not explicit in the extracted paper spec.",
                why_required="The generated reproduction plan needs a task type.",
                severity="important",
                suggested_resolution=(
                    "Confirm classification, regression, or another supported task."
                ),
            )
        )
    elif str(task_type).lower() not in SUPPORTED_TASKS:
        unsupported.append(
            UnsupportedFeature(
                feature=str(task_type),
                reason="V1 supports supervised learning planning paths.",
                workaround="Downgrade to scaffold or spec-only planning.",
                severity="degrades_fidelity",
                evidence_ids=spec.task.task_type.evidence_ids,
            )
        )

    if _missing(spec.model.architecture_name.value):
        missing_details.append(
            MissingDetail(
                id=stable_id("missing", spec.id, "model.architecture_name"),
                field_path="model.architecture_name",
                description="Architecture name is missing.",
                why_required="The implementation plan needs a named model target.",
                severity="important",
                suggested_resolution="Use PaperModel as a placeholder during scaffold planning.",
            )
        )
        assumptions.append(
            Assumption(
                id=stable_id("assumption", spec.id, "model.architecture_name"),
                field_path="model.architecture_name",
                assumed_value="PaperModel",
                reason="A stable class name is required for generated scaffolds.",
                risk="low",
                requires_confirmation=False,
            )
        )

    if _missing(spec.training.batch_size.value):
        assumption = Assumption(
            id=stable_id("assumption", spec.id, "training.batch_size"),
            field_path="training.batch_size",
            assumed_value=32,
            reason="The paper does not provide batch size; 32 is safe for CPU smoke tests.",
            risk="medium",
            requires_confirmation=True,
            alternatives=[8, 16, 64],
        )
        assumptions.append(assumption)
        confirmations.append(
            ConfirmationItem(
                id=stable_id("confirm", assumption.id),
                field_path="training.batch_size",
                question="Confirm inferred batch size for smoke-test planning.",
                current_value=None,
                suggested_value=32,
                reason=assumption.reason,
                severity="recommended",
                options=[8, 16, 32, 64],
            )
        )

    if _missing(spec.training.optimizer.value):
        assumptions.append(
            Assumption(
                id=stable_id("assumption", spec.id, "training.optimizer"),
                field_path="training.optimizer",
                assumed_value="AdamW",
                reason=(
                    "AdamW is a common default for modern PyTorch supervised learning scaffolds."
                ),
                risk="medium",
                requires_confirmation=True,
                alternatives=["Adam", "SGD"],
            )
        )

    confirmations.append(
        ConfirmationItem(
            id=stable_id("confirm", spec.id, "compute_target"),
            field_path="user_constraints.compute_target",
            question="Confirm the compute target for V1 planning.",
            current_value=constraints.compute_target,
            suggested_value=constraints.compute_target,
            reason="Planning output depends on CPU, single-GPU, or multi-GPU constraints.",
            severity="required",
            options=["cpu_smoke", "single_gpu", "multi_gpu"],
            blocks_execution_if_unanswered=True,
        )
    )

    support_level = "partial"
    supported = True
    if unsupported and constraints.reproduction_goal == "full_metric_attempt":
        support_level = "scaffold_only"
    if len([item for item in missing_details if item.blocks_execution]) > 0:
        supported = False
        support_level = "unsupported"

    for detail in missing_details:
        warnings.append(
            ValidationIssue(
                id=stable_id("issue", detail.id),
                severity="warning" if detail.severity != "blocking" else "error",
                field_path=detail.field_path,
                message=detail.description,
                suggested_action=detail.suggested_resolution,
                evidence_ids=detail.evidence_ids,
            )
        )

    return SpecValidationResult(
        id=stable_id("validation", spec.id),
        supported=supported,
        support_level=support_level,
        warnings=warnings,
        missing_details=missing_details,
        assumptions=assumptions,
        unsupported_features=unsupported,
        required_confirmations=confirmations,
    )
