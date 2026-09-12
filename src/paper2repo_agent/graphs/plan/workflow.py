from __future__ import annotations

from pathlib import Path
from collections.abc import Callable

from paper2repo_agent.config import Settings
from paper2repo_agent.graphs.plan.nodes import (
    build_evidence_store_node,
    build_human_review_node,
    draft_plan_node,
    extract_spec_with_llm_node,
    load_input_node,
    parse_paper_node,
    propose_hitl_fields_node,
    validate_spec_node,
    write_artifacts_node,
    write_manifest_node,
)
from paper2repo_agent.graphs.plan.state import PlanGraphState
from paper2repo_agent.schemas import UserConstraints


def _load_graph_dependencies():
    from langgraph.graph import END, StateGraph

    return {"END": END, "StateGraph": StateGraph}


def build_plan_graph():
    deps = _load_graph_dependencies()
    StateGraph = deps["StateGraph"]
    END = deps["END"]

    workflow = StateGraph(PlanGraphState)
    workflow.add_node("load_input", load_input_node)
    workflow.add_node("parse_paper", parse_paper_node)
    workflow.add_node("build_evidence_store", build_evidence_store_node)
    workflow.add_node("extract_spec_with_llm", extract_spec_with_llm_node)
    workflow.add_node("validate_spec", validate_spec_node)
    workflow.add_node("draft_plan", draft_plan_node)
    workflow.add_node("propose_hitl_fields", propose_hitl_fields_node)
    workflow.add_node("build_human_review", build_human_review_node)
    workflow.add_node("write_artifacts", write_artifacts_node)
    workflow.add_node("write_manifest", write_manifest_node)

    workflow.set_entry_point("load_input")
    workflow.add_edge("load_input", "parse_paper")
    workflow.add_edge("parse_paper", "build_evidence_store")
    workflow.add_edge("build_evidence_store", "extract_spec_with_llm")
    workflow.add_edge("extract_spec_with_llm", "validate_spec")
    workflow.add_edge("validate_spec", "draft_plan")
    workflow.add_edge("draft_plan", "propose_hitl_fields")
    workflow.add_edge("propose_hitl_fields", "build_human_review")
    workflow.add_edge("build_human_review", "write_artifacts")
    workflow.add_edge("write_artifacts", "write_manifest")
    workflow.add_edge("write_manifest", END)
    return workflow.compile()


_plan_app = None


def get_plan_app():
    global _plan_app
    if _plan_app is None:
        _plan_app = build_plan_graph()
    return _plan_app


def run_plan(
    paper_path: Path,
    output_dir: Path,
    *,
    source_type: str = "auto",
    fake_llm: bool = False,
    verbose: bool = True,
    settings: Settings | None = None,
    user_constraints: UserConstraints | None = None,
    progress_callback: Callable[[str], None] | None = None,
) -> PlanGraphState:
    app = get_plan_app()
    initial: PlanGraphState = {
        "paper_path": str(paper_path),
        "source_type": source_type,
        "output_dir": str(output_dir),
        "fake_llm": fake_llm,
        "verbose": verbose,
        "settings": settings or Settings.load(),
        "user_constraints": user_constraints or UserConstraints(),
        "progress_callback": progress_callback,
    }
    return app.invoke(initial)
