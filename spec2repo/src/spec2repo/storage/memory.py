"""Bounded task notes and tool evidence; callers hold the fenced run lock."""

import copy
import hashlib
import json
import re
from uuid import uuid4

from sqlalchemy import func, select

from spec2repo.storage.models import TaskMemory, ToolEvidence

MAX_ENTRIES = 256
MAX_BYTES = 4 * 1024 * 1024


def redact(text, secret=None):
    if secret:
        text = text.replace(secret, "[REDACTED]")
    return re.sub(
        r"(?i)((?:api[_-]?key|password|authorization|token)\s*[:=]\s*)([^\s,;]+)",
        r"\1[REDACTED]", text,
    )


def current(session, run, state):
    row = session.get(TaskMemory, (run.id, run.generation))
    if row is not None:
        return row
    previous = None
    if state.snapshot_hash:
        previous = session.scalar(select(TaskMemory).where(
            TaskMemory.run_id == run.id,
            TaskMemory.spec_hash == run.spec_hash,
            TaskMemory.snapshot_hash == state.snapshot_hash,
        ).order_by(TaskMemory.generation.desc()))
    row = TaskMemory(
        run_id=run.id, generation=run.generation, spec_hash=run.spec_hash,
        snapshot_hash=None,
        body=copy.deepcopy(previous.body) if previous else {
            "model_notes": {}, "observations": [], "failures": [],
        },
    )
    session.add(row)
    session.flush()
    return row


def evidence(session, run, memory, identifier):
    row = session.get(ToolEvidence, identifier)
    references = {x["evidence_id"] for x in (
        memory.body["observations"] + memory.body["failures"]
    )}
    references.update(memory.body["model_notes"].get("evidence_ids", []))
    if (row is None or row.run_id != run.id or row.spec_hash != run.spec_hash
            or (row.generation != run.generation and identifier not in references)):
        raise ValueError("Evidence unavailable in this task snapshot")
    return row


def capture(session, run, memory, name, path, result, secret=None):
    count, size = session.execute(select(
        func.count(), func.coalesce(func.sum(ToolEvidence.size_bytes), 0)
    ).where(ToolEvidence.run_id == run.id)).one()
    output = redact(result.get("output", ""), secret)
    bounded = output.encode()[:65536].decode("utf-8", errors="ignore")
    body = {**result, "output": bounded,
            "capture_limited": len(output.encode()) > 65536 or name == "terminal"}
    size_bytes = len(json.dumps(body, ensure_ascii=False).encode())
    preview = bounded if len(bounded) <= 1600 else bounded[:800] + "\n…\n" + bounded[-800:]
    reply = {**body, "output": preview, "truncated": len(bounded) > 1600}
    if count >= MAX_ENTRIES or size + size_bytes > MAX_BYTES:
        return {**reply, "evidence_unavailable": "Task evidence quota exhausted"}
    identifier = str(uuid4())
    session.add(ToolEvidence(
        id=identifier, run_id=run.id, generation=run.generation,
        spec_hash=run.spec_hash, body=body, size_bytes=size_bytes,
    ))
    observation = {
        "evidence_id": identifier, "tool": name, "path": path, "ok": result["ok"],
        "exit_code": result.get("exit_code"),
        "sha256": hashlib.sha256(bounded.encode()).hexdigest(),
    }
    updated = copy.deepcopy(memory.body)
    updated["observations"] = (updated["observations"] + [observation])[-12:]
    if not result["ok"]:
        updated["failures"] = (updated["failures"] + [observation])[-4:]
    memory.body = updated
    return {**reply, "evidence_id": identifier, "captured_chars": len(bounded),
            "captured_sha256": observation["sha256"]}
