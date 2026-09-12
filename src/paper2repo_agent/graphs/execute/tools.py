from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from paper2repo_agent.artifacts import RunPaths, prepare_run_dirs, write_manifest
from paper2repo_agent.evidence import query_evidence_by_field
from paper2repo_agent.schemas import (
    ApprovedPlan,
    CommandResult,
    CommandSpec,
    Evidence,
    EvidenceRef,
    EvidenceStore,
    ExecutionPolicy,
    ExecutionToolSpec,
    RunManifest,
    SourcePolicy,
    ToolCallRecord,
)
from paper2repo_agent.security import redact_mapping, redact_text
from paper2repo_agent.utils import (
    ensure_dir,
    load_jsonl,
    read_json,
    sha256_text,
    stable_id,
    utc_now,
    write_json,
    write_jsonl,
)


def default_tool_specs() -> dict[str, ExecutionToolSpec]:
    """Return all 22 tool specs with human-readable + JSON Schema interfaces.

    Every tool carries ``input_json_schema`` so the agent path
    can convert them to LangChain ``StructuredTool`` for LLM function
    calling. The existing ``input_schema`` / ``output_schema`` string
    fields remain for human documentation and audit trails.
    """
    SCHEMA_STRING = {"type": "string"}
    SCHEMA_INT = {"type": "integer"}
    return {
        # ── existing (9) with JSON Schema backfill ──────────────────
        "load_execution_context": ExecutionToolSpec(
            name="load_execution_context",
            description="Load approved execution context from planning and approval artifacts.",
            input_schema="run_dir: str",
            output_schema="paths, manifest, approved_plan, paper_spec, validation_result, evidence_store, source_policy",
            input_json_schema={
                "type": "object",
                "properties": {"run_dir": SCHEMA_STRING},
                "required": ["run_dir"],
            },
            risk_level="low",
            allowed_phases=["context"],
            writes_state=True,
        ),
        "query_evidence_by_field": ExecutionToolSpec(
            name="query_evidence_by_field",
            description="Query evidence rows by field path from the evidence store.",
            input_schema="sqlite_path: str, field_path: str",
            output_schema="list[evidence row]",
            input_json_schema={
                "type": "object",
                "properties": {"sqlite_path": SCHEMA_STRING, "field_path": SCHEMA_STRING},
                "required": ["sqlite_path", "field_path"],
            },
            risk_level="low",
            allowed_phases=["context", "generation", "repair", "report"],
        ),
        "query_evidence_by_id": ExecutionToolSpec(
            name="query_evidence_by_id",
            description="Load evidence items by id from the in-memory evidence store.",
            input_schema="evidence_ids: str | list[str]",
            output_schema="list[evidence item]",
            input_json_schema={
                "type": "object",
                "properties": {
                    "evidence_ids": {
                        "oneOf": [SCHEMA_STRING, {"type": "array", "items": SCHEMA_STRING}],
                    },
                },
                "required": ["evidence_ids"],
            },
            risk_level="low",
            allowed_phases=["context", "generation", "repair", "report"],
        ),
        "ensure_repo_dirs": ExecutionToolSpec(
            name="ensure_repo_dirs",
            description="Create the generated repository directory layout.",
            input_schema="repo_root: str, package_name: str",
            output_schema="repo_root: str",
            input_json_schema={
                "type": "object",
                "properties": {"repo_root": SCHEMA_STRING, "package_name": SCHEMA_STRING},
                "required": ["repo_root", "package_name"],
            },
            risk_level="low",
            allowed_phases=["generation"],
            writes_files=True,
        ),
        "write_repo_file": ExecutionToolSpec(
            name="write_repo_file",
            description="Write one file under generated_repo. Use this to write your final generated code.",
            input_schema="rel_path: str, content: str",
            output_schema="path: str",
            input_json_schema={
                "type": "object",
                "properties": {
                    "rel_path": {
                        "type": "string",
                        "description": "Relative path under generated_repo, e.g. src/pkg/model.py",
                    },
                    "content": {
                        "type": "string",
                        "description": "Complete file content (Python source code)",
                    },
                },
                "required": ["rel_path", "content"],
            },
            risk_level="medium",
            allowed_phases=["generation", "repair"],
            writes_files=True,
        ),
        "write_repo_files_batch": ExecutionToolSpec(
            name="write_repo_files_batch",
            description="Write a bounded batch of files under generated_repo.",
            input_schema="files: dict[str, str]",
            output_schema="paths: list[str]",
            input_json_schema={
                "type": "object",
                "properties": {
                    "files": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                        "description": "Mapping of relative paths to file contents",
                    },
                },
                "required": ["files"],
            },
            risk_level="medium",
            allowed_phases=["generation", "repair"],
            writes_files=True,
        ),
        "run_command": ExecutionToolSpec(
            name="run_command",
            description="Run a policy-allowed structured command in generated_repo.",
            input_schema="command_spec: CommandSpec",
            output_schema="CommandResult",
            input_json_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "array", "items": SCHEMA_STRING},
                    "cwd": SCHEMA_STRING,
                    "purpose": SCHEMA_STRING,
                },
                "required": ["command", "cwd"],
            },
            risk_level="high",
            allowed_phases=["verification", "repair"],
            writes_state=True,
        ),
        "write_execution_artifact": ExecutionToolSpec(
            name="write_execution_artifact",
            description="Write a JSON, JSONL, or Markdown artifact under run_dir.",
            input_schema="path: str, payload: Any, format: json|jsonl|text",
            output_schema="path: str",
            input_json_schema={
                "type": "object",
                "properties": {
                    "path": SCHEMA_STRING,
                    "payload": {
                        "description": "Content to write (dict for json, list for jsonl, str for text)"
                    },
                    "format": {"type": "string", "enum": ["json", "jsonl", "text"]},
                },
                "required": ["path", "payload", "format"],
            },
            risk_level="low",
            allowed_phases=["generation", "verification", "repair", "report", "finalize"],
            writes_files=True,
        ),
        "update_manifest": ExecutionToolSpec(
            name="update_manifest",
            description="Update run_manifest.json with execution refs and hashes.",
            input_schema="status: str, approval_status: str, approval_iteration_count: int, extra_files: list[str]",
            output_schema="RunManifest",
            input_json_schema={
                "type": "object",
                "properties": {
                    "status": SCHEMA_STRING,
                    "approval_status": SCHEMA_STRING,
                    "approval_iteration_count": SCHEMA_INT,
                    "extra_files": {"type": "array", "items": SCHEMA_STRING},
                },
                "required": [
                    "status",
                    "approval_status",
                    "approval_iteration_count",
                    "extra_files",
                ],
            },
            risk_level="medium",
            allowed_phases=["finalize"],
            writes_files=True,
            writes_state=True,
        ),
        # ── A. File reading ─────────────────────────────────────────
        "read_generated_file": ExecutionToolSpec(
            name="read_generated_file",
            description="Read an already-generated repository file. Use mode='api_summary' to get public signatures only, or 'full' for the complete source.",
            input_schema="path: str, mode: 'full' | 'api_summary'",
            output_schema="dict with path, ok, content or api_summary",
            input_json_schema={
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "Relative path under generated_repo, e.g. src/pkg/model.py",
                    },
                    "mode": {"type": "string", "enum": ["full", "api_summary"], "default": "full"},
                },
                "required": ["path"],
            },
            risk_level="low",
            allowed_phases=["generation", "repair"],
        ),
        "list_generated_files": ExecutionToolSpec(
            name="list_generated_files",
            description="List all Python files generated so far under generated_repo.",
            input_schema="subdir: str | None",
            output_schema="list[str]",
            input_json_schema={
                "type": "object",
                "properties": {
                    "subdir": {
                        "type": "string",
                        "description": "Optional subdirectory to limit listing",
                    },
                },
                "required": [],
            },
            risk_level="low",
            allowed_phases=["generation", "repair"],
        ),
        # ── B. Code understanding ───────────────────────────────────
        "get_module_public_api": ExecutionToolSpec(
            name="get_module_public_api",
            description="Parse a Python file's AST and return all public classes, functions (with full signatures), and top-level constants.",
            input_schema="path: str",
            output_schema="dict with path, ok, api_summary (public interface digest)",
            input_json_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path under generated_repo"},
                },
                "required": ["path"],
            },
            risk_level="low",
            allowed_phases=["generation", "repair", "verification"],
        ),
        "search_in_codebase": ExecutionToolSpec(
            name="search_in_codebase",
            description="Search for a text pattern across all Python files in the generated repository.",
            input_schema="pattern: str, file_pattern: str = '*.py'",
            output_schema="list of {path, line_number, line_content}",
            input_json_schema={
                "type": "object",
                "properties": {
                    "pattern": {
                        "type": "string",
                        "description": "Text pattern to search (case-insensitive substring match)",
                    },
                    "file_pattern": {"type": "string", "default": "*.py"},
                },
                "required": ["pattern"],
            },
            risk_level="low",
            allowed_phases=["generation", "repair"],
        ),
        "check_python_syntax": ExecutionToolSpec(
            name="check_python_syntax",
            description="Run ast.parse on a code string. Returns validity and any syntax error details.",
            input_schema="code: str",
            output_schema="dict with valid, error_line, error_message",
            input_json_schema={
                "type": "object",
                "properties": {
                    "code": {"type": "string", "description": "Python source code to check"},
                },
                "required": ["code"],
            },
            risk_level="low",
            allowed_phases=["generation", "repair"],
        ),
        # ── C. Runtime verification (most critical) ────────────────
        "run_smoke_forward": ExecutionToolSpec(
            name="run_smoke_forward",
            description=(
                "Run a single forward pass of a target function/class in a subprocess "
                "with smoke-scale synthetic input. Captures tensor shapes and dtypes. "
                "Use this after writing code to catch dimension/type bugs that "
                "syntax and import checks cannot find."
            ),
            input_schema="module: str, target: str, smoke_input: dict",
            output_schema="dict with ok, error_type, error_message, shapes: dict",
            input_json_schema={
                "type": "object",
                "properties": {
                    "module": {
                        "type": "string",
                        "description": "Dotted module name under the package, e.g. 'my_pkg.model'",
                    },
                    "target": {
                        "type": "string",
                        "description": "Name of class/function to call, e.g. 'MyModel' or 'build_model'",
                    },
                    "smoke_input": {
                        "type": "object",
                        "description": "Keyword arguments to pass to the target, e.g. {'x': torch.randn(2,3,224,224)} — reference tensors as symbolic descriptions since this runs in a subprocess",
                    },
                },
                "required": ["module", "target"],
            },
            risk_level="medium",
            allowed_phases=["generation", "repair", "verification"],
            writes_state=True,
        ),
        # ── D. Evidence retrieval ───────────────────────────────────
        "search_evidence_by_keyword": ExecutionToolSpec(
            name="search_evidence_by_keyword",
            description="Search paper evidence chunks by keyword. Returns the top_k most relevant evidence items.",
            input_schema="keywords: list[str], top_k: int = 5",
            output_schema="list[evidence dict]",
            input_json_schema={
                "type": "object",
                "properties": {
                    "keywords": {
                        "type": "array",
                        "items": SCHEMA_STRING,
                        "description": "Keywords to search for",
                    },
                    "top_k": {"type": "integer", "default": 5},
                },
                "required": ["keywords"],
            },
            risk_level="low",
            allowed_phases=["generation", "repair"],
        ),
        "get_evidence_detail": ExecutionToolSpec(
            name="get_evidence_detail",
            description="Fetch a single evidence item by its ID to see the full paper excerpt.",
            input_schema="evidence_id: str",
            output_schema="Evidence dict or None",
            input_json_schema={
                "type": "object",
                "properties": {
                    "evidence_id": {"type": "string", "description": "Evidence item ID"},
                },
                "required": ["evidence_id"],
            },
            risk_level="low",
            allowed_phases=["generation", "repair"],
        ),
        # ── E. Verification & diagnosis ─────────────────────────────
        "check_file_contract": ExecutionToolSpec(
            name="check_file_contract",
            description="Verify that a generated file satisfies its structured FileContract (required symbols, functions, imports).",
            input_schema="path: str",
            output_schema="dict with passed, required_symbols_checked, required_functions_checked, issues",
            input_json_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path under generated_repo"},
                },
                "required": ["path"],
            },
            risk_level="low",
            allowed_phases=["generation", "repair", "verification"],
        ),
        "check_cross_file_integration": ExecutionToolSpec(
            name="check_cross_file_integration",
            description="Check that the file correctly imports and calls symbols from upstream dependencies per integration contracts.",
            input_schema="path: str",
            output_schema="list[ContractVerificationIssue]",
            input_json_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path under generated_repo"},
                },
                "required": ["path"],
            },
            risk_level="low",
            allowed_phases=["generation", "repair", "verification"],
        ),
        "get_import_error_detail": ExecutionToolSpec(
            name="get_import_error_detail",
            description="Try importing a module in a subprocess and return any error details. Useful for diagnosing import failures.",
            input_schema="module_name: str",
            output_schema="dict with success, error_type, error_message",
            input_json_schema={
                "type": "object",
                "properties": {
                    "module_name": {
                        "type": "string",
                        "description": "Dotted module path, e.g. 'my_pkg.losses'",
                    },
                },
                "required": ["module_name"],
            },
            risk_level="medium",
            allowed_phases=["verification", "repair"],
        ),
        # ── F. Repair tools ─────────────────────────────────────────
        "get_repair_history": ExecutionToolSpec(
            name="get_repair_history",
            description="Get previous repair records for a file (or all files).",
            input_schema="file_path: str | None",
            output_schema="list[repair record dict]",
            input_json_schema={
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Optional file path filter"},
                },
                "required": [],
            },
            risk_level="low",
            allowed_phases=["repair"],
        ),
        "diff_file_versions": ExecutionToolSpec(
            name="diff_file_versions",
            description="Generate a unified diff between the current file content and its previous version.",
            input_schema="path: str, previous_content_hash: str",
            output_schema="dict with path, ok, diff (unified diff string)",
            input_json_schema={
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path under generated_repo"},
                    "previous_content_hash": {
                        "type": "string",
                        "description": "SHA256 hash of the previous content",
                    },
                },
                "required": ["path", "previous_content_hash"],
            },
            risk_level="low",
            allowed_phases=["repair"],
        ),
    }


