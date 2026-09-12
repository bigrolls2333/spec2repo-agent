from __future__ import annotations

from pathlib import Path
from typing import Any

from paper2repo_agent.artifacts import prepare_run_dirs, write_manifest, write_planning_artifacts
from paper2repo_agent.evidence import (
    build_evidence_store,
    collect_evidence_refs,
    collect_plan_semantic_evidence_refs,
    write_evidence_databases,
    write_evidence_files,
)
from paper2repo_agent.hitl import build_merged_confirmations
from paper2repo_agent.llm import (
    draft_plan_with_llm,
    extract_spec_with_llm,
    propose_hitl_fields_with_llm,
)
from paper2repo_agent.mineru import MineruClient
from paper2repo_agent.parser import infer_source_type, parse_local_text_file, parse_markdown_text
from paper2repo_agent.planner import align_plan_confirmations
from paper2repo_agent.review import (
    build_decision_template,
    build_human_review,
    render_human_review_markdown,
)
from paper2repo_agent.schemas import PaperInput, SourcePolicy, UserConstraints
from paper2repo_agent.utils import stable_id, utc_now
from paper2repo_agent.validation import validate_spec

from paper2repo_agent.graphs.plan.state import PlanGraphState, progress


def _field_value(field: Any, fallback: Any) -> Any:
    value = getattr(field, "value", None)
    if value is None:
        return fallback
    if isinstance(value, str) and value == "":
        return fallback
    return value


def load_input_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node load_input: preparing run directories and input metadata")
    paper_path = Path(state["paper_path"]).resolve()
    output_dir = Path(state["output_dir"]).resolve()
    source_type = infer_source_type(paper_path, state.get("source_type"))
    run_id = state.get("run_id") or stable_id("run", paper_path.name, utc_now())
    paths = prepare_run_dirs(output_dir)
    constraints = state.get("user_constraints") or UserConstraints()
    source_policy = SourcePolicy(id=stable_id("source_policy", run_id, paper_path.name))
    paper_input = PaperInput(
        source_type=source_type,
        source_path=str(paper_path),
        output_dir=str(output_dir),
        user_constraints=constraints,
        source_policy=source_policy,
    )
    return {
        "run_id": run_id,
        "paths": paths,
        "source_type": source_type,
        "user_constraints": constraints,
        "source_policy": source_policy,
        "paper_input": paper_input,
    }


def parse_paper_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node parse_paper: parsing input paper")
    paper_path = Path(state["paper_path"]).resolve()
    source_type = state["source_type"]
    run_id = state["run_id"]
    settings = state["settings"]
    paths = state["paths"]

    if source_type == "pdf":
        mineru_dir = paths.planning_dir / "mineru"
        markdown_path = MineruClient(
            settings,
            progress=lambda message: progress(state, message),
        ).parse_pdf_to_markdown(paper_path, mineru_dir)
        markdown = markdown_path.read_text(encoding="utf-8", errors="replace")
        paper_id = stable_id("paper", paper_path.name, markdown[:2000])
        parsed = parse_markdown_text(
            markdown,
            paper_id=paper_id,
            title=paper_path.stem,
            source_path=str(paper_path),
        )
        parsed.metadata["source_type"] = "pdf"
        parsed.metadata["mineru_markdown_path"] = str(markdown_path)
    else:
        parsed = parse_local_text_file(
            paper_path,
            source_type,
            paper_id=stable_id("paper", run_id),
        )
    return {"parsed_paper": parsed}


def build_evidence_store_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node build_evidence_store: creating evidence records")
    store = build_evidence_store(state["parsed_paper"], state["run_id"])
    progress(state, f"node build_evidence_store: {len(store.items)} evidence records")
    return {"evidence_store": store}


def extract_spec_with_llm_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node extract_spec_with_llm: extracting PaperSpec")
    store = state["evidence_store"]
    spec = extract_spec_with_llm(
        state["parsed_paper"],
        [item.id for item in store.items],
        state["settings"],
        fake_llm=state.get("fake_llm", False),
        progress=lambda message: progress(state, message),
    )
    spec.source_policy_ref = "planning/source_policy.json"
    progress(
        state,
        "LLM summary [PaperSpec]: "
        f"task={_field_value(spec.task.task_type, 'unknown')}; "
        f"architecture={_field_value(spec.model.architecture_name, 'unknown')}; "
        f"components={len(spec.model.components)}; losses={len(spec.model.losses)}; "
        f"datasets={len(spec.datasets)}.",
    )
    return {"paper_spec": spec}


def validate_spec_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node validate_spec: validating extracted PaperSpec")
    result = validate_spec(state["paper_spec"], state["user_constraints"])
    return {"validation_result": result}


def draft_plan_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node draft_plan: building ReproductionPlan")
    plan = draft_plan_with_llm(
        state["paper_spec"],
        state["validation_result"],
        state["user_constraints"],
        state["settings"],
        evidence_store=state.get("evidence_store"),
        fake_llm=state.get("fake_llm", False),
        progress=lambda message: progress(state, message),
    )
    plan = align_plan_confirmations(plan, state["validation_result"])
    progress(
        state,
        "LLM summary [ReproductionPlan]: "
        f"goal={plan.objective.goal}; scope={plan.scope.dataset_mode}/{plan.scope.device_target}; "
        f"claims={len(plan.selected_claims)}; steps={len(plan.execution_steps)}; "
        f"confirmations={len(plan.required_confirmations)}.",
    )

    # Generate a best-effort planning trace without blocking the run.
    from paper2repo_agent.llm import generate_planning_trace

    planning_trace = generate_planning_trace(
        state["paper_spec"],
        state["validation_result"],
        state["user_constraints"],
        state["settings"],
        run_id=state["run_id"],
        plan_id=plan.id,
        evidence_store=state.get("evidence_store"),
        fake_llm=state.get("fake_llm", False),
        progress=lambda message: progress(state, message),
    )
    result: dict[str, Any] = {"draft_plan": plan}
    if planning_trace is not None:
        result["planning_trace"] = planning_trace
    return result


