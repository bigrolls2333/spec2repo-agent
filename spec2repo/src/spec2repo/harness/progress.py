"""Conservative evidence-based loop detection, scoped to one execution attempt."""

import hashlib
import json
import os
import stat
from collections import deque


class AgentStalled(RuntimeError):
    """Repeated identical failures continued after a corrective observation."""


def workspace_digest(root):
    """Bounded content fingerprint. Unknown/unsafe trees disable detection, not execution."""
    if root is None:
        return None
    digest = hashlib.sha256()
    size = count = 0
    try:
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs.sort()
            count += len(dirs) + len(files)
            if count > 1000:
                return None
            for name in dirs + files:
                path = root / os.path.relpath(os.path.join(directory, name), root)
                info = path.lstat()
                if path.is_symlink() or getattr(path, "is_junction", lambda: False)():
                    return None
                if not stat.S_ISDIR(info.st_mode) and (
                    not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                ):
                    return None
            dirs[:] = [d for d in dirs if d not in {"__pycache__", ".pytest_cache"}]
            for name in sorted(files):
                path = root / os.path.relpath(os.path.join(directory, name), root)
                info = path.stat()
                size += info.st_size
                if count > 1000 or size > 16 * 1024 * 1024:
                    return None
                with path.open("rb") as source:
                    data = source.read(16 * 1024 * 1024 + 1)
                after = path.stat()
                if len(data) != info.st_size or after.st_mtime_ns != info.st_mtime_ns:
                    return None
                digest.update(path.relative_to(root).as_posix().encode() + b"\0")
                digest.update(hashlib.sha256(data).digest())
        return digest.hexdigest()
    except OSError:
        return None


class ProgressMonitor:
    VERSION = "identical-evidence-v1"

    def __init__(self):
        self.recent = deque(maxlen=24)
        self.feedback = None
        self.stalled = False

    def observe(self, name, args, result, before, after, call):
        if before is None or after is None:
            self.recent.clear()
            return None
        if before != after:
            self.recent.clear()
            self.feedback = None
            return None
        if name not in {"terminal", "read_file", "search_files", "list_files"}:
            return None
        if name == "terminal" and result.get("ok"):
            self.recent.clear()
            self.feedback = None
            return None
        if "output" not in result:
            return None
        key = hashlib.sha256(json.dumps({
            "tool": name, "args": args.model_dump(exclude={"depends_on"}),
            "workspace": after, "ok": result["ok"], "exit_code": result.get("exit_code"),
            "output": result.get("captured_sha256", hashlib.sha256(
                result["output"].encode()).hexdigest()),
        }, sort_keys=True).encode()).hexdigest()
        if any(r[0] == key and r[1] == call for r in self.recent):
            return None
        self.recent.append((key, call, result.get("evidence_id")))
        matches = [r for r in self.recent if r[0] == key]
        # Multiple copies inside ONE model response are not independent failed decisions.
        repeats = len({r[1] for r in matches})
        if repeats not in {3, 5}:
            return None
        self.stalled = self.stalled or (name == "terminal" and repeats == 5)
        self.feedback = {
            "kind": "repeated_failure" if not result["ok"] else "repeated_read",
            "tool": name, "repeated_turns": repeats,
            "evidence_ids": [r[2] for r in matches if r[2]][-5:],
            "action": "stop" if self.stalled else "reconsider",
            "message": "Repeated identical results without observed workspace changes. "
                       "Inspect the cited evidence, identify missing information, then choose "
                       "a different implementation or revise the plan. Do not repeat blindly.",
        }
        return self.feedback
