from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from paper2repo_agent.schemas import (
    ConfirmationItem,
    ConfirmationProposal,
    ConfirmationProposalSet,
    EvidenceStore,
    HumanDecisions,
    ReproductionPlan,
    SpecValidationResult,
    UserConstraints,
)
from paper2repo_agent.utils import stable_id


FIELD_PATH_PREFIXES = (
    "user_constraints.",
    "objective.",
    "scope.",
    "implementation_strategy.",
    "dependency_strategy.",
    "data_strategy.",
    "verification_strategy.",
    "execution_steps.",
)

FIELD_PATH_EXACT = {
    "selected_claims",
    "training.batch_size",
}

EXECUTION_POLICY_FIELDS = {
    "user_constraints.compute_target",
    "user_constraints.dataset_policy",
    "user_constraints.allow_network",
    "user_constraints.allow_large_downloads",
}

POLICY_CONFIRMATION_DEFS: tuple[dict[str, Any], ...] = (
    {
        "field_path": "user_constraints.compute_target",
        "question": "请确认本次规划和后续执行使用的计算资源范围。",
        "reason": "计算资源会影响依赖选择、smoke test 规模和执行限制。",
        "severity": "required",
        "options": ["cpu_smoke", "single_gpu", "multi_gpu"],
        "blocks": True,
    },
    {
        "field_path": "user_constraints.dataset_policy",
        "question": "请确认本次运行允许 Paper2Repo 如何处理数据集。",
        "reason": "数据策略会决定生成代码使用合成数据、用户提供数据，还是允许下载公开数据集。",
        "severity": "required",
        "options": ["synthetic_only", "user_provided", "download_allowed"],
        "blocks": True,
    },
    {
        "field_path": "user_constraints.allow_network",
        "question": "请确认后续执行阶段是否允许访问网络。",
        "reason": "网络访问会影响数据集下载、依赖安装和复现边界。",
        "severity": "required",
        "options": [False, True],
        "blocks": True,
    },
    {
        "field_path": "user_constraints.allow_large_downloads",
        "question": "请确认后续执行阶段是否允许下载大文件。",
        "reason": "大文件下载会消耗存储、带宽和运行时间预算。",
        "severity": "required",
        "options": [False, True],
        "blocks": True,
    },
    {
        "field_path": "user_constraints.max_runtime_minutes",
        "question": "请确认生成的 smoke run 或小规模运行的时间预算。",
        "reason": "运行时间预算会影响训练循环规模、验证超时设置和生成代码默认值。",
        "severity": "recommended",
        "options": None,
        "blocks": False,
    },
)


def build_merged_confirmations(
    validation: SpecValidationResult,
    constraints: UserConstraints,
    proposals: ConfirmationProposalSet,
    *,
    run_id: str,
    evidence_store: EvidenceStore | None = None,
    max_items: int = 14,
) -> list[ConfirmationItem]:
    """Build final HITL items from backend policy and LLM-proposed fields."""

    allowed_evidence_ids = _allowed_evidence_ids(evidence_store)
    policy_items = build_policy_confirmations(validation, constraints, run_id=run_id)
    proposal_items = validate_confirmation_proposals(
        proposals,
        run_id=run_id,
        allowed_evidence_ids=allowed_evidence_ids,
    )
    return merge_confirmation_items(policy_items, proposal_items, max_items=max_items)


def build_policy_confirmations(
    validation: SpecValidationResult,
    constraints: UserConstraints,
    *,
    run_id: str,
) -> list[ConfirmationItem]:
    items: list[ConfirmationItem] = []

    for item in validation.required_confirmations:
        if item.blocks_execution_if_unanswered or item.field_path.startswith("user_constraints."):
            items.append(item.model_copy(deep=True))

    payload = constraints.model_dump(mode="json")
    for definition in POLICY_CONFIRMATION_DEFS:
        field_path = str(definition["field_path"])
        if any(existing.field_path == field_path for existing in items):
            continue
        field_name = field_path.split(".", 1)[1]
        current_value = payload.get(field_name)
        items.append(
            ConfirmationItem(
                id=stable_id("confirm", run_id, "policy", field_path),
                field_path=field_path,
                question=str(definition["question"]),
                current_value=current_value,
                suggested_value=current_value,
                reason=str(definition["reason"]),
                severity=str(definition["severity"]),  # type: ignore[arg-type]
                options=definition["options"],
                blocks_execution_if_unanswered=bool(definition["blocks"]),
            )
        )
    return items


