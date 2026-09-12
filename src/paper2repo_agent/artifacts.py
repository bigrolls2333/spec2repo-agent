from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from paper2repo_agent.schemas import (
    ApprovedPlan,
    HumanReview,
    ParsedPaper,
    PaperInput,
    PaperSpec,
    PlanRevisionRecord,
    ReproductionPlan,
    RunManifest,
    SourcePolicy,
    SpecValidationResult,
    UserConstraints,
)
from paper2repo_agent.utils import (
    ensure_dir,
    read_json,
    sha256_file,
    sha256_text,
    utc_now,
    write_json,
    write_text,
)


@dataclass
class RunPaths:
    """Serializable path locator stored safely in durable graph checkpoints."""

    run_dir: Path

    def __post_init__(self) -> None:
        # Resolve to an absolute path so that artifact references computed via
        # `path.relative_to(run_dir)` never fail with a relative-vs-absolute
        # mismatch when the CLI is invoked with a relative run directory.
        self.run_dir = self.run_dir.resolve()

    @property
    def planning_dir(self) -> Path:
        return self.run_dir / "planning"

    @property
    def approval_dir(self) -> Path:
        return self.run_dir / "approval"

    @property
    def execution_dir(self) -> Path:
        return self.run_dir / "execution"

    @property
    def generated_repo(self) -> Path:
        return self.run_dir / "generated_repo"

    @property
    def manifest(self) -> Path:
        return self.run_dir / "run_manifest.json"

    @property
    def paper_input(self) -> Path:
        return self.planning_dir / "paper_input.json"

    @property
    def parsed_paper(self) -> Path:
        return self.planning_dir / "parsed_paper.json"

    @property
    def evidence_store(self) -> Path:
        return self.planning_dir / "evidence_store.jsonl"

    @property
    def evidence_refs(self) -> Path:
        return self.planning_dir / "evidence_refs.jsonl"

    @property
    def evidence_sqlite(self) -> Path:
        return self.planning_dir / "evidence.sqlite"

    @property
    def evidence_database_refs(self) -> Path:
        return self.planning_dir / "evidence_database_refs.json"

    @property
    def paper_spec(self) -> Path:
        return self.planning_dir / "extracted_spec.json"

    @property
    def validation_result(self) -> Path:
        return self.planning_dir / "validation_result.json"

    @property
    def user_constraints(self) -> Path:
        return self.planning_dir / "user_constraints.json"

    @property
    def draft_plan(self) -> Path:
        return self.planning_dir / "draft_plan.json"

    @property
    def human_review_json(self) -> Path:
        return self.planning_dir / "human_review.json"

    @property
    def human_review_md(self) -> Path:
        return self.planning_dir / "human_review.md"

    @property
    def source_policy(self) -> Path:
        return self.planning_dir / "source_policy.json"

    @property
    def approval_decisions_template(self) -> Path:
        return self.planning_dir / "approval_decisions.template.json"

    @property
    def approval_iterations_dir(self) -> Path:
        return self.approval_dir / "iterations"

    @property
    def latest_plan(self) -> Path:
        return self.approval_dir / "latest_plan.json"

    @property
    def latest_human_review_json(self) -> Path:
        return self.approval_dir / "latest_human_review.json"

    @property
    def latest_human_review_md(self) -> Path:
        return self.approval_dir / "latest_human_review.md"

    @property
    def human_decisions(self) -> Path:
        return self.approval_dir / "human_decisions.json"

    @property
    def approved_plan(self) -> Path:
        return self.approval_dir / "approved_plan.json"

    @property
    def execution_policy(self) -> Path:
        return self.execution_dir / "execution_policy.json"

    @property
    def codebase_design(self) -> Path:
        return self.execution_dir / "codebase_design.json"

    @property
    def coding_plan(self) -> Path:
        return self.execution_dir / "coding_plan.json"

    @property
    def critique_coding_plan(self) -> Path:
        return self.execution_dir / "critique_coding_plan.json"

    @property
    def retrieval_reports(self) -> Path:
        return self.execution_dir / "retrieval_reports.jsonl"

    @property
    def repair_context_reports(self) -> Path:
        return self.execution_dir / "repair_context_reports.jsonl"

    @property
    def file_contracts(self) -> Path:
        return self.execution_dir / "file_contracts.json"

    @property
    def file_generation_tasks(self) -> Path:
        return self.execution_dir / "file_generation_tasks.json"

    @property
    def file_generation_dag(self) -> Path:
        return self.execution_dir / "file_generation_dag.json"

    @property
    def generated_files(self) -> Path:
        return self.execution_dir / "generated_files.jsonl"

    @property
    def code_trace(self) -> Path:
        return self.execution_dir / "code_trace.jsonl"

    @property
    def context_reports(self) -> Path:
        return self.execution_dir / "context_reports.jsonl"

    @property
    def llm_calls(self) -> Path:
        return self.execution_dir / "llm_calls.jsonl"

    @property
    def cost_report(self) -> Path:
        return self.execution_dir / "cost_report.json"

    @property
    def failure_digest(self) -> Path:
        return self.execution_dir / "failure_digest.json"

    @property
    def patch_plan(self) -> Path:
        return self.execution_dir / "patch_plan.json"

    @property
    def repair_tasks(self) -> Path:
        return self.execution_dir / "repair_tasks.json"

    @property
    def provenance(self) -> Path:
        return self.execution_dir / "provenance.jsonl"

    @property
    def command_results(self) -> Path:
        return self.execution_dir / "command_results.jsonl"

    @property
    def command_specs(self) -> Path:
        return self.execution_dir / "command_specs.json"

    @property
    def tool_calls(self) -> Path:
        return self.execution_dir / "tool_calls.jsonl"

    @property
    def verification_result(self) -> Path:
        return self.execution_dir / "verification_result.json"

    @property
    def contract_verification(self) -> Path:
        return self.execution_dir / "contract_verification.json"

    @property
    def execution_result(self) -> Path:
        return self.execution_dir / "execution_result.json"

    @property
    def planning_trace(self) -> Path:
        return self.planning_dir / "planning_trace.json"

    @property
    def agent_traces(self) -> Path:
        return self.execution_dir / "agent_traces.jsonl"

    @property
    def plan_feedback(self) -> Path:
        return self.execution_dir / "plan_feedback.json"

    @property
    def plan_trace_revision(self) -> Path:
        return self.execution_dir / "plan_trace_revision.json"

    @property
    def final_report_json(self) -> Path:
        return self.run_dir / "final_report.json"

    @property
    def final_report_md(self) -> Path:
        return self.run_dir / "final_report.md"

    def repair_dir(self, iteration: int) -> Path:
        return self.execution_dir / "repairs" / f"{iteration:03d}"

    def iteration_dir(self, iteration: int) -> Path:
        return self.approval_iterations_dir / f"{iteration:03d}"

    def iteration_plan(self, iteration: int) -> Path:
        return self.iteration_dir(iteration) / "plan.json"

    def iteration_human_review_json(self, iteration: int) -> Path:
        return self.iteration_dir(iteration) / "human_review.json"

    def iteration_human_review_md(self, iteration: int) -> Path:
        return self.iteration_dir(iteration) / "human_review.md"

    def iteration_human_decisions(self, iteration: int) -> Path:
        return self.iteration_dir(iteration) / "human_decisions.json"

    def iteration_revision_record(self, iteration: int) -> Path:
        return self.iteration_dir(iteration) / "revision_record.json"


