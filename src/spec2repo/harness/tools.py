"""Typed capabilities. All workspace operations run inside the restricted container."""

from typing import Annotated

from pydantic import Field, StringConstraints

from spec2repo.domain.agent import BoundedText, ImplementationPlan
from spec2repo.domain.spec import Contract
from spec2repo.sandbox.docker import CommandFailed

Short = Annotated[str, StringConstraints(min_length=1, max_length=1000)]


class ToolArgs(Contract):
    depends_on: tuple[int, ...] = Field(
        default=(), max_length=7,
        description="Optional 1-based indices of earlier calls in this response that must succeed.",
    )


class ReadFile(ToolArgs):
    path: Short
    offset: int = Field(default=0, ge=0, le=16_000_000)


class ListFiles(ToolArgs):
    path: Short = "."


class SearchFiles(ToolArgs):
    text: Short


class WriteFile(ToolArgs):
    path: Short
    content: Annotated[str, StringConstraints(max_length=32000)]


class Terminal(ToolArgs):
    argv: tuple[Short, ...] = Field(min_length=1, max_length=30)


class ProposePlan(ToolArgs):
    plan: ImplementationPlan


class RevisePlan(ToolArgs):
    plan: ImplementationPlan
    reason: BoundedText


class Clarify(ToolArgs):
    questions: tuple[BoundedText, ...] = Field(min_length=1, max_length=8)


class Finish(ToolArgs):
    summary: BoundedText


MemoryNote = Annotated[str, StringConstraints(min_length=1, max_length=300)]


class UpdateMemory(ToolArgs):
    progress: tuple[MemoryNote, ...] = Field(default=(), max_length=4)
    decisions: tuple[MemoryNote, ...] = Field(default=(), max_length=4)
    failed_attempts: tuple[MemoryNote, ...] = Field(default=(), max_length=4)
    next_steps: tuple[MemoryNote, ...] = Field(default=(), max_length=4)
    evidence_ids: tuple[Short, ...] = Field(default=(), max_length=12)


class ReadOutput(ToolArgs):
    evidence_id: Short
    offset: int = Field(default=0, ge=0, le=65536)
    limit: int = Field(default=4000, ge=1, le=8000)


TOOLS = {
    "update_task_memory": (UpdateMemory, "Replace concise task notes; cite evidence IDs. Not authoritative."),
    "read_tool_output": (ReadOutput, "Read a captured tool output page by evidence ID; offset is characters."),
    "propose_plan": (ProposePlan, "Submit a complete implementation plan for owner approval."),
    "request_clarification": (Clarify, "Pause for business clarification or any spec/API change."),
    "read_file": (ReadFile, "Read up to 8000 characters from a workspace file."),
    "list_files": (ListFiles, "List at most 200 immediate entries within the workspace."),
    "search_files": (SearchFiles, "Search literal text in workspace files, bounded to 50 matches."),
    "write_file": (WriteFile, "Create or replace a UTF-8 file in this workspace."),
    "terminal": (Terminal, "Run argv in isolated /workspace; use sh -c if shell syntax is needed."),
    "revise_plan": (RevisePlan, "Revise implementation organization only; never change CoreSpec."),
    "finish": (Finish, "Submit implementation for independent verification, not success."),
}


def schemas(phase):
    names = (
        ["propose_plan", "request_clarification"]
        if phase == "PLAN"
        else [
            "update_task_memory",
            "read_tool_output",
            "read_file",
            "list_files",
            "search_files",
            "write_file",
            "terminal",
            "revise_plan",
            "request_clarification",
            "finish",
        ]
    )
    return [
        {
            "type": "function",
            "function": {
                "name": name,
                "description": TOOLS[name][1],
                "parameters": TOOLS[name][0].model_json_schema(),
            },
        }
        for name in names
    ]


# Data arguments are JSON in argv, never interpolated into executable Python/shell source.
FILE_TOOL = r"""
import json, pathlib, sys
name, args = sys.argv[1], json.loads(sys.argv[2])
root = pathlib.Path('/workspace')
def path(value):
    target = (root / value).resolve()
    if not target.is_relative_to(root):
        raise ValueError('Path outside workspace')
    return target
if name == 'write_file':
    target = path(args['path'])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(args['content'], encoding='utf-8')
    print('written')
elif name == 'read_file':
    with path(args['path']).open(encoding='utf-8') as file:
        file.seek(args['offset'])
        print(file.read(8000))
elif name == 'list_files':
    entries = sorted(path(args['path']).iterdir())
    print(json.dumps([str(p.relative_to(root)) for p in entries[:200]]))
else:
    matches, visited = [], 0
    for entry in root.rglob('*'):
        visited += 1
        if visited > 1000 or len(matches) >= 50:
            break
        if entry.is_symlink() or not entry.is_file() or entry.stat().st_size > 100000:
            continue
        for number, line in enumerate(entry.read_text(encoding='utf-8', errors='replace').splitlines(), 1):
            if args['text'] in line:
                matches.append({'path': str(entry.relative_to(root)), 'line': number,
                                'text': line[:300]})
                if len(matches) >= 50:
                    break
    print(json.dumps(matches))
"""


def workspace_tool(sandbox, lease, name, args, *, authorized, stop, invocation=None):
    argv = (
        list(args.argv)
        if name == "terminal"
        else ["python", "-c", FILE_TOOL, name, args.model_dump_json()]
    )
    try:
        extra = {"invocation": invocation} if invocation else {}
        output = sandbox.run(lease, argv, authorized=authorized, stop=stop, timeout=30, **extra)
        return {"ok": True, "output": output, "truncated": False}
    except CommandFailed as exc:
        return {
            "ok": False,
            "exit_code": exc.exit_code,
            "output": exc.output,
            "truncated": False,
        }