def validate_confirmation_proposals(
    proposals: ConfirmationProposalSet,
    *,
    run_id: str,
    allowed_evidence_ids: set[str],
) -> list[ConfirmationItem]:
    items: list[ConfirmationItem] = []
    seen: set[str] = set()
    for proposal in proposals.proposals:
        field_path = proposal.field_path.strip()
        if not field_path or not _allowed_field_path(field_path):
            continue
        if field_path in seen:
            continue
        seen.add(field_path)
        evidence_ids = [
            evidence_id
            for evidence_id in _dedupe(proposal.evidence_ids)
            if evidence_id in allowed_evidence_ids
        ]
        items.append(
            ConfirmationItem(
                id=stable_id(
                    "confirm",
                    run_id,
                    "llm",
                    field_path,
                    proposal.question[:160],
                ),
                field_path=field_path,
                question=proposal.question.strip() or f"请确认复现计划中的 `{field_path}`。",
                current_value=proposal.current_value,
                suggested_value=proposal.suggested_value,
                reason=_proposal_reason(proposal),
                severity=proposal.severity,
                options=_normalize_options(proposal.options, proposal.suggested_value),
                evidence_ids=evidence_ids,
                blocks_execution_if_unanswered=(
                    proposal.blocks_execution_if_unanswered
                    and field_path in EXECUTION_POLICY_FIELDS
                ),
            )
        )
    return items


def merge_confirmation_items(
    policy_items: list[ConfirmationItem],
    proposal_items: list[ConfirmationItem],
    *,
    max_items: int = 14,
) -> list[ConfirmationItem]:
    merged: list[ConfirmationItem] = []
    seen: set[str] = set()

    for item in policy_items:
        if item.field_path in seen:
            continue
        seen.add(item.field_path)
        merged.append(item)

    sorted_proposals = sorted(
        proposal_items,
        key=lambda item: (
            {"required": 0, "recommended": 1, "optional": 2}[item.severity],
            item.field_path,
        ),
    )
    for item in sorted_proposals:
        if item.field_path in seen:
            continue
        if len(merged) >= max_items:
            break
        seen.add(item.field_path)
        merged.append(item)
    return merged


def apply_human_decisions_to_plan(
    plan: ReproductionPlan,
    decisions: HumanDecisions,
) -> ReproductionPlan:
    updated = plan.model_copy(deep=True)
    for decision in decisions.decisions:
        if decision.action not in {"accept", "override"}:
            continue
        value = _decision_value(decision.value)
        _apply_decision_value(updated, decision.item_id, value)

    for item in updated.required_confirmations:
        for decision in decisions.decisions:
            if item.field_path != decision.item_id or decision.action not in {"accept", "override"}:
                continue
            value = _decision_value(decision.value)
            item.current_value = value
            item.suggested_value = value
    return ReproductionPlan.model_validate(updated.model_dump(mode="json"))