def prepare_run_dirs(run_dir: Path) -> RunPaths:
    paths = RunPaths(run_dir)
    ensure_dir(paths.planning_dir)
    ensure_dir(paths.approval_dir)
    ensure_dir(paths.approval_iterations_dir)
    ensure_dir(paths.execution_dir)
    return paths


def write_planning_artifacts(
    *,
    paths: RunPaths,
    paper_input: PaperInput,
    parsed_paper: ParsedPaper,
    source_policy: SourcePolicy,
    paper_spec: PaperSpec,
    validation_result: SpecValidationResult,
    user_constraints: UserConstraints,
    draft_plan: ReproductionPlan,
    human_review: HumanReview,
    human_review_markdown: str,
    approval_decision_template: dict[str, Any],
) -> None:
    write_json(paths.paper_input, paper_input.model_dump(mode="json"))
    write_json(paths.parsed_paper, parsed_paper.model_dump(mode="json"))
    write_json(paths.source_policy, source_policy.model_dump(mode="json"))
    write_json(paths.paper_spec, paper_spec.model_dump(mode="json"))
    write_json(paths.validation_result, validation_result.model_dump(mode="json"))
    write_json(paths.user_constraints, user_constraints.model_dump(mode="json"))
    write_json(paths.draft_plan, draft_plan.model_dump(mode="json"))
    write_json(paths.human_review_json, human_review.model_dump(mode="json"))
    write_text(paths.human_review_md, human_review_markdown)
    write_json(paths.approval_decisions_template, approval_decision_template)