class ToolExecutor:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.specs = state.get("tool_specs") or default_tool_specs()

    def call(self, name: str, **kwargs: Any) -> Any:
        spec = self.specs[name]
        started = utc_now()
        input_payload = redact_mapping(kwargs)
        input_hash = sha256_text(
            json.dumps(input_payload, sort_keys=True, ensure_ascii=False, default=str)
        )
        artifacts_written: list[str] = []
        status = "succeeded"
        output_hash: str | None = None
        error: str | None = None
        try:
            result = getattr(self, f"_tool_{name}")(**kwargs)
            payload, artifacts_written = _normalize_tool_result(result)
            if payload is not None:
                output_hash = sha256_text(
                    json.dumps(_jsonable(payload), sort_keys=True, ensure_ascii=False, default=str)
                )
            return payload
        except Exception as exc:  # noqa: BLE001
            status = "failed"
            error = redact_text(f"{exc.__class__.__name__}: {exc}")
            raise
        finally:
            record = ToolCallRecord(
                id=stable_id(
                    "toolcall", f"{name}:{started}:{len(self.state.get('tool_calls', []))}"
                ),
                run_id=_run_id(self.state),
                phase=self.state.get("execution_phase", "unknown"),
                tool_name=spec.name,
                input_hash=input_hash,
                output_hash=output_hash,
                status=status,
                started_at=started,
                ended_at=utc_now(),
                error=error,
                artifacts_written=artifacts_written,
            )
            self.state.setdefault("tool_calls", []).append(record)

    def _tool_load_execution_context(self, *, run_dir: str) -> dict[str, Any]:
        paths = prepare_run_dirs(Path(run_dir))
        if not paths.manifest.exists():
            raise FileNotFoundError(f"Run manifest not found: {paths.manifest}")
        if not paths.approved_plan.exists():
            raise FileNotFoundError(
                "Approved plan not found. Finish `paper2repo review` or `paper2repo approve` before running execute."
            )
        manifest = RunManifest.model_validate(read_json(paths.manifest))
        approved_plan = ApprovedPlan.model_validate(read_json(paths.approved_plan))
        if not approved_plan.execution_allowed:
            raise RuntimeError("ApprovedPlan.execution_allowed is false; execution is blocked.")
        paper_spec = read_json(paths.paper_spec)
        validation_result = read_json(paths.validation_result)
        source_policy = (
            SourcePolicy.model_validate(read_json(paths.source_policy))
            if paths.source_policy.exists()
            else SourcePolicy(id="source_policy_default")
        )
        evidence_items = [Evidence.model_validate(row) for row in load_jsonl(paths.evidence_store)]
        evidence_refs = [EvidenceRef.model_validate(row) for row in load_jsonl(paths.evidence_refs)]
        paper_id = evidence_items[0].paper_id if evidence_items else "paper"
        evidence_store = EvidenceStore(
            run_id=manifest.run_id,
            paper_id=paper_id,
            items=evidence_items,
            refs=evidence_refs,
        )
        return {
            "paths": paths,
            "manifest": manifest,
            "approved_plan": approved_plan,
            "paper_spec": paper_spec,
            "validation_result": validation_result,
            "source_policy": source_policy,
            "evidence_store": evidence_store,
            "repo_dir": str(paths.generated_repo),
            "tool_specs": self.specs,
            "planning_trace": _load_planning_trace(paths),
        }

    def _tool_query_evidence_by_field(
        self, *, sqlite_path: str, field_path: str
    ) -> list[dict[str, object]]:
        return query_evidence_by_field(Path(sqlite_path), field_path)

    def _tool_query_evidence_by_id(self, *, evidence_ids: str | list[str]) -> list[dict[str, Any]]:
        ids = [evidence_ids] if isinstance(evidence_ids, str) else list(evidence_ids)
        items = self.state["evidence_store"].items
        rows = [item.model_dump(mode="json") for item in items if item.id in ids]
        return rows

    def _tool_ensure_repo_dirs(
        self, *, repo_root: str, package_name: str
    ) -> tuple[dict[str, str], list[str]]:
        root = _validate_repo_path(self.state, Path(repo_root))
        dirs = [
            root,
            root / "src" / package_name,
            root / "tests",
            root / "scripts",
            root / "docs",
            self.state["paths"].execution_dir / "repairs",
        ]
        for path in dirs:
            ensure_dir(path)
        return {"repo_root": str(root)}, [_rel(self.state["paths"].run_dir, path) for path in dirs]

    def _tool_write_repo_file(
        self, *, rel_path: str, content: str
    ) -> tuple[dict[str, str], list[str]]:
        target = _validate_repo_path(self.state, self.state["paths"].generated_repo / rel_path)
        normalized_rel_path = _rel(self.state["paths"].generated_repo, target)
        _enforce_agent_write_guardrails(self.state, normalized_rel_path, content)
        ensure_dir(target.parent)
        target.write_text(content, encoding="utf-8")
        rel = _rel(self.state["paths"].run_dir, target)
        return {"path": rel}, [rel]

    def _tool_write_repo_files_batch(
        self, *, files: dict[str, str]
    ) -> tuple[list[dict[str, str]], list[str]]:
        written: list[dict[str, str]] = []
        artifact_paths: list[str] = []
        for rel_path, content in files.items():
            payload, paths = self._tool_write_repo_file(rel_path=rel_path, content=content)
            written.append(payload)
            artifact_paths.extend(paths)
        return written, artifact_paths

    def _tool_run_command(self, *, command_spec: CommandSpec) -> CommandResult:
        policy: ExecutionPolicy = self.state["execution_policy"]
        command_text = _command_text(command_spec.command)
        normalized_text = _normalized_command_text(command_spec.command)
        normalized_allowed = [_normalize_allowed_command(item) for item in policy.allowed_commands]
        if not any(
            normalized_text == allowed or normalized_text.startswith(f"{allowed} ")
            for allowed in normalized_allowed
        ):
            raise RuntimeError(f"Command is not allowed by execution policy: {command_text}")
        return _run_command(command_spec)

    def _tool_write_execution_artifact(
        self,
        *,
        path: str,
        payload: Any,
        format: str,
    ) -> tuple[dict[str, str], list[str]]:
        target = _validate_run_path(self.state, self.state["paths"].run_dir / path)
        ensure_dir(target.parent)
        if format == "json":
            write_json(target, _jsonable(payload))
        elif format == "jsonl":
            write_jsonl(target, [_jsonable(item) for item in payload])
        elif format == "text":
            target.write_text(str(payload), encoding="utf-8")
        else:
            raise ValueError(f"Unsupported artifact format: {format}")
        rel = _rel(self.state["paths"].run_dir, target)
        return {"path": rel}, [rel]

    def _tool_update_manifest(
        self,
        *,
        status: str,
        approval_status: str,
        approval_iteration_count: int,
        extra_files: list[str],
    ) -> RunManifest:
        paths: RunPaths = self.state["paths"]
        manifest = write_manifest(
            paths=paths,
            run_id=self.state["manifest"].run_id,
            status=status,
            approved_plan=self.state.get("approved_plan"),
            approval_status=approval_status,
            approval_iteration_count=approval_iteration_count,
            extra_files=[
                _validate_run_path(self.state, paths.run_dir / rel_path) for rel_path in extra_files
            ],
        )
        return manifest

    # ── Additional agent tools ─────────────────────────────────────

    def _tool_read_generated_file(self, *, path: str, mode: str = "full") -> dict[str, Any]:
        target = _validate_repo_path(self.state, self.state["paths"].generated_repo / path)
        if not target.exists():
            return {"path": path, "ok": False, "error": f"File not found: {path}"}
        content = target.read_text(encoding="utf-8", errors="replace")
        if mode == "api_summary":
            from paper2repo_agent.graphs.execute.nodes import _interface_summary

            return {
                "path": path,
                "ok": True,
                "mode": "api_summary",
                "summary": _interface_summary(path, content),
            }
        return {"path": path, "ok": True, "mode": "full", "content": content}

    def _tool_list_generated_files(self, *, subdir: str | None = None) -> list[str]:
        root = self.state["paths"].generated_repo
        if subdir:
            root = _validate_repo_path(self.state, root / subdir)
        return sorted(
            str(p.relative_to(self.state["paths"].generated_repo).as_posix())
            for p in root.rglob("*.py")
            if p.is_file()
        )

    def _tool_get_module_public_api(self, *, path: str) -> dict[str, Any]:
        target = _validate_repo_path(self.state, self.state["paths"].generated_repo / path)
        if not target.exists():
            return {"path": path, "ok": False, "error": f"File not found: {path}"}
        content = target.read_text(encoding="utf-8", errors="replace")
        from paper2repo_agent.graphs.execute.nodes import _interface_summary

        return {"path": path, "ok": True, "api_summary": _interface_summary(path, content)}

    def _tool_search_in_codebase(
        self, *, pattern: str, file_pattern: str = "*.py"
    ) -> list[dict[str, Any]]:
        root = self.state["paths"].generated_repo
        matches: list[dict[str, Any]] = []
        for py_file in root.rglob(file_pattern):
            if not py_file.is_file():
                continue
            try:
                lines = py_file.read_text(encoding="utf-8", errors="replace").splitlines()
            except Exception:
                continue
            for i, line in enumerate(lines, start=1):
                if pattern.lower() in line.lower():
                    matches.append(
                        {
                            "path": str(py_file.relative_to(root).as_posix()),
                            "line_number": i,
                            "line_content": line.strip()[:200],
                        }
                    )
            if len(matches) >= 30:
                break
        return matches[:30]

    def _tool_check_python_syntax(self, *, code: str) -> dict[str, Any]:
        import ast as _ast

        try:
            _ast.parse(code)
            return {"valid": True, "error_line": None, "error_message": None}
        except SyntaxError as exc:
            return {
                "valid": False,
                "error_line": exc.lineno,
                "error_message": f"{exc.__class__.__name__}: {exc.msg}",
                "text": exc.text[:200] if exc.text else None,
            }

    def _tool_run_smoke_forward(
        self, *, module: str, target: str, smoke_input: dict | None = None
    ) -> dict[str, Any]:
        """Controlled subprocess run of a single forward pass to catch shape/dtype bugs."""
        import json as _json

        _validate_generated_module_name(self.state, module)
        _validate_identifier(target, "target")
        repo = str(self.state["paths"].generated_repo)
        src_path = str(Path(repo) / "src")
        probe_code = (
            "import importlib, inspect, json, sys\n"
            "from types import SimpleNamespace\n"
            "import torch\n"
            f"sys.path.insert(0, {src_path!r})\n"
            f"module_name = {module!r}\n"
            f"target_name = {target!r}\n"
            f"smoke_input = {_json.dumps(smoke_input or {})!r}\n"
            "smoke_input = json.loads(smoke_input)\n"
            "\n"
            "def _cfg_get(config, key, default):\n"
            "    if isinstance(config, dict):\n"
            "        return config.get(key, default)\n"
            "    return getattr(config, key, default)\n"
            "\n"
            "try:\n"
            "    config_mod = importlib.import_module(module_name.rsplit('.', 1)[0] + '.config')\n"
            "    DEFAULT_CONFIG = getattr(config_mod, 'DEFAULT_CONFIG')\n"
            "except Exception:\n"
            "    DEFAULT_CONFIG = SimpleNamespace(input_shape=(16,), num_classes=2, batch_size=2, hidden_dim=16, hash_dim=8, text_input_dim=16)\n"
            "\n"
            "batch_size = int(smoke_input.get('batch_size') or _cfg_get(DEFAULT_CONFIG, 'batch_size', 2) or 2)\n"
            "input_shape = tuple(smoke_input.get('input_shape') or _cfg_get(DEFAULT_CONFIG, 'input_shape', (16,)) or (16,))\n"
            "num_classes = int(smoke_input.get('num_classes') or _cfg_get(DEFAULT_CONFIG, 'num_classes', 2) or 2)\n"
            "hidden_dim = int(smoke_input.get('hidden_dim') or _cfg_get(DEFAULT_CONFIG, 'hidden_dim', 16) or 16)\n"
            "hash_dim = int(smoke_input.get('hash_dim') or _cfg_get(DEFAULT_CONFIG, 'hash_dim', 8) or 8)\n"
            "text_input_dim = int(smoke_input.get('text_input_dim') or _cfg_get(DEFAULT_CONFIG, 'text_input_dim', 16) or 16)\n"
            "\n"
            "def _tensor_for(name):\n"
            "    lowered = name.lower()\n"
            "    if lowered in {'target', 'targets', 'label', 'labels', 'y'}:\n"
            "        return torch.randint(0, max(num_classes, 2), (batch_size,), dtype=torch.long)\n"
            "    if 'text' in lowered:\n"
            "        return torch.randn(batch_size, text_input_dim)\n"
            "    if 'similarity' in lowered or lowered in {'s', 'sim'}:\n"
            "        return torch.eye(batch_size, dtype=torch.float32)\n"
            "    if 'adj' in lowered:\n"
            "        return torch.eye(batch_size, dtype=torch.float32)\n"
            "    if lowered == 'edge_index':\n"
            "        return torch.tensor([[0, 1], [1, 0]], dtype=torch.long)\n"
            "    return torch.randn(batch_size, *input_shape)\n"
            "\n"
            "def _summarize(value):\n"
            "    if isinstance(value, torch.Tensor):\n"
            "        return {'type': 'Tensor', 'shape': list(value.shape), 'dtype': str(value.dtype)}\n"
            "    if isinstance(value, dict):\n"
            "        return {'type': 'dict', 'keys': list(value.keys()), 'values': {str(k): _summarize(v) for k, v in value.items()}}\n"
            "    if isinstance(value, (list, tuple)):\n"
            "        return {'type': type(value).__name__, 'items': [_summarize(v) for v in value]}\n"
            "    return {'type': type(value).__name__}\n"
            "\n"
            "def _callable_kwargs(fn):\n"
            "    explicit = smoke_input.get('forward_kwargs') or smoke_input.get('call_kwargs')\n"
            "    if isinstance(explicit, dict) and explicit:\n"
            "        return {key: _tensor_for(str(key)) for key in explicit}\n"
            "    signature = inspect.signature(fn)\n"
            "    kwargs = {}\n"
            "    for name, param in signature.parameters.items():\n"
            "        if name == 'self':\n"
            "            continue\n"
            "        if param.kind in (inspect.Parameter.VAR_POSITIONAL, inspect.Parameter.VAR_KEYWORD):\n"
            "            continue\n"
            "        kwargs[name] = _tensor_for(name)\n"
            "    return kwargs\n"
            "\n"
            "def _instantiate(cls):\n"
            "    init_kwargs = smoke_input.get('init_kwargs') if isinstance(smoke_input.get('init_kwargs'), dict) else {}\n"
            "    attempts = []\n"
            "    if init_kwargs:\n"
            "        attempts.append(lambda: cls(**init_kwargs))\n"
            "    attempts.extend([\n"
            "        lambda: cls(input_shape=input_shape, num_classes=num_classes, hidden_dim=hidden_dim, hash_dim=hash_dim, text_input_dim=text_input_dim),\n"
            "        lambda: cls(DEFAULT_CONFIG),\n"
            "        lambda: cls(input_shape, num_classes, hidden_dim, hash_dim, text_input_dim),\n"
            "        lambda: cls(),\n"
            "    ])\n"
            "    errors = []\n"
            "    for attempt in attempts:\n"
            "        try:\n"
            "            return attempt()\n"
            "        except Exception as exc:\n"
            "            errors.append(f'{type(exc).__name__}: {exc}')\n"
            "    raise RuntimeError('Could not instantiate target class: ' + ' | '.join(errors[-3:]))\n"
            "\n"
            "try:\n"
            "    module = importlib.import_module(module_name)\n"
            "    target_obj = getattr(module, target_name)\n"
            "    if inspect.isclass(target_obj):\n"
            "        instance = _instantiate(target_obj)\n"
            "        if isinstance(instance, torch.nn.Module):\n"
            "            instance.eval()\n"
            "        call_target = instance.forward if hasattr(instance, 'forward') else instance\n"
            "        with torch.no_grad():\n"
            "            result = call_target(**_callable_kwargs(call_target))\n"
            "    else:\n"
            "        result = target_obj(**_callable_kwargs(target_obj))\n"
            "        if isinstance(result, torch.nn.Module):\n"
            "            result.eval()\n"
            "            call_target = result.forward if hasattr(result, 'forward') else result\n"
            "            with torch.no_grad():\n"
            "                result = call_target(**_callable_kwargs(call_target))\n"
            "    print(json.dumps({'ok': True, 'shapes': _summarize(result)}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'ok': False, 'error_type': type(e).__name__, 'error_message': str(e)[:2000]}))\n"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", probe_code],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
                env={
                    **os.environ,
                    "PYTHONPATH": src_path + os.pathsep + os.environ.get("PYTHONPATH", ""),
                },
            )
            stdout = completed.stdout.strip()
            stderr = completed.stderr.strip()
            if stdout:
                try:
                    payload = _json.loads(stdout)
                except _json.JSONDecodeError:
                    return {
                        "ok": False,
                        "error_type": "JSONDecodeError",
                        "error_message": stdout[:2000],
                        "stderr": stderr[:1000],
                    }
                if completed.returncode != 0 and payload.get("ok") is not False:
                    payload["ok"] = False
                    payload["error_type"] = (
                        payload.get("error_type") or f"SubprocessExit{completed.returncode}"
                    )
                    payload["stderr"] = stderr[:1000]
                return payload
            return {"ok": False, "error_type": "NoOutput", "error_message": stderr[:2000]}
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "error_type": "TimeoutExpired",
                "error_message": "run_smoke_forward timed out after 60s",
            }

    def _tool_search_evidence_by_keyword(
        self, *, keywords: list[str], top_k: int = 5
    ) -> list[dict[str, Any]]:
        items = self.state.get("evidence_store")
        if items is None:
            return []
        store = items if hasattr(items, "items") else None
        if store is None:
            return []
        evidence_list = store.items if hasattr(store, "items") else []
        scored: list[tuple[int, dict[str, Any]]] = []
        for ev in evidence_list:
            text = (ev.text or "").lower()
            score = sum(1 for kw in keywords if kw.lower() in text)
            if score > 0:
                scored.append((score, ev.model_dump(mode="json")))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [item for _, item in scored[:top_k]]

    def _tool_get_evidence_detail(self, *, evidence_id: str) -> dict[str, Any] | None:
        items = self.state.get("evidence_store")
        if items is None:
            return None
        store = items if hasattr(items, "items") else None
        if store is None:
            return None
        evidence_list = store.items if hasattr(store, "items") else []
        for ev in evidence_list:
            if ev.id == evidence_id:
                return ev.model_dump(mode="json")
        return None

    def _tool_check_file_contract(self, *, path: str) -> dict[str, Any]:
        target = _validate_repo_path(self.state, self.state["paths"].generated_repo / path)
        if not target.exists():
            return {
                "path": path,
                "passed": False,
                "issues": [{"message": f"File not found: {path}"}],
            }
        content = target.read_text(encoding="utf-8", errors="replace")
        file_contract = None
        for task in self.state.get("file_tasks", []):
            if task.path == path and task.file_contract is not None:
                file_contract = task.file_contract
                break
        if file_contract is None:
            return {
                "path": path,
                "passed": True,
                "issues": [],
                "note": "No FileContract defined for this path.",
            }
        from paper2repo_agent.graphs.execute.nodes import _verify_single_file_contract

        record = _verify_single_file_contract(path, content, file_contract)
        return {
            "path": path,
            "passed": record.passed,
            "required_symbols_checked": record.required_symbols_checked,
            "required_functions_checked": record.required_functions_checked,
            "issues": [
                {"message": issue.message, "symbol": issue.symbol} for issue in record.issues
            ],
        }

    def _tool_check_cross_file_integration(self, *, path: str) -> list[dict[str, Any]]:
        target = _validate_repo_path(self.state, self.state["paths"].generated_repo / path)
        if not target.exists():
            return [
                {"path": path, "category": "missing_file", "message": f"File not found: {path}"}
            ]
        content = target.read_text(encoding="utf-8", errors="replace")
        from paper2repo_agent.graphs.execute.nodes import (
            _source_map_for_generated_contracts,
            _verify_cross_file_api_contracts,
        )

        source_by_path = _source_map_for_generated_contracts(
            self.state, override_path=path, override_content=content
        )
        issues: list[dict[str, Any]] = []
        for record in _verify_cross_file_api_contracts(self.state, source_by_path):
            for issue in record.issues:
                if (
                    path in {issue.consumer_path, issue.path}
                    if hasattr(issue, "consumer_path")
                    else (record.path == path)
                ):
                    issues.append(
                        {
                            "path": record.path,
                            "category": issue.category,
                            "message": issue.message,
                            "symbol": issue.symbol,
                            "provider_path": getattr(issue, "provider_path", None),
                        }
                    )
        return issues

    def _tool_get_import_error_detail(self, *, module_name: str) -> dict[str, Any]:
        import json as _json

        _validate_generated_module_name(self.state, module_name)
        repo = str(self.state["paths"].generated_repo)
        src_path = str(Path(repo) / "src")
        probe = (
            "import importlib, sys, json\n"
            f"sys.path.insert(0, {src_path!r})\n"
            f"module_name = {module_name!r}\n"
            "try:\n"
            "    importlib.import_module(module_name)\n"
            "    print(json.dumps({'success': True, 'module': module_name}))\n"
            "except Exception as e:\n"
            "    print(json.dumps({'success': False, 'error_type': type(e).__name__, 'error_message': str(e)[:2000]}))\n"
        )
        try:
            completed = subprocess.run(
                [sys.executable, "-c", probe],
                cwd=repo,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
                env={
                    **os.environ,
                    "PYTHONPATH": src_path + os.pathsep + os.environ.get("PYTHONPATH", ""),
                },
            )
            stdout = completed.stdout.strip()
            if stdout:
                try:
                    return _json.loads(stdout)
                except _json.JSONDecodeError:
                    return {
                        "success": False,
                        "error_type": "JSONDecodeError",
                        "error_message": stdout[:2000],
                    }
            return {
                "success": False,
                "error_type": "NoOutput",
                "error_message": completed.stderr.strip()[:2000],
            }
        except subprocess.TimeoutExpired:
            return {
                "success": False,
                "error_type": "TimeoutExpired",
                "error_message": "Import check timed out after 30s",
            }

    def _tool_get_repair_history(self, *, file_path: str | None = None) -> list[dict[str, Any]]:
        records = self.state.get("repair_records", [])
        if file_path:
            records = [
                r
                for r in records
                if file_path in (r.files_changed if hasattr(r, "files_changed") else [])
            ]
        return [
            {
                "id": r.id,
                "repair_task_id": r.repair_task_id if hasattr(r, "repair_task_id") else None,
                "files_changed": r.files_changed if hasattr(r, "files_changed") else [],
                "status": r.status if hasattr(r, "status") else "unknown",
            }
            for r in records[-10:]
        ]

    def _tool_diff_file_versions(self, *, path: str, previous_content_hash: str) -> dict[str, Any]:
        target = _validate_repo_path(self.state, self.state["paths"].generated_repo / path)
        if not target.exists():
            return {"path": path, "ok": False, "error": f"File not found: {path}"}
        current = target.read_text(encoding="utf-8", errors="replace")
        import difflib

        generated_contents = self.state.get("generated_file_contents", {})
        previous = generated_contents.get(path)
        if previous is None:
            return {"path": path, "ok": False, "error": "No previous version cached in state."}
        diff = list(
            difflib.unified_diff(
                previous.splitlines(keepends=True),
                current.splitlines(keepends=True),
                fromfile=f"{path} (previous)",
                tofile=f"{path} (current)",
            )
        )
        return {"path": path, "ok": True, "diff": "".join(diff)[:5000]}


