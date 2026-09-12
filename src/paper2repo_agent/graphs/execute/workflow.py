from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from paper2repo_agent.graphs.execute.nodes import (
    build_codebase_design_node,
    build_execution_coding_plan_node,
    build_execution_policy_node,
    build_file_generation_tasks_node,
    build_failure_digest_node,
    build_patch_plan_node,
    build_plan_feedback_node,
    build_repair_tasks_node,
    build_verification_commands_node,
    critique_coding_plan_node,
    generate_files_by_layer_node,
    load_approved_plan_node,
    plan_file_generation_dag_node,
    plan_rollback_gate_node,
    prepare_execution_workspace_node,
    repair_files_if_needed_node,
    run_contract_verification_node,
    run_static_verification_node,
    write_code_trace_node,
    write_cost_report_node,
    write_execution_artifacts_node,
    write_execution_result_node,
    write_final_report_node,
)
from paper2repo_agent.graphs.execute.state import ExecuteGraphState


def _load_graph_dependencies():
    from langgraph.graph import END, StateGraph

    return {"END": END, "StateGraph": StateGraph}


def build_execute_graph(*, checkpointer=None):
    deps = _load_graph_dependencies()
    StateGraph = deps["StateGraph"]
    END = deps["END"]

    workflow = StateGraph(ExecuteGraphState)
    workflow.add_node("load_approved_plan", load_approved_plan_node)
    workflow.add_node("build_execution_policy", build_execution_policy_node)
    workflow.add_node("prepare_execution_workspace", prepare_execution_workspace_node)
    workflow.add_node("build_codebase_design", build_codebase_design_node)
    workflow.add_node("build_execution_coding_plan", build_execution_coding_plan_node)
    workflow.add_node("critique_coding_plan", critique_coding_plan_node)
    workflow.add_node("build_file_generation_tasks", build_file_generation_tasks_node)
    workflow.add_node("plan_file_generation_dag", plan_file_generation_dag_node)
    workflow.add_node("generate_files_by_layer", generate_files_by_layer_node)
    workflow.add_node("build_verification_commands", build_verification_commands_node)
    workflow.add_node("run_static_verification", run_static_verification_node)
    workflow.add_node("run_contract_verification", run_contract_verification_node)
    workflow.add_node("build_failure_digest", build_failure_digest_node)
    workflow.add_node("build_repair_tasks", build_repair_tasks_node)
    workflow.add_node("build_patch_plan", build_patch_plan_node)
    workflow.add_node("repair_files_if_needed", repair_files_if_needed_node)
    workflow.add_node("write_code_trace", write_code_trace_node)
    workflow.add_node("write_cost_report", write_cost_report_node)
    workflow.add_node("write_execution_result", write_execution_result_node)
    workflow.add_node("build_plan_feedback", build_plan_feedback_node)
    workflow.add_node("plan_rollback_gate", plan_rollback_gate_node)
    workflow.add_node("write_final_report", write_final_report_node)
    workflow.add_node("write_execution_artifacts", write_execution_artifacts_node)

    workflow.set_entry_point("load_approved_plan")
    workflow.add_edge("load_approved_plan", "build_execution_policy")
    workflow.add_edge("build_execution_policy", "prepare_execution_workspace")
    workflow.add_edge("prepare_execution_workspace", "build_codebase_design")
    workflow.add_edge("build_codebase_design", "build_execution_coding_plan")
    workflow.add_edge("build_execution_coding_plan", "critique_coding_plan")
    workflow.add_edge("critique_coding_plan", "build_file_generation_tasks")
    workflow.add_edge("build_file_generation_tasks", "plan_file_generation_dag")
    workflow.add_edge("plan_file_generation_dag", "generate_files_by_layer")
    workflow.add_edge("generate_files_by_layer", "build_verification_commands")
    workflow.add_edge("build_verification_commands", "run_static_verification")
    workflow.add_edge("run_static_verification", "run_contract_verification")
    workflow.add_edge("run_contract_verification", "build_failure_digest")
    workflow.add_edge("build_failure_digest", "build_repair_tasks")
    workflow.add_edge("build_repair_tasks", "build_patch_plan")
    workflow.add_edge("build_patch_plan", "repair_files_if_needed")
    workflow.add_edge("repair_files_if_needed", "write_code_trace")
    workflow.add_edge("write_code_trace", "write_cost_report")
    workflow.add_edge("write_cost_report", "write_execution_result")
    # ── P3: Planning-Execution feedback + rollback gate ──────────────
    workflow.add_edge("write_execution_result", "build_plan_feedback")
    workflow.add_edge("build_plan_feedback", "plan_rollback_gate")
    workflow.add_conditional_edges(
        "plan_rollback_gate",
        _route_after_rollback_gate,
        {
            "build_file_generation_tasks": "build_file_generation_tasks",
            "write_final_report": "write_final_report",
        },
    )
    workflow.add_edge("write_final_report", "write_execution_artifacts")
    workflow.add_edge("write_execution_artifacts", END)
    return workflow.compile(checkpointer=checkpointer)