def write_approval_iteration(
    *,
    paths: RunPaths,
    iteration: int,
    plan: ReproductionPlan,
    human_review: HumanReview,
    human_review_markdown: str,
    revision_record: PlanRevisionRecord,
    human_decisions: object | None = None,
) -> list[Path]:
    written = [
        paths.iteration_plan(iteration),
        paths.iteration_human_review_json(iteration),
        paths.iteration_human_review_md(iteration),
        paths.iteration_revision_record(iteration),
    ]
    write_json(paths.iteration_plan(iteration), plan.model_dump(mode="json"))
    write_json(
        paths.iteration_human_review_json(iteration),
        human_review.model_dump(mode="json"),
    )
    write_text(paths.iteration_human_review_md(iteration), human_review_markdown)
    write_json(
        paths.iteration_revision_record(iteration),
        revision_record.model_dump(mode="json"),
    )
    if human_decisions is not None:
        write_json(paths.iteration_human_decisions(iteration), _dump(human_decisions))
        written.append(paths.iteration_human_decisions(iteration))
    write_latest_approval_artifacts(
        paths=paths,
        plan=plan,
        human_review=human_review,
        human_review_markdown=human_review_markdown,
    )
    written.extend(
        [
            paths.latest_plan,
            paths.latest_human_review_json,
            paths.latest_human_review_md,
        ]
    )
    return written


def write_iteration_decisions(
    *,
    paths: RunPaths,
    iteration: int,
    human_decisions: object,
) -> Path:
    write_json(paths.iteration_human_decisions(iteration), _dump(human_decisions))
    return paths.iteration_human_decisions(iteration)


def write_latest_approval_artifacts(
    *,
    paths: RunPaths,
    plan: ReproductionPlan,
    human_review: HumanReview,
    human_review_markdown: str,
) -> None:
    write_json(paths.latest_plan, plan.model_dump(mode="json"))
    write_json(paths.latest_human_review_json, human_review.model_dump(mode="json"))
    write_text(paths.latest_human_review_md, human_review_markdown)


def next_approval_iteration(paths: RunPaths) -> int:
    if not paths.approval_iterations_dir.exists():
        return 0
    indexes = [
        int(path.name)
        for path in paths.approval_iterations_dir.iterdir()
        if path.is_dir() and path.name.isdigit()
    ]
    return max(indexes, default=-1) + 1


def current_approval_iteration(paths: RunPaths) -> int | None:
    if not paths.approval_iterations_dir.exists():
        return None
    indexes = [
        int(path.name)
        for path in paths.approval_iterations_dir.iterdir()
        if path.is_dir() and path.name.isdigit()
    ]
    return max(indexes) if indexes else None


def count_approval_iterations(paths: RunPaths) -> int:
    if not paths.approval_iterations_dir.exists():
        return 0
    return len(
        [
            path
            for path in paths.approval_iterations_dir.iterdir()
            if path.is_dir() and path.name.isdigit()
        ]
    )