def _normalize_tool_result(result: Any) -> tuple[Any, list[str]]:
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[1], list):
        return result[0], result[1]
    return result, []


def _jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {key: _jsonable(item) for key, item in value.items()}
    return value


_DOTTED_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_repo_path(state: dict[str, Any], path: Path) -> Path:
    root = state["paths"].generated_repo.resolve()
    target = path.resolve()
    if target != root and root not in target.parents:
        raise RuntimeError(f"Repo path escapes generated_repo: {path}")
    return target


def _validate_identifier(value: str, label: str) -> str:
    if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
        raise RuntimeError(f"Invalid {label}: {value!r}")
    return value


def _validate_dotted_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not _DOTTED_NAME_RE.fullmatch(value):
        raise RuntimeError(f"Invalid {label}: {value!r}")
    return value


def _validate_generated_module_name(state: dict[str, Any], module_name: str) -> str:
    module_name = _validate_dotted_name(module_name, "module name")
    package = getattr(state.get("execution_policy"), "package_name", None)
    if package:
        package = _validate_dotted_name(str(package), "package name")
        if module_name != package and not module_name.startswith(f"{package}."):
            raise RuntimeError(
                f"Module must belong to generated package {package!r}: {module_name!r}"
            )
    return module_name


def _enforce_agent_write_guardrails(state: dict[str, Any], rel_path: str, content: str) -> None:
    task = state.get("current_agent_task")
    if task is None:
        return
    if rel_path != getattr(task, "path", None):
        raise RuntimeError(f"Agent write target mismatch: expected {task.path}, got {rel_path}")
    from paper2repo_agent.graphs.execute.nodes import (
        _generated_file_guardrail_error,
        _looks_unsafe_generated_code,
    )

    if _looks_unsafe_generated_code(content):
        raise RuntimeError(
            "Generated file contains blocked network, shell, or credential access patterns."
        )
    guardrail_error = _generated_file_guardrail_error(state, task, content)
    if guardrail_error:
        raise RuntimeError(guardrail_error)