def propose_hitl_fields_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node propose_hitl_fields: asking LLM for task-specific confirmations")
    proposals = propose_hitl_fields_with_llm(
        state["paper_spec"],
        state["validation_result"],
        state["draft_plan"],
        state["user_constraints"],
        state["settings"],
        evidence_store=state.get("evidence_store"),
        fake_llm=state.get("fake_llm", False),
        progress=lambda message: progress(state, message),
    )
    validation = state["validation_result"].model_copy(deep=True)
    validation.required_confirmations = build_merged_confirmations(
        validation,
        state["user_constraints"],
        proposals,
        run_id=state["run_id"],
        evidence_store=state.get("evidence_store"),
    )
    plan = align_plan_confirmations(state["draft_plan"].model_copy(deep=True), validation)
    progress(
        state,
        f"node propose_hitl_fields: {len(validation.required_confirmations)} confirmation items",
    )
    proposal_items = list(getattr(proposals, "proposals", []) or [])
    proposal_fields = [
        getattr(item, "field_path", "") or getattr(item, "item_id", "")
        for item in proposal_items[:6]
    ]
    progress(
        state,
        "LLM summary [HITL proposals]: "
        f"llm_proposals={len(proposal_items)}; merged_confirmations={len(validation.required_confirmations)}; "
        f"fields={', '.join([field for field in proposal_fields if field]) or 'none'}.",
    )
    return {
        "confirmation_proposals": proposals,
        "validation_result": validation,
        "draft_plan": plan,
    }


def build_human_review_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node build_human_review: rendering review artifacts")
    review = build_human_review(state["draft_plan"], state["validation_result"])
    semantic_refs = collect_plan_semantic_evidence_refs(
        state["draft_plan"],
        state["evidence_store"],
    )
    review.evidence_refs = sorted(
        {
            *review.evidence_refs,
            *[ref.evidence_id for ref in semantic_refs],
        }
    )
    markdown = render_human_review_markdown(review, state["draft_plan"])
    template = build_decision_template(review)
    return {
        "human_review": review,
        "human_review_markdown": markdown,
        "approval_decision_template": template,
    }


def write_artifacts_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node write_artifacts: writing planning artifacts")
    store = state["evidence_store"]
    store.refs = [
        *collect_evidence_refs(
            state["paper_spec"],
            artifact_type="paper_spec",
            artifact_id=state["paper_spec"].id,
            created_by="llm",
        ),
        *collect_evidence_refs(
            state["validation_result"],
            artifact_type="validation_result",
            artifact_id=state["validation_result"].id,
            created_by="validator",
        ),
        *collect_evidence_refs(
            state["draft_plan"],
            artifact_type="plan",
            artifact_id=state["draft_plan"].id,
            created_by="validator",
        ),
        *collect_plan_semantic_evidence_refs(
            state["draft_plan"],
            store,
        ),
        *collect_evidence_refs(
            state["human_review"],
            artifact_type="human_review",
            artifact_id=state["human_review"].id,
            created_by="validator",
        ),
    ]
    paths = state["paths"]
    write_planning_artifacts(
        paths=paths,
        paper_input=state["paper_input"],
        parsed_paper=state["parsed_paper"],
        source_policy=state["source_policy"],
        paper_spec=state["paper_spec"],
        validation_result=state["validation_result"],
        user_constraints=state["user_constraints"],
        draft_plan=state["draft_plan"],
        human_review=state["human_review"],
        human_review_markdown=state["human_review_markdown"],
        approval_decision_template=state["approval_decision_template"],
    )
    # Persist the planning trace when one was generated.
    if state.get("planning_trace"):
        from paper2repo_agent.utils import write_json

        write_json(
            paths.planning_trace,
            state["planning_trace"].model_dump(mode="json"),
        )
        progress(
            state,
            f"node write_artifacts: planning_trace written ({len(state['planning_trace'].steps)} steps)",
        )
    write_evidence_files(store, paths.planning_dir)
    database_write = write_evidence_databases(
        store,
        paths.evidence_sqlite,
        settings=state["settings"],
        refs_path=paths.evidence_database_refs,
    )
    for warning in database_write.warnings:
        progress(state, f"node write_artifacts: {warning}")
    progress(state, f"node write_artifacts: artifacts written to {paths.run_dir}")
    return {
        "evidence_store": store,
        "manifest_status": "planned",
        "evidence_database_refs": database_write.database_refs,
        "evidence_database_warnings": database_write.warnings,
    }


def write_manifest_node(state: PlanGraphState) -> dict[str, Any]:
    progress(state, "node write_manifest: writing run_manifest.json")
    write_manifest(
        paths=state["paths"],
        run_id=state["run_id"],
        status=state.get("manifest_status", "planned"),
        approval_status="awaiting_review",
    )
    return {}
