"""Conservative dependency inference; explicit edges can only add restrictions."""

from dataclasses import dataclass
from pathlib import Path

from spec2repo.harness.model import ModelError
from spec2repo.harness.tools import TOOLS

BARRIERS = {"terminal", "revise_plan", "finish", "propose_plan", "request_clarification"}
BARRIERS.update({"update_task_memory", "read_tool_output"})


@dataclass
class Invocation:
    id: str
    name: str
    args: object
    raw: str
    dependencies: set[int]


def batch(calls, allowed, workspace):
    if not isinstance(calls, list) or not 1 <= len(calls) <= 8:
        raise ModelError("Invalid tool batch size")
    parsed, ids = [], set()
    try:
        for index, call in enumerate(calls):
            name, call_id = call["function"]["name"], call["id"]
            raw = call["function"]["arguments"]
            if name not in allowed or not isinstance(call_id, str) or not call_id or call_id in ids:
                raise ValueError("Invalid tool identity")
            args = TOOLS[name][0].model_validate_json(raw)
            deps = {n - 1 for n in args.depends_on}
            if any(n < 0 or n >= index for n in deps):
                raise ValueError("Dependencies must refer to earlier calls")
            if name in {"propose_plan", "request_clarification"} and len(calls) != 1:
                raise ValueError("Approval and clarification must be standalone")
            if name == "finish" and index != len(calls) - 1:
                raise ValueError("Finish must be last")
            ids.add(call_id)
            parsed.append(Invocation(call_id, name, args, raw, deps))
    except (KeyError, TypeError, ValueError):
        raise ModelError("Invalid tool batch protocol or dependencies") from None
    for j, later in enumerate(parsed):
        for i, earlier in enumerate(parsed[:j]):
            if conflicts(earlier, later, workspace):
                later.dependencies.add(i)
    return parsed


def footprint(tool, workspace):
    if tool.name in BARRIERS or tool.name in {"search_files", "list_files"}:
        return None
    root = Path(workspace).resolve()
    raw = tool.args.path.replace("\\", "/")
    # Container absolute /workspace paths resolve to the same host footprint.
    raw = raw.removeprefix("/workspace/")
    path = (root / raw).resolve()
    if not path.is_relative_to(root):
        return None
    # Hard-linked files can alias a different path: serialize conservatively.
    if path.exists() and path.stat().st_nlink > 1:
        return None
    return str(path).replace("\\", "/").casefold().rstrip("/")


def conflicts(a, b, workspace):
    if a.name in BARRIERS or b.name in BARRIERS:
        return True
    if a.name != "write_file" and b.name != "write_file":
        return False
    left, right = footprint(a, workspace), footprint(b, workspace)
    return (
        left is None
        or right is None
        or (left == right or left.startswith(right + "/") or right.startswith(left + "/"))
    )


def trim_oldest(history):
    """Drop one assistant response and ALL its results, preserving the latest block."""
    end = next((i for i in range(3, len(history)) if history[i]["role"] == "assistant"), None)
    if end is None:
        return False
    del history[2:end]
    return True