def _route_after_rollback_gate(state: ExecuteGraphState) -> str:
    """Conditional edge: loop back to file generation on rollback, else finalize."""
    if state.get("rollback_triggered"):
        return "build_file_generation_tasks"
    return "write_final_report"


_execute_app = None


def get_execute_app():
    global _execute_app
    if _execute_app is None:
        _execute_app = build_execute_graph()
    return _execute_app


def run_execute(
    run_dir: Path,
    *,
    verbose: bool = True,
    progress_callback: Callable[[str], None] | None = None,
    thread_id: str | None = None,
    resume: bool = False,
    durable: bool = True,
) -> ExecuteGraphState:
    """Execute or resume a run with durable node-level checkpoints.

    ``thread_id`` is stable for a given run directory by default. A failed run
    can therefore be continued with ``resume=True`` without replaying completed
    graph nodes. Set ``durable=False`` only for tests or disposable runs.
    """
    initial: ExecuteGraphState = {
        "run_dir": str(run_dir),
        "verbose": verbose,
    }
    if not durable:
        from paper2repo_agent.runtime.context import bind_progress_callback

        with bind_progress_callback(progress_callback):
            return get_execute_app().invoke(initial)

    from paper2repo_agent.runtime.checkpoint import (
        checkpoint_path,
        derive_thread_id,
        open_sqlite_checkpointer,
        read_runtime_record,
        write_runtime_record,
    )
    from paper2repo_agent.runtime.context import bind_progress_callback

    resolved_run_dir = run_dir.resolve()
    previous_record = read_runtime_record(resolved_run_dir)
    if resume and not checkpoint_path(resolved_run_dir).exists():
        raise FileNotFoundError(
            f"No durable checkpoint exists for run directory: {resolved_run_dir}"
        )
    if previous_record and not resume and thread_id is None:
        raise RuntimeError(
            "This run directory already has runtime state. Use --resume to continue it, "
            "or provide a new --thread-id for an intentional fresh attempt."
        )
    recorded_thread_id = previous_record.get("thread_id") if previous_record else None
    resolved_thread_id = thread_id or recorded_thread_id or derive_thread_id(resolved_run_dir)
    config = {"configurable": {"thread_id": resolved_thread_id}}
    write_runtime_record(
        resolved_run_dir,
        thread_id=resolved_thread_id,
        status="RUNNING",
        resume=resume,
    )
    try:
        with open_sqlite_checkpointer(resolved_run_dir) as checkpointer:
            app = build_execute_graph(checkpointer=checkpointer)
            with bind_progress_callback(progress_callback):
                result = app.invoke(None if resume else initial, config=config)
    except BaseException as exc:
        try:
            write_runtime_record(
                resolved_run_dir,
                thread_id=resolved_thread_id,
                status="FAILED",
                resume=resume,
                error=exc,
            )
        except Exception:
            # Lifecycle metadata must never hide the original execution error.
            pass
        raise

    write_runtime_record(
        resolved_run_dir,
        thread_id=resolved_thread_id,
        status="SUCCEEDED",
        resume=resume,
    )
    return result


def resume_execute(
    run_dir: Path,
    *,
    verbose: bool = True,
    progress_callback: Callable[[str], None] | None = None,
    thread_id: str | None = None,
) -> ExecuteGraphState:
    """Resume a previously checkpointed execution from its next pending node."""
    return run_execute(
        run_dir,
        verbose=verbose,
        progress_callback=progress_callback,
        thread_id=thread_id,
        resume=True,
    )
