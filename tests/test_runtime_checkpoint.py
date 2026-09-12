from __future__ import annotations

from pathlib import Path
from typing import TypedDict

import pytest

from paper2repo_agent.runtime.checkpoint import (
    checkpoint_path,
    derive_thread_id,
    open_sqlite_checkpointer,
    read_runtime_record,
    write_runtime_record,
)


def test_thread_id_is_stable_for_run_directory(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    assert derive_thread_id(run_dir) == derive_thread_id(run_dir)
    assert derive_thread_id(run_dir).startswith("execute_")
    assert derive_thread_id(run_dir) != derive_thread_id(tmp_path / "other")


def test_runtime_record_tracks_failure_and_resume(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    thread_id = derive_thread_id(run_dir)

    write_runtime_record(
        run_dir,
        thread_id=thread_id,
        status="RUNNING",
        resume=False,
    )
    failed = write_runtime_record(
        run_dir,
        thread_id=thread_id,
        status="FAILED",
        resume=False,
        error=RuntimeError("API key sk-abcdefghijklmnop must not leak"),
    )
    resumed = write_runtime_record(
        run_dir,
        thread_id=thread_id,
        status="RUNNING",
        resume=True,
    )

    assert failed["resumable"] is True
    assert "sk-abcdefghijklmnop" not in failed["last_error"]
    assert resumed["resume_count"] == 1
    assert read_runtime_record(run_dir) == resumed
    assert checkpoint_path(run_dir).name == "checkpoints.sqlite"


def test_sqlite_checkpoint_resumes_after_last_completed_node(tmp_path: Path) -> None:
    from langgraph.graph import END, StateGraph

    class State(TypedDict, total=False):
        value: int

    calls = {"first": 0, "flaky": 0}

    def first(state: State) -> State:
        calls["first"] += 1
        return {"value": state.get("value", 0) + 1}

    def flaky(state: State) -> State:
        calls["flaky"] += 1
        if calls["flaky"] == 1:
            raise RuntimeError("simulated worker crash")
        return {"value": state["value"] + 1}

    def build(checkpointer):
        workflow = StateGraph(State)
        workflow.add_node("first", first)
        workflow.add_node("flaky", flaky)
        workflow.set_entry_point("first")
        workflow.add_edge("first", "flaky")
        workflow.add_edge("flaky", END)
        return workflow.compile(checkpointer=checkpointer)

    config = {"configurable": {"thread_id": "resume-test"}}
    with open_sqlite_checkpointer(tmp_path) as saver:
        with pytest.raises(RuntimeError, match="simulated worker crash"):
            build(saver).invoke({"value": 0}, config=config)

    with open_sqlite_checkpointer(tmp_path) as saver:
        result = build(saver).invoke(None, config=config)

    assert result["value"] == 2
    assert calls == {"first": 1, "flaky": 2}
