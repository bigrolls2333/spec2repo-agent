"""Outcome and observable-process metrics; missing usage is never zero cost."""

from collections import Counter


def summarize(runs):
    successes = sum(r.get("accepted") is True for r in runs)
    calls = known = tokens = interventions = 0
    first_pass = repair_eligible = repaired = 0
    failures = Counter()
    repeated_failures = repeated_reads = 0
    for run in runs:
        seen = {}
        reports = run.get("verifications", [])
        if reports:
            first_pass += reports[0].get("outcome") == "PASSED"
            if reports[0].get("outcome") == "FAILED":
                repair_eligible += 1
                repaired += run.get("accepted") is True
        for event in run.get("events", []):
            kind, payload = event["type"], event["payload"]
            if kind == "agent.tool_observed":
                state = payload.get("workspace_before")
                if not state or state != payload.get("workspace_after"):
                    seen.clear()
                elif payload.get("captured_sha256"):
                    key = (payload.get("generation"), payload.get("tool"),
                           payload.get("arguments_hash"), state, payload["captured_sha256"],
                           payload.get("ok"))
                    if key in seen and seen[key] != payload["call"]:
                        repeated_failures += payload.get("ok") is False
                        repeated_reads += payload.get("tool") in {"read_file", "search_files", "list_files"}
                    seen[key] = payload["call"]
            if kind == "agent.call_reserved":
                calls += 1
            if kind == "agent.model_returned":
                usage = payload.get("usage") or {}
                value = usage.get("total_tokens")
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    known += 1
                    tokens += value
            if kind == "agent.progress_intervention":
                interventions += 1
            if kind in {"agent.model_failed", "agent.protocol_failed", "agent.tool_failed"}:
                failures[payload.get("category", "unknown")] += 1
        if not run.get("accepted"):
            failures["run:" + run.get("status", "unknown")] += 1
    total = len(runs)
    return {
        "tasks": total, "successes": successes,
        "success_rate": successes / total if total else None,
        "first_pass_rate": first_pass / total if total else None,
        "repair_eligible": repair_eligible,
        "repair_success_rate": repaired / repair_eligible if repair_eligible else None,
        "model_calls": calls, "calls_with_usage": known, "known_tokens": tokens,
        "usage_complete": known == calls,
        "tokens_per_success_including_failures": (
            tokens / successes if successes and known == calls else None
        ),
        "elapsed_seconds_per_success_including_failures": (
            sum(r["elapsed_seconds"] for r in runs) / successes
            if successes and all(r.get("elapsed_seconds") is not None for r in runs) else None
        ),
        "interventions": interventions, "failure_counts": dict(failures),
        "repeated_failure_operations": repeated_failures,
        "repeated_read_operations": repeated_reads,
        "cost": None,
    }
