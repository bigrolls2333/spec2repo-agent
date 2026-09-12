"""Durable checkpoint and lifecycle metadata for graph executions.

The runtime intentionally stays small: LangGraph owns node-level checkpoints,
while this module owns the stable thread identity and a human-readable run
record. Queueing, distributed leases and sandboxing belong to later layers.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from hashlib import sha256
from pathlib import Path
from typing import Any

from paper2repo_agent.security import redact_text
from paper2repo_agent.utils import read_json, utc_now, write_json


RUNTIME_SCHEMA_VERSION = 1


class RuntimeDependencyError(RuntimeError):
    """Raised when durable runtime support was not installed."""


def runtime_dir(run_dir: Path) -> Path:
    return run_dir.resolve() / "runtime"


def checkpoint_path(run_dir: Path) -> Path:
    return runtime_dir(run_dir) / "checkpoints.sqlite"


def runtime_record_path(run_dir: Path) -> Path:
    return runtime_dir(run_dir) / "run.json"


def derive_thread_id(run_dir: Path, *, phase: str = "execute") -> str:
    """Return a stable, path-independent-looking identity for one graph run."""
    normalized = str(run_dir.resolve()).casefold().encode("utf-8")
    digest = sha256(normalized).hexdigest()[:20]
    return f"{phase}_{digest}"


def read_runtime_record(run_dir: Path) -> dict[str, Any] | None:
    path = runtime_record_path(run_dir)
    if not path.exists():
        return None
    payload = read_json(path)
    return payload if isinstance(payload, dict) else None


def write_runtime_record(
    run_dir: Path,
    *,
    thread_id: str,
    status: str,
    resume: bool,
    error: BaseException | None = None,
) -> dict[str, Any]:
    """Upsert a compact lifecycle record next to the checkpoint database."""
    root = runtime_dir(run_dir)
    root.mkdir(parents=True, exist_ok=True)
    previous = read_runtime_record(run_dir) or {}
    now = utc_now()
    record: dict[str, Any] = {
        "schema_version": RUNTIME_SCHEMA_VERSION,
        "phase": "execute",
        "thread_id": thread_id,
        "status": status,
        "checkpoint_db": "runtime/checkpoints.sqlite",
        "created_at": previous.get("created_at", now),
        "updated_at": now,
        "resume_count": int(previous.get("resume_count", 0)) + (1 if resume else 0),
        "resumable": status in {"RUNNING", "FAILED"},
    }
    if status == "RUNNING":
        record["started_at"] = now
    elif previous.get("started_at"):
        record["started_at"] = previous["started_at"]
    if status == "SUCCEEDED":
        record["completed_at"] = now
    if error is not None:
        record["last_error"] = redact_text(f"{type(error).__name__}: {error}")[:2000]
    elif previous.get("last_error") and status != "SUCCEEDED":
        record["last_error"] = previous["last_error"]
    write_json(runtime_record_path(run_dir), record)
    return record


@contextmanager
def open_sqlite_checkpointer(run_dir: Path) -> Iterator[Any]:
    """Open the SQLite saver for the lifetime of a compiled graph invocation."""
    try:
        from langgraph.checkpoint.sqlite import SqliteSaver
    except ImportError as exc:  # pragma: no cover - depends on installation extras
        raise RuntimeDependencyError(
            "Durable execution requires 'langgraph-checkpoint-sqlite'. "
            "Install the project dependencies and retry."
        ) from exc

    path = checkpoint_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with SqliteSaver.from_conn_string(str(path)) as saver:
        saver.setup()
        yield saver