def list_approval_artifacts(paths: RunPaths) -> list[Path]:
    files = [
        paths.latest_plan,
        paths.latest_human_review_json,
        paths.latest_human_review_md,
        paths.human_decisions,
        paths.approved_plan,
    ]
    if paths.approval_iterations_dir.exists():
        files.extend(
            path for path in sorted(paths.approval_iterations_dir.rglob("*")) if path.is_file()
        )
    return [path for path in files if path.exists()]


def make_revision_record(
    *,
    paths: RunPaths,
    iteration: int,
    output_plan: ReproductionPlan,
    status: str,
    model_name: str | None,
    input_plan: ReproductionPlan | None = None,
    decision_ref: Path | None = None,
    notes: str | None = None,
) -> PlanRevisionRecord:
    return PlanRevisionRecord(
        id=f"revision_{iteration:03d}",
        iteration=iteration,
        input_plan_ref=_rel(paths.run_dir, paths.iteration_plan(iteration - 1))
        if input_plan is not None and iteration > 0
        else None,
        input_plan_hash=sha256_text(input_plan.model_dump_json())
        if input_plan is not None
        else None,
        decision_ref=_rel(paths.run_dir, decision_ref) if decision_ref else None,
        output_plan_ref=_rel(paths.run_dir, paths.iteration_plan(iteration)),
        output_plan_hash=sha256_text(output_plan.model_dump_json()),
        model_name=model_name,
        created_at=utc_now(),
        status=status,
        notes=notes,
    )