def _apply_decision_value(plan: ReproductionPlan, field_path: str, value: Any) -> None:
    if field_path == "training.batch_size":
        synthetic = plan.data_strategy.synthetic_data_spec
        if synthetic is not None:
            current_batch_size = _nested_get(synthetic, "batch_size", 2)
            _nested_set(synthetic, "batch_size", _safe_int(value, current_batch_size))
        return

    if field_path == "user_constraints.compute_target":
        compute_target = str(value or plan.user_constraints.compute_target)
        if compute_target in {"cpu_smoke", "single_gpu", "multi_gpu"}:
            plan.user_constraints.compute_target = compute_target  # type: ignore[assignment]
            plan.scope.device_target = "cpu" if compute_target == "cpu_smoke" else "cuda"
        return

    if field_path == "user_constraints.dataset_policy":
        dataset_policy = str(value or plan.user_constraints.dataset_policy)
        if dataset_policy in {"synthetic_only", "user_provided", "download_allowed"}:
            plan.user_constraints.dataset_policy = dataset_policy  # type: ignore[assignment]
            dataset_mode = {
                "synthetic_only": "synthetic",
                "user_provided": "user_provided",
                "download_allowed": "download",
            }[dataset_policy]
            plan.scope.dataset_mode = dataset_mode  # type: ignore[assignment]
            plan.data_strategy.mode = dataset_mode  # type: ignore[assignment]
        return

    if field_path == "user_constraints.allow_network":
        plan.user_constraints.allow_network = _safe_bool(value, plan.user_constraints.allow_network)
        return

    if field_path == "user_constraints.allow_large_downloads":
        plan.user_constraints.allow_large_downloads = _safe_bool(
            value,
            plan.user_constraints.allow_large_downloads,
        )
        return

    if field_path == "user_constraints.max_runtime_minutes":
        minutes = _safe_int(value, plan.user_constraints.max_runtime_minutes)
        plan.user_constraints.max_runtime_minutes = minutes
        plan.scope.max_training_minutes = minutes
        return

    if field_path == "user_constraints.package_manager":
        package_manager = str(value or plan.user_constraints.package_manager)
        if package_manager in {"pip", "uv", "poetry", "conda"}:
            plan.user_constraints.package_manager = package_manager  # type: ignore[assignment]
            plan.dependency_strategy.package_manager = package_manager  # type: ignore[assignment]
        return

    if _set_nested_value(plan, field_path, value):
        return


def _allowed_field_path(field_path: str) -> bool:
    if field_path in FIELD_PATH_EXACT:
        return True
    return any(
        field_path == prefix.rstrip(".") or field_path.startswith(prefix)
        for prefix in FIELD_PATH_PREFIXES
    )


def _allowed_evidence_ids(evidence_store: EvidenceStore | None) -> set[str]:
    if evidence_store is None:
        return set()
    return {item.id for item in evidence_store.items}


def _proposal_reason(proposal: ConfirmationProposal) -> str:
    parts = [proposal.reason.strip()]
    if proposal.decision_effect:
        parts.append(f"决策影响：{proposal.decision_effect.strip()}")
    if proposal.risk:
        parts.append(f"风险等级：{proposal.risk}。")
    return " ".join(part for part in parts if part)


def _normalize_options(options: list[Any], suggested_value: Any) -> list[Any] | None:
    normalized = list(options)
    if (
        normalized
        and suggested_value not in (None, "")
        and not any(_json_equivalent(option, suggested_value) for option in normalized)
    ):
        normalized.insert(0, suggested_value)
    deduped = _dedupe_json_values(normalized)
    return deduped[:6] or None


