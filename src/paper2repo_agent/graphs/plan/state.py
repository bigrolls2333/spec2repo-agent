from __future__ import annotations

from typing import Any, TypedDict

from paper2repo_agent.artifacts import RunPaths
from paper2repo_agent.config import Settings
from paper2repo_agent.schemas import (
    ConfirmationProposalSet,
    EvidenceStore,
    HumanReview,
    PaperInput,
    PaperSpec,
    ParsedPaper,
    PlanningTrace,
    ReproductionPlan,
    SourcePolicy,
    SpecValidationResult,
    UserConstraints,
)


class PlanGraphState(TypedDict, total=False):
    paper_path: str
    source_type: str
    output_dir: str
    run_id: str
    fake_llm: bool
    verbose: bool
    settings: Settings
    paths: RunPaths
    user_constraints: UserConstraints
    source_policy: SourcePolicy
    paper_input: PaperInput
    parsed_paper: ParsedPaper
    evidence_store: EvidenceStore
    paper_spec: PaperSpec
    validation_result: SpecValidationResult
    draft_plan: ReproductionPlan
    planning_trace: PlanningTrace
    confirmation_proposals: ConfirmationProposalSet
    human_review: HumanReview
    human_review_markdown: str
    approval_decision_template: dict[str, Any]
    manifest_status: str
    progress_callback: Any


def progress(state: PlanGraphState, message: str) -> None:
    callback = state.get("progress_callback")
    if callback is not None:
        callback(message)
    if state.get("verbose", True):
        print(f"[paper2repo] {message}", flush=True)
