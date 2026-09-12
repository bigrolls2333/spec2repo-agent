from __future__ import annotations

import json
import platform
from pathlib import Path

import typer
from rich.console import Console
from rich.prompt import Confirm, IntPrompt, Prompt
from rich.table import Table

from paper2repo_agent.artifacts import (
    RunPaths,
    count_approval_iterations,
    current_approval_iteration,
    list_approval_artifacts,
    make_revision_record,
    next_approval_iteration,
    prepare_run_dirs,
    write_approval_iteration,
    write_iteration_decisions,
    write_manifest,
)
from paper2repo_agent.config import Settings
from paper2repo_agent.evidence import (
    collect_evidence_refs,
    collect_plan_semantic_evidence_refs,
    write_evidence_databases,
    write_evidence_files,
)
from paper2repo_agent.graphs.execute import run_execute
from paper2repo_agent.graphs.plan import run_plan
from paper2repo_agent.hitl import apply_human_decisions_to_plan
from paper2repo_agent.llm import revise_plan_with_llm
from paper2repo_agent.review import build_human_review, render_human_review_markdown
from paper2repo_agent.schemas import (
    ApprovedPlan,
    Evidence,
    EvidenceRef,
    EvidenceStore,
    HumanDecision,
    HumanDecisions,
    HumanReview,
    PaperSpec,
    ReproductionPlan,
    RunManifest,
    SpecValidationResult,
    UserConstraints,
)
from paper2repo_agent.utils import (
    load_jsonl,
    read_json,
    sha256_text,
    utc_now,
    write_json,
)

app = typer.Typer(help="Paper2Repo planning agent.")
console = Console()


@app.callback()
def main() -> None:
    """Plan machine-learning paper reproduction packages."""