def _dedupe_json_values(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        key = json.dumps(value, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _json_equivalent(left: Any, right: Any) -> bool:
    return json.dumps(left, ensure_ascii=False, sort_keys=True) == json.dumps(
        right,
        ensure_ascii=False,
        sort_keys=True,
    )


def _decision_value(value: Any) -> Any:
    if isinstance(value, str):
        trimmed = value.strip()
        if not trimmed:
            return trimmed
        if trimmed.lower() == "true":
            return True
        if trimmed.lower() == "false":
            return False
        if trimmed.lower() == "null":
            return None
        if re.fullmatch(r"-?\d+", trimmed):
            return int(trimmed)
        if re.fullmatch(r"-?\d+\.\d+", trimmed):
            return float(trimmed)
        if trimmed.startswith("{") or trimmed.startswith("["):
            try:
                return json.loads(trimmed)
            except json.JSONDecodeError:
                return trimmed
        return trimmed
    return value


def _set_nested_value(root: Any, field_path: str, value: Any) -> bool:
    if "." not in field_path:
        if hasattr(root, field_path):
            setattr(root, field_path, value)
            return True
        return False

    parts = field_path.split(".")
    current = root
    for part in parts[:-1]:
        current = _resolve_nested_part(current, part)
        if current is None:
            return False
    return _assign_nested_part(current, parts[-1], value)


def _resolve_nested_part(current: Any, part: str) -> Any | None:
    if isinstance(current, list):
        if not part.isdigit():
            return None
        index = int(part)
        if index < 0 or index >= len(current):
            return None
        return current[index]
    if isinstance(current, dict):
        return current.get(part)
    return getattr(current, part, None)


def _assign_nested_part(current: Any, part: str, value: Any) -> bool:
    if isinstance(current, list):
        if not part.isdigit():
            return False
        index = int(part)
        if index < 0 or index >= len(current):
            return False
        current[index] = value
        return True
    if isinstance(current, dict):
        current[part] = value
        return True
    if isinstance(current, BaseModel) and part in current.__class__.model_fields:
        data = current.model_dump(mode="json")
        data[part] = value
        refreshed = current.__class__.model_validate(data)
        for field_name in current.__class__.model_fields:
            setattr(current, field_name, getattr(refreshed, field_name))
        return True
    if hasattr(current, part):
        setattr(current, part, value)
        return True
    return False


def _nested_get(current: Any, key: str, fallback: Any = None) -> Any:
    if isinstance(current, dict):
        return current.get(key, fallback)
    return getattr(current, key, fallback)


def _nested_set(current: Any, key: str, value: Any) -> None:
    if isinstance(current, dict):
        current[key] = value
        return
    setattr(current, key, value)


def _safe_int(value: Any, fallback: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return fallback


def _safe_bool(value: Any, fallback: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "y", "1"}:
            return True
        if lowered in {"false", "no", "n", "0"}:
            return False
    return fallback


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


# ═════════════════════════════════════════════════════════════════════
# Agentic HITL — in-loop trigger rendering.
# ═════════════════════════════════════════════════════════════════════


def render_hitl_trigger_prompt(
    trigger: str,
    trace: Any,
    context: dict | None = None,
) -> str:
    """Format a human-readable prompt for an in-loop HITL trigger.

    Args:
        trigger: One of ``"on_stall"``, ``"on_smoke_failure_low_conf"``,
            ``"before_plan_invalid"``, ``"before_critical_write"``,
            ``"on_plan_assumption_falsified"``.
        trace: The ``AgentTrace`` at the trigger point (supports getattr).
        context: Trigger-specific context dict.

    Returns:
        Markdown-formatted prompt for the human to read and respond to.
    """
    ctx = context or {}

    if trigger == "on_stall":
        return _render_stall_prompt(trace, ctx)
    if trigger == "on_smoke_failure_low_conf":
        return _render_smoke_failure_prompt(trace, ctx)
    if trigger == "before_plan_invalid":
        return _render_plan_invalid_prompt(trace, ctx)
    if trigger == "before_critical_write":
        return _render_critical_write_prompt(trace, ctx)
    if trigger == "on_plan_assumption_falsified":
        return _render_assumption_falsified_prompt(trace, ctx)
    return (
        f"## Unknown HITL trigger: {trigger}\n\n"
        f"Context: {json.dumps(ctx, ensure_ascii=False, default=str)}"
    )


def _render_stall_prompt(trace: Any, ctx: dict) -> str:
    stall_reason = ctx.get("stall_reason", "unknown")
    recent = _fmt_recent_actions(trace, n=6)
    return (
        "## ⚠️ Agent Progress Stall\n\n"
        f"**File**: `{getattr(trace, 'file_path', 'unknown')}`\n"
        f"**Steps so far**: {getattr(trace, 'total_steps', 0)}\n"
        f"**Stall reason**: {stall_reason}\n\n"
        "The agent appears to be going in circles. What should it do?\n\n"
        "**Respond with one of**:\n"
        "- `change_approach`: Try a completely different strategy\n"
        "- `hint: <your hint>`: Give the agent a specific directional hint\n"
        "- `fallback`: Force fallback to single-shot template generation\n"
        "- `continue`: Let the agent try one more round unchallenged\n\n"
        "### Recent actions:\n"
        f"{recent}\n"
    )


def _render_smoke_failure_prompt(trace: Any, ctx: dict) -> str:
    error_info = ctx.get("error_info", "unknown error")
    last_code = ctx.get("last_code", "(not available)")
    return (
        "## ❌ Smoke Forward Failed + Low Confidence\n\n"
        f"**File**: `{getattr(trace, 'file_path', 'unknown')}`\n"
        f"**Error**: {error_info}\n\n"
        "The agent's smoke test failed and it seems uncertain about how "
        "to fix it.\n\n"
        "**Respond with one of**:\n"
        "- `diagnose: <your diagnosis>`: Tell the agent exactly what's wrong\n"
        "- `hint: <your hint>`: Give a directional hint without full diagnosis\n"
        "- `fallback`: Force fallback to single-shot generation\n"
        "- `continue`: Let the agent try to fix on its own\n\n"
        "### Last code:\n```python\n"
        f"{_clip(last_code, 2000)}\n"
        "```\n"
    )


def _render_plan_invalid_prompt(trace: Any, ctx: dict) -> str:
    plan_step = ctx.get("plan_step", "unknown")
    reason = ctx.get("reason", "budget exceeded or stall repeated")
    return (
        "## 🔄 Plan Assumption May Be Invalid\n\n"
        f"**File**: `{getattr(trace, 'file_path', 'unknown')}`\n"
        f"**Suspected plan step**: `{plan_step}`\n"
        f"**Reason**: {reason}\n\n"
        "The agent cannot complete this file and suspects a plan-level "
        "assumption is wrong. Emitting **plan_assumption_invalid** is a "
        "cross-phase signal that will trigger a plan rollback.\n\n"
        "**Respond with one of**:\n"
        "- `accept`: Mark the assumption as invalid → plan_assumption_invalid\n"
        "- `revise: <corrected assumption>`: Override with a corrected assumption\n"
        "- `continue`: Ignore and try the same approach once more\n"
        "- `fallback`: Degrade to single-shot WITHOUT plan-level signalling\n"
    )


def _render_critical_write_prompt(trace: Any, ctx: dict) -> str:
    tool_args = ctx.get("tool_args", {})
    rel_path = tool_args.get("rel_path", "unknown")
    content_preview = _clip(str(tool_args.get("content", "")), 3000)
    return (
        "## 📝 Critical Write Approaching\n\n"
        f"**File to write**: `{rel_path}`\n"
        f"**Agent task**: `{getattr(trace, 'file_path', 'unknown')}`\n\n"
        "The agent is about to write to the generated repository. "
        "Review the code before approving.\n\n"
        "**Respond with one of**:\n"
        "- `approve`: Allow the write to proceed normally\n"
        "- `reject: <reason>`: Block this write (agent will see rejection)\n"
        "- `edit: <instructions>`: Tell the agent what to change before writing\n\n"
        "### Code preview:\n```python\n"
        f"{content_preview}\n"
        "```\n"
    )


def _render_assumption_falsified_prompt(trace: Any, ctx: dict) -> str:
    plan_step = ctx.get("plan_step", "unknown")
    return (
        "## 💡 Plan Assumption Falsified (First Time)\n\n"
        f"**File**: `{getattr(trace, 'file_path', 'unknown')}`\n"
        f"**Falsified step**: `{plan_step}`\n\n"
        "A plan assumption has been contradicted by runtime evidence for "
        "the first time. This is the most information-dense moment in the "
        "plan-execute loop.\n\n"
        "**Respond with one of**:\n"
        "- `accept_rollback`: Accept automatic local rollback of this assumption\n"
        "- `revise: <corrected assumption>`: Manually correct the assumption\n"
        "- `continue_as_is`: Keep the current assumption and force continue\n"
    )


def _fmt_recent_actions(trace: Any, n: int = 6) -> str:
    actions = getattr(trace, "actions", []) or []
    recent = actions[-n:]
    lines: list[str] = []
    for a in recent:
        action_type = getattr(a, "action_type", "?")
        tool_name = getattr(a, "tool_name", None)
        reasoning = getattr(a, "reasoning", "") or ""
        prefix = f"- [{action_type}] `{tool_name}`" if tool_name else f"- [{action_type}]"
        lines.append(f"{prefix}: {_clip(reasoning, 120)}")
    return "\n".join(lines) if lines else "(no actions yet)"


def _clip(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...<truncated {len(text) - limit} chars>"