def write_manifest(
    *,
    paths: RunPaths,
    run_id: str,
    status: str,
    extra_files: Iterable[Path] = (),
    approved_plan: ApprovedPlan | None = None,
    approval_status: str = "not_started",
    approval_iteration_count: int | None = None,
) -> RunManifest:
    files = [
        paths.paper_input,
        paths.parsed_paper,
        paths.evidence_store,
        paths.evidence_refs,
        paths.evidence_sqlite,
        paths.evidence_database_refs,
        paths.paper_spec,
        paths.validation_result,
        paths.user_constraints,
        paths.draft_plan,
        paths.human_review_json,
        paths.human_review_md,
        paths.source_policy,
        paths.approval_decisions_template,
        paths.latest_plan,
        paths.latest_human_review_json,
        paths.latest_human_review_md,
        paths.execution_policy,
        paths.coding_plan,
        paths.retrieval_reports,
        paths.repair_context_reports,
        paths.file_contracts,
        paths.file_generation_tasks,
        paths.file_generation_dag,
        paths.generated_files,
        paths.failure_digest,
        paths.patch_plan,
        paths.provenance,
        paths.command_specs,
        paths.tool_calls,
        paths.command_results,
        paths.verification_result,
        paths.contract_verification,
        paths.execution_result,
        paths.planning_trace,
        paths.agent_traces,
        paths.plan_feedback,
        paths.plan_trace_revision,
        paths.final_report_json,
        paths.final_report_md,
        *extra_files,
    ]
    hashes = {
        _rel(paths.run_dir, path): sha256_file(path)
        for path in files
        if path.exists() and path.is_file()
    }
    now = utc_now()
    database_refs = {"evidence": _rel(paths.run_dir, paths.evidence_sqlite)}
    if paths.evidence_database_refs.exists():
        refs_payload = read_json(paths.evidence_database_refs)
        refs = refs_payload.get("database_refs") if isinstance(refs_payload, dict) else None
        if isinstance(refs, dict):
            database_refs.update(
                {str(key): str(value) for key, value in refs.items() if key != "evidence" and value}
            )

    manifest = RunManifest(
        run_id=run_id,
        created_at=now,
        updated_at=now,
        status=status,
        approval_status=approval_status,
        approval_iteration_count=approval_iteration_count
        if approval_iteration_count is not None
        else count_approval_iterations(paths),
        paper_input_ref=_rel(paths.run_dir, paths.paper_input),
        parsed_paper_ref=_rel(paths.run_dir, paths.parsed_paper),
        evidence_store_ref=_rel(paths.run_dir, paths.evidence_store),
        evidence_refs_ref=_rel(paths.run_dir, paths.evidence_refs),
        paper_spec_ref=_rel(paths.run_dir, paths.paper_spec),
        validation_result_ref=_rel(paths.run_dir, paths.validation_result),
        draft_plan_ref=_rel(paths.run_dir, paths.draft_plan),
        human_review_ref=_rel(paths.run_dir, paths.human_review_json),
        latest_plan_ref=_rel(paths.run_dir, paths.latest_plan)
        if paths.latest_plan.exists()
        else None,
        latest_review_ref=_rel(paths.run_dir, paths.latest_human_review_md)
        if paths.latest_human_review_md.exists()
        else None,
        human_decisions_ref=_rel(paths.run_dir, paths.human_decisions)
        if paths.human_decisions.exists()
        else None,
        approved_plan_ref=_rel(paths.run_dir, paths.approved_plan)
        if approved_plan or paths.approved_plan.exists()
        else None,
        planning_trace_ref=_rel(paths.run_dir, paths.planning_trace)
        if paths.planning_trace.exists()
        else None,
        agent_traces_ref=_rel(paths.run_dir, paths.agent_traces)
        if paths.agent_traces.exists()
        else None,
        plan_feedback_ref=_rel(paths.run_dir, paths.plan_feedback)
        if paths.plan_feedback.exists()
        else None,
        plan_trace_revision_ref=_rel(paths.run_dir, paths.plan_trace_revision)
        if paths.plan_trace_revision.exists()
        else None,
        generated_repo_ref=_rel(paths.run_dir, paths.generated_repo)
        if paths.generated_repo.exists()
        else None,
        execution_policy_ref=_rel(paths.run_dir, paths.execution_policy)
        if paths.execution_policy.exists()
        else None,
        coding_plan_ref=_rel(paths.run_dir, paths.coding_plan)
        if paths.coding_plan.exists()
        else None,
        retrieval_reports_ref=_rel(paths.run_dir, paths.retrieval_reports)
        if paths.retrieval_reports.exists()
        else None,
        repair_context_reports_ref=_rel(paths.run_dir, paths.repair_context_reports)
        if paths.repair_context_reports.exists()
        else None,
        file_contracts_ref=_rel(paths.run_dir, paths.file_contracts)
        if paths.file_contracts.exists()
        else None,
        file_generation_tasks_ref=_rel(paths.run_dir, paths.file_generation_tasks)
        if paths.file_generation_tasks.exists()
        else None,
        file_generation_dag_ref=_rel(paths.run_dir, paths.file_generation_dag)
        if paths.file_generation_dag.exists()
        else None,
        generated_files_ref=_rel(paths.run_dir, paths.generated_files)
        if paths.generated_files.exists()
        else None,
        failure_digest_ref=_rel(paths.run_dir, paths.failure_digest)
        if paths.failure_digest.exists()
        else None,
        patch_plan_ref=_rel(paths.run_dir, paths.patch_plan) if paths.patch_plan.exists() else None,
        provenance_ref=_rel(paths.run_dir, paths.provenance) if paths.provenance.exists() else None,
        command_specs_ref=_rel(paths.run_dir, paths.command_specs)
        if paths.command_specs.exists()
        else None,
        tool_calls_ref=_rel(paths.run_dir, paths.tool_calls) if paths.tool_calls.exists() else None,
        command_results_ref=_rel(paths.run_dir, paths.command_results)
        if paths.command_results.exists()
        else None,
        verification_result_ref=_rel(paths.run_dir, paths.verification_result)
        if paths.verification_result.exists()
        else None,
        contract_verification_ref=_rel(paths.run_dir, paths.contract_verification)
        if paths.contract_verification.exists()
        else None,
        execution_result_ref=_rel(paths.run_dir, paths.execution_result)
        if paths.execution_result.exists()
        else None,
        final_report_ref=_rel(paths.run_dir, paths.final_report_json)
        if paths.final_report_json.exists()
        else None,
        final_report_markdown_ref=_rel(paths.run_dir, paths.final_report_md)
        if paths.final_report_md.exists()
        else None,
        artifact_hashes=hashes,
        database_refs=database_refs,
    )
    write_json(paths.manifest, manifest.model_dump(mode="json"))
    return manifest


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _dump(value: object) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value