@app.command()
def plan(
    paper_path: Path = typer.Argument(..., exists=True, help="PDF, Markdown, or text paper input."),
    out: Path = typer.Option(..., "--out", "-o", help="Run output directory."),
    source_type: str = typer.Option("auto", "--source-type", help="pdf, markdown, text, or auto."),
    fake_llm: bool = typer.Option(False, "--fake-llm", help="Use deterministic fake LLM output."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress graph progress output."),
    interactive: bool = typer.Option(
        False, "--interactive", help="Run the interactive review loop."
    ),
    approved_by: str | None = typer.Option(
        None, "--approved-by", help="Approver name for interactive approval."
    ),
) -> None:
    settings = Settings.load()
    constraints = (
        _prompt_user_constraints(UserConstraints())
        if interactive
        else UserConstraints(os=_current_os())
    )
    if not quiet:
        console.print(f"[paper2repo] starting plan: input={paper_path} out={out}")
    result = run_plan(
        paper_path=paper_path,
        output_dir=out,
        source_type=source_type,
        fake_llm=fake_llm,
        verbose=not quiet,
        settings=settings,
        user_constraints=constraints,
    )
    console.print(f"[green]Planning complete[/green]: {result['paths'].run_dir}")
    console.print(f"Human review: {result['paths'].human_review_md}")
    if interactive:
        _run_review_loop(
            run_dir=result["paths"].run_dir,
            settings=settings,
            fake_llm=fake_llm,
            quiet=quiet,
            approved_by=approved_by,
        )


@app.command("review")
def review_run(
    run_dir: Path = typer.Argument(..., exists=True, help="Run directory."),
    fake_llm: bool = typer.Option(False, "--fake-llm", help="Use deterministic fake LLM revision."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress revision progress output."),
    approved_by: str | None = typer.Option(None, "--approved-by", help="Approver name."),
) -> None:
    _run_review_loop(
        run_dir=run_dir,
        settings=Settings.load(),
        fake_llm=fake_llm,
        quiet=quiet,
        approved_by=approved_by,
    )


@app.command("inspect-run")
def inspect_run(run_dir: Path = typer.Argument(..., exists=True, help="Run directory.")) -> None:
    manifest = RunManifest.model_validate(read_json(run_dir / "run_manifest.json"))
    table = Table(title=f"Run {manifest.run_id}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Status", manifest.status)
    table.add_row("Approval", manifest.approval_status)
    table.add_row("Iterations", str(manifest.approval_iteration_count))
    table.add_row("Schema", manifest.schema_version)
    table.add_row("PaperSpec", manifest.paper_spec_ref or "")
    table.add_row("Latest Plan", manifest.latest_plan_ref or manifest.draft_plan_ref or "")
    table.add_row("Latest Review", manifest.latest_review_ref or manifest.human_review_ref or "")
    table.add_row("Approved Plan", manifest.approved_plan_ref or "")
    table.add_row("Generated Repo", manifest.generated_repo_ref or "")
    table.add_row("Verification", manifest.verification_result_ref or "")
    table.add_row("Execution Result", manifest.execution_result_ref or "")
    table.add_row("Final Report", manifest.final_report_ref or "")
    evidence_refs = ", ".join(f"{key}={value}" for key, value in manifest.database_refs.items())
    table.add_row("Evidence DB", evidence_refs)
    console.print(table)


@app.command("export-review")
def export_review(
    run_dir: Path = typer.Argument(..., exists=True, help="Run directory."),
    iteration: int | None = typer.Option(None, "--iteration", help="Approval iteration to export."),
) -> None:
    paths = prepare_run_dirs(run_dir)
    if iteration is not None:
        review_path = paths.iteration_human_review_md(iteration)
    elif paths.latest_human_review_md.exists():
        review_path = paths.latest_human_review_md
    else:
        review_path = paths.human_review_md
    console.print(review_path.read_text(encoding="utf-8"))


@app.command()
def approve(
    run_dir: Path = typer.Argument(..., exists=True, help="Run directory."),
    decision_file: Path = typer.Option(
        ...,
        "--decision-file",
        exists=True,
        help="Human decisions JSON.",
    ),
    approved_by: str | None = typer.Option(None, "--approved-by", help="Approver name."),
) -> None:
    paths = prepare_run_dirs(run_dir)
    manifest = RunManifest.model_validate(read_json(paths.manifest))
    plan_obj, review_obj, _ = _load_current_plan_review(paths)
    decision_payload = read_json(decision_file)
    decision_payload.setdefault("review_id", review_obj.id)
    decisions = HumanDecisions.model_validate(decision_payload)
    error = _approval_validation_error(review_obj, decisions)
    if error:
        raise typer.BadParameter(error, param_hint="--decision-file")

    write_json(paths.human_decisions, decisions.model_dump(mode="json"))
    iteration = current_approval_iteration(paths)
    if iteration is None:
        iteration = _initialize_approval_iteration(
            paths=paths,
            plan=plan_obj,
            review=review_obj,
            review_markdown=_load_current_review_markdown(paths),
            model_name="manual",
            notes="Initialized from file-based approve command.",
        )
    decision_ref = write_iteration_decisions(
        paths=paths, iteration=iteration, human_decisions=decisions
    )
    plan_obj = _apply_decisions_to_plan(plan_obj, decisions)
    _write_current_plan(paths, iteration, plan_obj)
    plan_hash = sha256_text(plan_obj.model_dump_json())
    approved = ApprovedPlan(
        plan=plan_obj,
        human_decisions=decisions,
        frozen_at=utc_now(),
        approved_by=approved_by,
        plan_hash=plan_hash,
        execution_allowed=decisions.global_action == "approve",
    )
    write_json(paths.approved_plan, approved.model_dump(mode="json"))
    revision_record = make_revision_record(
        paths=paths,
        iteration=iteration,
        output_plan=plan_obj,
        status="approved" if approved.execution_allowed else "cancelled",
        model_name="manual",
        decision_ref=decision_ref,
        notes=decisions.user_notes,
    )
    write_json(paths.iteration_revision_record(iteration), revision_record.model_dump(mode="json"))
    write_manifest(
        paths=paths,
        run_id=manifest.run_id,
        status="approved" if approved.execution_allowed else "approval_recorded",
        extra_files=[paths.human_decisions, paths.approved_plan, *list_approval_artifacts(paths)],
        approved_plan=approved,
        approval_status=_approval_status_for_action(decisions.global_action),
    )
    console.print(f"[green]Approved plan written[/green]: {paths.approved_plan}")


@app.command()
def execute(
    run_dir: Path = typer.Argument(..., exists=True, help="Run directory with an approved plan."),
    quiet: bool = typer.Option(False, "--quiet", help="Suppress execute graph progress output."),
    resume: bool = typer.Option(
        False,
        "--resume",
        help="Resume from the latest durable checkpoint instead of starting at the entry node.",
    ),
    thread_id: str | None = typer.Option(
        None,
        "--thread-id",
        help="Override the runtime thread ID (normally discovered automatically).",
    ),
) -> None:
    result = run_execute(
        run_dir=run_dir,
        verbose=not quiet,
        resume=resume,
        thread_id=thread_id,
    )
    execution_result = result["execution_result"]
    final_report = result["final_report"]
    console.print(f"[green]Execute complete[/green]: {execution_result.final_status}")
    console.print(f"Generated repo: {execution_result.output_dir}")
    console.print(f"Final report: {run_dir / 'final_report.md'}")
    console.print(final_report.verification_summary)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", "--host", help="Web server host."),
    port: int = typer.Option(8000, "--port", help="Web server port."),
    runs_dir: Path | None = typer.Option(None, "--runs-dir", help="Runs directory."),
    reload: bool = typer.Option(False, "--reload", help="Enable uvicorn reload."),
    access_log: bool = typer.Option(
        False, "--access-log/--no-access-log", help="Show HTTP access logs."
    ),
) -> None:
    import uvicorn

    from paper2repo_agent.web.app import create_app

    console.print(f"[paper2repo] serving web console at http://{host}:{port}")
    if reload and runs_dir is None:
        uvicorn.run(
            "paper2repo_agent.web.app:app",
            host=host,
            port=port,
            reload=True,
            access_log=access_log,
        )
    else:
        web_app = create_app(runs_dir=runs_dir)
        uvicorn.run(web_app, host=host, port=port, reload=False, access_log=access_log)


def _run_review_loop(
    *,
    run_dir: Path,
    settings: Settings,
    fake_llm: bool,
    quiet: bool,
    approved_by: str | None,
) -> None:
    paths = prepare_run_dirs(run_dir)
    manifest = RunManifest.model_validate(read_json(paths.manifest))
    spec = PaperSpec.model_validate(read_json(paths.paper_spec))
    validation = SpecValidationResult.model_validate(read_json(paths.validation_result))
    evidence_store = _load_evidence_store(paths, manifest)

    plan_obj, review_obj, review_markdown = _load_current_plan_review(paths)
    iteration = current_approval_iteration(paths)
    if iteration is None:
        iteration = _initialize_approval_iteration(
            paths=paths,
            plan=plan_obj,
            review=review_obj,
            review_markdown=review_markdown,
            model_name=_model_label(settings, fake_llm),
            notes="Initial review draft.",
        )
        write_manifest(
            paths=paths,
            run_id=manifest.run_id,
            status="planned",
            extra_files=list_approval_artifacts(paths),
            approval_status="awaiting_review",
        )

    while True:
        _print_review_summary(plan_obj, review_obj)
        decisions = _collect_human_decisions(review_obj)
        error = _approval_validation_error(review_obj, decisions)
        if error:
            console.print(f"[red]{error}[/red]")
            continue

        write_json(paths.human_decisions, decisions.model_dump(mode="json"))
        decision_ref = write_iteration_decisions(
            paths=paths,
            iteration=iteration,
            human_decisions=decisions,
        )

        if decisions.global_action == "cancel":
            revision_record = make_revision_record(
                paths=paths,
                iteration=iteration,
                output_plan=plan_obj,
                status="cancelled",
                model_name=_model_label(settings, fake_llm),
                decision_ref=decision_ref,
                notes=decisions.user_notes,
            )
            write_json(
                paths.iteration_revision_record(iteration), revision_record.model_dump(mode="json")
            )
            write_manifest(
                paths=paths,
                run_id=manifest.run_id,
                status="cancelled",
                extra_files=[paths.human_decisions, *list_approval_artifacts(paths)],
                approval_status="cancelled",
            )
            console.print("[yellow]Review cancelled[/yellow]")
            return

        if decisions.global_action == "approve":
            plan_obj = _apply_decisions_to_plan(plan_obj, decisions)
            _write_current_plan(paths, iteration, plan_obj)
            plan_hash = sha256_text(plan_obj.model_dump_json())
            approved = ApprovedPlan(
                plan=plan_obj,
                human_decisions=decisions,
                frozen_at=utc_now(),
                approved_by=approved_by,
                plan_hash=plan_hash,
                execution_allowed=True,
            )
            write_json(paths.approved_plan, approved.model_dump(mode="json"))
            revision_record = make_revision_record(
                paths=paths,
                iteration=iteration,
                output_plan=plan_obj,
                status="approved",
                model_name=_model_label(settings, fake_llm),
                decision_ref=decision_ref,
                notes=decisions.user_notes,
            )
            write_json(
                paths.iteration_revision_record(iteration), revision_record.model_dump(mode="json")
            )
            write_manifest(
                paths=paths,
                run_id=manifest.run_id,
                status="approved",
                extra_files=[
                    paths.human_decisions,
                    paths.approved_plan,
                    *list_approval_artifacts(paths),
                ],
                approved_plan=approved,
                approval_status="approved",
            )
            console.print(f"[green]Approved plan written[/green]: {paths.approved_plan}")
            return

        revised_plan = revise_plan_with_llm(
            current_plan=plan_obj,
            review=review_obj,
            decisions=decisions,
            spec=spec,
            validation=validation,
            constraints=plan_obj.user_constraints,
            settings=settings,
            evidence_store=evidence_store,
            fake_llm=fake_llm,
            progress=None if quiet else lambda message: console.print(f"[paper2repo] {message}"),
        )
        revised_review = build_human_review(revised_plan, validation)
        semantic_refs = collect_plan_semantic_evidence_refs(revised_plan, evidence_store)
        revised_review.evidence_refs = sorted(
            {
                *revised_review.evidence_refs,
                *[ref.evidence_id for ref in semantic_refs],
            }
        )
        revised_markdown = render_human_review_markdown(revised_review, revised_plan)
        _record_revision_evidence_refs(
            paths=paths,
            evidence_store=evidence_store,
            plan_obj=revised_plan,
            review_obj=revised_review,
            semantic_refs=semantic_refs,
        )
        next_iteration = next_approval_iteration(paths)
        revision_record = make_revision_record(
            paths=paths,
            iteration=next_iteration,
            output_plan=revised_plan,
            status="revised",
            model_name=_model_label(settings, fake_llm),
            input_plan=plan_obj,
            decision_ref=decision_ref,
            notes=decisions.user_notes,
        )
        write_approval_iteration(
            paths=paths,
            iteration=next_iteration,
            plan=revised_plan,
            human_review=revised_review,
            human_review_markdown=revised_markdown,
            revision_record=revision_record,
        )
        write_manifest(
            paths=paths,
            run_id=manifest.run_id,
            status="planned",
            extra_files=[paths.human_decisions, *list_approval_artifacts(paths)],
            approval_status="awaiting_review",
            approval_iteration_count=count_approval_iterations(paths),
        )
        plan_obj = revised_plan
        review_obj = revised_review
        review_markdown = revised_markdown
        iteration = next_iteration


def _prompt_user_constraints(defaults: UserConstraints) -> UserConstraints:
    initial_request = Prompt.ask(
        "Initial reproduction request",
        default=defaults.initial_request or "",
    )
    compute_target = Prompt.ask(
        "Compute target",
        choices=["cpu_smoke", "single_gpu", "multi_gpu"],
        default=defaults.compute_target,
    )
    gpu_model = ""
    gpu_memory_gb = None
    if compute_target != "cpu_smoke":
        gpu_model = Prompt.ask("GPU model", default=defaults.gpu_model or "")
        gpu_memory_gb = IntPrompt.ask("GPU memory GB", default=defaults.gpu_memory_gb or 24)
    return UserConstraints(
        initial_request=initial_request or None,
        reproduction_goal=Prompt.ask(
            "Reproduction goal",
            choices=[
                "spec_only",
                "scaffold_only",
                "smoke_test",
                "small_scale_result",
                "full_metric_attempt",
            ],
            default=defaults.reproduction_goal,
        ),
        compute_target=compute_target,
        gpu_model=gpu_model or None,
        gpu_memory_gb=gpu_memory_gb,
        max_runtime_minutes=IntPrompt.ask(
            "Max runtime minutes",
            default=defaults.max_runtime_minutes,
        ),
        os=_current_os(),
        python_version=Prompt.ask("Python version", default=defaults.python_version),
        package_manager=Prompt.ask(
            "Package manager",
            choices=["pip", "uv", "poetry", "conda"],
            default=defaults.package_manager,
        ),
        allow_network=Confirm.ask(
            "Allow network access during execution planning", default=defaults.allow_network
        ),
        allow_large_downloads=Confirm.ask(
            "Allow large downloads during execution planning",
            default=defaults.allow_large_downloads,
        ),
        dataset_policy=Prompt.ask(
            "Dataset policy",
            choices=["synthetic_only", "user_provided", "download_allowed"],
            default=defaults.dataset_policy,
        ),
    )


def _collect_human_decisions(review: HumanReview) -> HumanDecisions:
    decisions: list[HumanDecision] = []
    for item in review.confirmation_items:
        console.print("")
        console.print(f"[bold]{item.field_path}[/bold]")
        console.print(item.question)
        console.print(f"Suggested value: {item.suggested_value}")
        if item.options:
            console.print("Options:")
            for index, option in enumerate(item.options, start=1):
                console.print(f"  [{index}] {json.dumps(option, ensure_ascii=False)}")
        action = Prompt.ask(
            "Decision",
            choices=["accept", "override", "defer", "reject"],
            default="accept",
        )
        value = item.suggested_value if action == "accept" else None
        if action == "override":
            if item.options:
                source = Prompt.ask(
                    "Override source",
                    choices=["option", "custom"],
                    default="option",
                )
                if source == "option":
                    choice = IntPrompt.ask(
                        "Select option number",
                        default=1,
                    )
                    option_index = max(1, min(choice, len(item.options))) - 1
                    value = item.options[option_index]
                else:
                    default_value = json.dumps(item.suggested_value, ensure_ascii=False)
                    value = Prompt.ask("Override value (JSON or plain text)", default=default_value)
            else:
                default_value = json.dumps(item.suggested_value, ensure_ascii=False)
                value = Prompt.ask("Override value (JSON or plain text)", default=default_value)
        note = Prompt.ask("Note", default="")
        decisions.append(
            HumanDecision(
                item_id=item.field_path,
                action=action,
                value=value,
                note=note or None,
            )
        )

    console.print("")
    default_action = "approve"
    if review.recommended_action in {"revise", "downgrade_scope"}:
        default_action = "revise"
    if review.recommended_action == "cancel":
        default_action = "cancel"
    global_action = Prompt.ask(
        "Global action",
        choices=["approve", "revise", "cancel"],
        default=default_action,
    )
    user_notes = Prompt.ask("Global notes", default="")
    return HumanDecisions(
        review_id=review.id,
        decisions=decisions,
        global_action=global_action,
        user_notes=user_notes or None,
    )


def _approval_validation_error(review: HumanReview, decisions: HumanDecisions) -> str | None:
    if decisions.global_action != "approve":
        return None
    decision_map = {decision.item_id: decision for decision in decisions.decisions}
    for item in review.confirmation_items:
        decision = decision_map.get(item.field_path)
        if decision is None:
            return f"Missing decision for confirmation item: {item.field_path}"
        if item.blocks_execution_if_unanswered and decision.action in {"defer", "reject"}:
            return f"Blocking confirmation item requires accept or override: {item.field_path}"
        if decision.action == "override" and (decision.value is None or decision.value == ""):
            return f"Override requires a value: {item.field_path}"
    return None


def _apply_decisions_to_plan(
    plan_obj: ReproductionPlan,
    decisions: HumanDecisions,
) -> ReproductionPlan:
    return apply_human_decisions_to_plan(plan_obj, decisions)


def _write_current_plan(
    paths: RunPaths,
    iteration: int,
    plan_obj: ReproductionPlan,
) -> None:
    write_json(paths.iteration_plan(iteration), plan_obj.model_dump(mode="json"))
    write_json(paths.latest_plan, plan_obj.model_dump(mode="json"))


def _safe_int(value: object, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _print_review_summary(plan_obj: ReproductionPlan, review_obj: HumanReview) -> None:
    table = Table(title=f"Review {plan_obj.paper_title}")
    table.add_column("Field")
    table.add_column("Value")
    table.add_row("Plan ID", plan_obj.id)
    table.add_row("Goal", plan_obj.objective.goal)
    table.add_row("Compute", plan_obj.user_constraints.compute_target)
    table.add_row("Dataset", plan_obj.user_constraints.dataset_policy)
    table.add_row("Recommended", review_obj.recommended_action)
    table.add_row("Confirmations", str(len(review_obj.confirmation_items)))
    table.add_row("Risks", str(len(review_obj.risks)))
    console.print(table)


def _initialize_approval_iteration(
    *,
    paths: RunPaths,
    plan: ReproductionPlan,
    review: HumanReview,
    review_markdown: str,
    model_name: str,
    notes: str | None,
) -> int:
    iteration = next_approval_iteration(paths)
    revision_record = make_revision_record(
        paths=paths,
        iteration=iteration,
        output_plan=plan,
        status="drafted",
        model_name=model_name,
        input_plan=None,
        notes=notes,
    )
    write_approval_iteration(
        paths=paths,
        iteration=iteration,
        plan=plan,
        human_review=review,
        human_review_markdown=review_markdown,
        revision_record=revision_record,
    )
    return iteration


def _load_current_plan_review(paths: RunPaths) -> tuple[ReproductionPlan, HumanReview, str]:
    if paths.latest_plan.exists():
        plan_path = paths.latest_plan
        review_json_path = paths.latest_human_review_json
        review_md_path = paths.latest_human_review_md
    else:
        plan_path = paths.draft_plan
        review_json_path = paths.human_review_json
        review_md_path = paths.human_review_md
    return (
        ReproductionPlan.model_validate(read_json(plan_path)),
        HumanReview.model_validate(read_json(review_json_path)),
        review_md_path.read_text(encoding="utf-8"),
    )


def _load_current_review_markdown(paths: RunPaths) -> str:
    if paths.latest_human_review_md.exists():
        return paths.latest_human_review_md.read_text(encoding="utf-8")
    return paths.human_review_md.read_text(encoding="utf-8")


def _load_evidence_store(paths: RunPaths, manifest: RunManifest) -> EvidenceStore:
    evidence_rows = load_jsonl(paths.evidence_store)
    ref_rows = load_jsonl(paths.evidence_refs)
    evidence_items = [Evidence.model_validate(row) for row in evidence_rows]
    refs = [EvidenceRef.model_validate(row) for row in ref_rows]
    paper_id = evidence_items[0].paper_id if evidence_items else "paper"
    return EvidenceStore(
        run_id=manifest.run_id,
        paper_id=paper_id,
        items=evidence_items,
        refs=refs,
    )


def _record_revision_evidence_refs(
    *,
    paths: RunPaths,
    evidence_store: EvidenceStore,
    plan_obj: ReproductionPlan,
    review_obj: HumanReview,
    semantic_refs: list[EvidenceRef],
) -> None:
    refs = [
        *evidence_store.refs,
        *collect_evidence_refs(
            plan_obj,
            artifact_type="plan",
            artifact_id=plan_obj.id,
            created_by="validator",
        ),
        *semantic_refs,
        *collect_evidence_refs(
            review_obj,
            artifact_type="human_review",
            artifact_id=review_obj.id,
            created_by="validator",
        ),
    ]
    evidence_store.refs = list({ref.id: ref for ref in refs}.values())
    write_evidence_files(evidence_store, paths.planning_dir)
    write_evidence_databases(
        evidence_store,
        paths.evidence_sqlite,
        settings=Settings.load(),
        refs_path=paths.evidence_database_refs,
    )


def _model_label(settings: Settings, fake_llm: bool) -> str:
    return "fake-llm" if fake_llm else settings.model_name


def _current_os() -> str:
    value = platform.system().lower()
    if "windows" in value:
        return "windows"
    if "linux" in value:
        return "linux"
    if "darwin" in value or "mac" in value:
        return "macos"
    return "unknown"


def _approval_status_for_action(global_action: str) -> str:
    if global_action == "approve":
        return "approved"
    if global_action == "revise":
        return "revision_requested"
    return "cancelled"


if __name__ == "__main__":
    app()