def _validate_run_path(state: dict[str, Any], path: Path) -> Path:
    root = state["paths"].run_dir.resolve()
    target = path.resolve()
    if target != root and root not in target.parents:
        raise RuntimeError(f"Run path escapes run_dir: {path}")
    return target


def _rel(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _run_id(state: dict[str, Any]) -> str:
    manifest = state.get("manifest")
    if manifest is not None:
        return manifest.run_id
    return stable_id("run", state.get("run_dir", "unknown"))


def _command_text(command: list[str]) -> str:
    return " ".join(command)


def _normalized_command_text(command: list[str]) -> str:
    if not command:
        return ""
    head = Path(command[0]).name or command[0]
    return " ".join([head, *command[1:]])


def _normalize_allowed_command(value: str) -> str:
    parts = value.split()
    if not parts:
        return value
    parts[0] = Path(parts[0]).name or parts[0]
    return " ".join(parts)


def _tail(value: Any, limit: int = 4000) -> str | None:
    if value is None:
        return None
    text = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else str(value)
    text = redact_text(text)
    return text[-limit:] if len(text) > limit else text


def _run_command(spec: CommandSpec) -> CommandResult:
    started = time.perf_counter()
    env = os.environ.copy()
    src_path = str(Path(spec.cwd) / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            spec.command,
            cwd=spec.cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=spec.timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return CommandResult(
            command_id=spec.id,
            command=spec.command,
            cwd=spec.cwd,
            status="timeout",
            stdout_tail=_tail(exc.stdout),
            stderr_tail=_tail(exc.stderr),
            duration_seconds=time.perf_counter() - started,
        )
    status = _classify_command_status(
        spec, completed.returncode, completed.stdout, completed.stderr
    )
    return CommandResult(
        command_id=spec.id,
        command=spec.command,
        cwd=spec.cwd,
        status=status,
        exit_code=completed.returncode,
        stdout_tail=_tail(completed.stdout),
        stderr_tail=_tail(completed.stderr),
        duration_seconds=time.perf_counter() - started,
    )


def _classify_command_status(
    spec: CommandSpec,
    returncode: int,
    stdout: str | None,
    stderr: str | None,
) -> str:
    if returncode != 0:
        return "failed"
    payload = _extract_json_payload(stdout)
    if spec.check_type == "smoke_train" and isinstance(payload, dict):
        status = (
            str(payload.get("status") or payload.get("training_result", {}).get("status") or "")
            .strip()
            .lower()
        )
        if status == "skipped":
            return "skipped"
        if status in {"failed", "error"}:
            return "failed"
    return "passed"


def _extract_json_payload(stdout: str | None) -> dict[str, Any] | None:
    text = (stdout or "").strip()
    if not text:
        return None
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None
    return payload if isinstance(payload, dict) else None


# ─────────────────────────────────────────────────────────────────────
# Agent tool-calling factories
# ─────────────────────────────────────────────────────────────────────


def _agent_visible_tools(
    specs: dict[str, ExecutionToolSpec], phase: str
) -> list[ExecutionToolSpec]:
    """Return only the tools whose ``allowed_phases`` includes *phase*.

    **Principle**: tools the LLM cannot see cannot be called — this is
    a security-prefix that replaces post-call blocking with pre-call
    visibility filtering.
    """
    return [spec for spec in specs.values() if phase in spec.allowed_phases]


def to_langchain_tool(spec: ExecutionToolSpec, executor: object) -> Any:
    """Convert an ``ExecutionToolSpec`` into a LangChain ``StructuredTool``.

    **Key invariant**: the execution body calls ``executor.call(spec.name, **kwargs)``,
    which dispatches to the same ``ToolExecutor._tool_xxx`` method as the
    node path — same 5 safety guardrails, same ``ToolCallRecord`` audit.
    The Agent path only changes WHO decides (LLM vs node), not HOW tools run.

    Args:
        spec: Tool specification with ``input_json_schema`` (required).
        executor: A ``ToolExecutor`` instance whose ``.call()`` method runs tools.

    Returns:
        A ``StructuredTool`` ready for inclusion in an LLM ``.invoke(tools=[...])``.
    """
    if spec.input_json_schema is None:
        raise ValueError(
            f"Tool '{spec.name}' has no input_json_schema; "
            "cannot build StructuredTool for LLM function calling."
        )

    from pydantic import BaseModel, Field as PydanticField, create_model

    # Build a Pydantic args model dynamically from input_json_schema.
    props = spec.input_json_schema.get("properties", {})
    required: list[str] = spec.input_json_schema.get("required", [])
    fields: dict[str, tuple[type, Any]] = {}

    for field_name, field_schema in props.items():
        py_type = _json_type_to_python(field_schema)
        field_desc = field_schema.get("description", "")
        if field_name in required:
            fields[field_name] = (py_type, PydanticField(description=field_desc))
        else:
            fields[field_name] = (
                py_type | None,
                PydanticField(default=None, description=field_desc),
            )

    ArgsSchema = create_model(
        f"_{spec.name}_args__",
        __base__=BaseModel,
        **fields,
    )

    def _execute(**kwargs: Any) -> str:
        """Execute via shared ToolExecutor.call()底座."""
        try:
            result = executor.call(spec.name, **kwargs)
            if result is None:
                return json.dumps({"ok": True, "result": None})
            return json.dumps(_jsonable(result), ensure_ascii=False, default=str)
        except Exception as exc:  # noqa: BLE001
            return json.dumps(
                {
                    "ok": False,
                    "error": f"{exc.__class__.__name__}: {exc}",
                }
            )

    # Annotate the function signature so LangChain can infer schema.
    _execute.__name__ = spec.name
    _execute.__doc__ = spec.description

    try:
        from langchain_core.tools import StructuredTool

        return StructuredTool.from_function(
            func=_execute,
            name=spec.name,
            description=spec.description,
            args_schema=ArgsSchema,
            return_direct=False,
        )
    except ImportError:
        raise ImportError(
            "langchain_core>=0.3 is required for StructuredTool. "
            "Install with: pip install langchain-core>=0.3"
        )


def _json_type_to_python(schema: dict[str, Any]) -> type:
    """Map a JSON Schema type entry to a Python type for Pydantic model fields."""
    type_map: dict[str, type] = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": bool,
        "array": list,
        "object": dict,
    }
    if "enum" in schema:
        return str
    if "oneOf" in schema:
        return str  # simplified: accept string for oneOf[ str | list[str] ]
    json_type = schema.get("type", "string")
    if isinstance(json_type, list):
        # Pick the first non-null type
        json_type = next((t for t in json_type if t != "null"), "string")
    return type_map.get(json_type, str)


def _load_planning_trace(paths: RunPaths) -> dict | None:
    """Load the PlanningTrace from disk if it exists, returning a raw dict
    so the execute state can reconstruct it as a PlanningTrace model."""
    if not paths.planning_trace.exists():
        return None
    try:
        from paper2repo_agent.utils import read_json

        return read_json(paths.planning_trace)
    except Exception:
        return None
