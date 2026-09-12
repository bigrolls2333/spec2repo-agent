from __future__ import annotations

import json
from typing import Any

from paper2repo_agent.schemas import HumanReview, ReproductionPlan, SpecValidationResult
from paper2repo_agent.utils import stable_id


def build_human_review(
    plan: ReproductionPlan,
    validation: SpecValidationResult,
) -> HumanReview:
    recommended = "approve" if validation.supported else "downgrade_scope"
    if validation.blocking_issues:
        recommended = "revise"

    confirmation_items = plan.required_confirmations or validation.required_confirmations
    confirmation_fields = [item.field_path for item in confirmation_items]
    evidence_refs = sorted(
        {
            evidence_id
            for item in [
                *confirmation_items,
                *validation.assumptions,
                *validation.missing_details,
            ]
            for evidence_id in getattr(item, "evidence_ids", [])
        }
    )

    return HumanReview(
        id=stable_id("review", plan.id),
        plan_id=plan.id,
        summary=(
            f"请确认论文《{plan.paper_title}》的复现计划。"
            f"当前规格支持度：{validation.support_level}。"
        ),
        plan_overview=_plan_overview(plan),
        scope_summary={
            "goal": plan.objective.goal,
            "compute_target": plan.user_constraints.compute_target,
            "dataset_policy": plan.user_constraints.dataset_policy,
            "device_target": plan.scope.device_target,
            "dataset_mode": plan.scope.dataset_mode,
            "max_runtime_minutes": plan.scope.max_training_minutes,
        },
        decision_requirements=confirmation_fields,
        evidence_refs=evidence_refs,
        confirmation_items=confirmation_items,
        inferred_assumptions=validation.assumptions,
        missing_details=validation.missing_details,
        risks=plan.risks,
        recommended_action=recommended,
    )


def build_decision_template(review: HumanReview) -> dict[str, Any]:
    return {
        "review_id": review.id,
        "global_action": "approve",
        "user_notes": "",
        "decisions": [
            {
                "item_id": item.field_path,
                "action": "accept",
                "value": item.suggested_value,
                "note": "",
            }
            for item in review.confirmation_items
        ],
    }


def render_human_review_markdown(review: HumanReview, plan: ReproductionPlan) -> str:
    template = build_decision_template(review)
    lines: list[str] = [
        f"# 人工确认：{plan.paper_title}",
        "",
        "## 复现目标",
        "",
        f"- 计划 ID：`{plan.id}`",
        f"- 建议动作：`{review.recommended_action}`",
        f"- 目标类型：`{plan.objective.goal}`",
        f"- 目标说明：{plan.objective.description}",
        "",
        "## 用户约束",
        "",
        f"- 复现目标：`{plan.user_constraints.reproduction_goal}`",
        f"- 计算资源：`{plan.user_constraints.compute_target}`",
        f"- 数据策略：`{plan.user_constraints.dataset_policy}`",
        f"- 运行时间限制：`{plan.user_constraints.max_runtime_minutes}` 分钟",
        f"- Python：`{plan.user_constraints.python_version}`",
        f"- 包管理器：`{plan.user_constraints.package_manager}`",
        "",
        "## 论文支撑事实",
        "",
    ]
    lines.extend([f"- {item}" for item in review.plan_overview] or ["- 暂无论文支撑事实摘要。"])
    if review.evidence_refs:
        lines.extend(["", "证据引用：`" + ", ".join(review.evidence_refs) + "`"])

    lines.extend(["", "## 候选计划", ""])
    lines.extend(
        [
            f"- 目标设备：`{plan.scope.device_target}`",
            f"- 数据模式：`{plan.scope.dataset_mode}`",
            f"- 是否尝试指标复现：`{plan.scope.metric_reproduction}`",
            f"- 选中的论文主张：`{', '.join(plan.selected_claims) or '无'}`",
            "",
            "### 执行步骤",
            "",
        ]
    )
    if not plan.execution_steps:
        lines.append("暂无执行步骤。")
    for step in plan.execution_steps:
        lines.extend(
            [
                f"- `{step.id}` {step.name}: {step.description}",
                f"  输出：{', '.join(step.outputs) or '无'}",
            ]
        )

    lines.extend(["", "## 需要确认的事项", ""])
    if not review.confirmation_items:
        lines.append("暂无需要确认的事项。")
    for item in review.confirmation_items:
        lines.extend(
            [
                f"### {item.field_path}",
                "",
                f"- 条目 ID：`{item.field_path}`",
                f"- 重要性：`{item.severity}`",
                f"- 问题：{item.question}",
                f"- 建议值：`{item.suggested_value}`",
                f"- 当前值：`{item.current_value}`",
                f"- 可选项：`{json.dumps(item.options or [], ensure_ascii=False)}`",
                f"- 原因：{item.reason}",
                f"- 证据 ID：`{', '.join(item.evidence_ids) or '无'}`",
                "",
            ]
        )

    lines.extend(["## 推断假设", ""])
    if not review.inferred_assumptions:
        lines.append("暂无推断假设。")
    for assumption in review.inferred_assumptions:
        lines.append(
            f"- `{assumption.field_path}` = `{assumption.assumed_value}` "
            f"（{assumption.risk} 风险）| 证据：`{', '.join(assumption.evidence_ids) or '无'}` "
            f"| {assumption.reason}"
        )

    lines.extend(["", "## 缺失细节", ""])
    if not review.missing_details:
        lines.append("暂无缺失细节。")
    for detail in review.missing_details:
        lines.append(
            f"- `{detail.field_path}` ({detail.severity}) | 证据："
            f"`{', '.join(detail.evidence_ids) or '无'}` | {detail.description} | "
            f"建议处理：{detail.suggested_resolution or '用户确认'}"
        )

    lines.extend(["", "## 风险", ""])
    if not review.risks:
        lines.append("暂无风险。")
    for risk in review.risks:
        lines.append(
            f"- `{risk.id}` ({risk.severity}): {risk.description} | 缓解方式：{risk.mitigation}"
        )

    lines.extend(["", "## 决策模板", "", "```json"])
    lines.append(json.dumps(template, ensure_ascii=False, indent=2))
    lines.extend(["```", ""])
    return "\n".join(lines)


def _plan_overview(plan: ReproductionPlan) -> list[str]:
    overview = [
        f"架构摘要：{plan.implementation_strategy.architecture_summary}",
        f"目标框架：{plan.scope.framework}",
        f"计划数据模式：{plan.scope.dataset_mode}",
    ]
    if plan.selected_claims:
        overview.append(f"选中的论文主张：{', '.join(plan.selected_claims)}")
    if plan.dependency_strategy.runtime_packages:
        overview.append(
            "运行依赖："
            + ", ".join(package.name for package in plan.dependency_strategy.runtime_packages)
        )
    return overview
