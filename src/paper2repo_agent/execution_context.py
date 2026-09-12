from __future__ import annotations

import json
import re
from typing import Any

from paper2repo_agent.evidence import query_evidence_by_field
from paper2repo_agent.schemas import (
    CodeImplementationPlan,
    FileGenerationTask,
    PatchPlan,
    RepairTask,
    RetrievalReport,
    RetrievedContextItem,
)
from paper2repo_agent.security import redact_text
from paper2repo_agent.utils import stable_id


def render_retrieval_report(report: RetrievalReport, *, max_chars: int = 12000) -> str:
    blocks: list[str] = []
    for item in report.items:
        header = f"[{item.source_type}] {item.title}"
        if item.field_path:
            header += f" ({item.field_path})"
        blocks.append(f"{header}\n{item.content}")
    text = "\n\n".join(blocks)
    return text[:max_chars]


class ExecutionContextRetriever:
    def __init__(self, state: dict[str, Any]) -> None:
        self.state = state
        self.paths = state["paths"]
        self.plan = state["approved_plan"].plan
        self.spec = state["paper_spec"]
        self.validation = state["validation_result"]
        self.coding_plan = state.get("coding_plan")
        self.evidence_by_id = {item.id: item for item in state["evidence_store"].items}
        self.task_by_path = {task.path: task for task in state.get("file_tasks", [])}

    def for_file_task(self, task: FileGenerationTask) -> RetrievalReport:
        items: list[RetrievedContextItem] = []
        selected_evidence_ids: list[str] = []
        seen_item_keys: set[tuple[str, str]] = set()
        seen_evidence_ids: set[str] = set()

        self._append_plan_items(task, items, seen_item_keys)
        self._append_coding_plan_item(task, items, seen_item_keys)
        self._append_code_implementation_item(task, items, seen_item_keys)
        self._append_validation_items(task, items, seen_item_keys)

        for evidence_id in task.relevant_evidence_ids[:8]:
            if evidence_id in seen_evidence_ids:
                continue
            evidence = self.evidence_by_id.get(evidence_id)
            if evidence is None:
                continue
            selected_evidence_ids.append(evidence_id)
            seen_evidence_ids.add(evidence_id)
            items.append(
                RetrievedContextItem(
                    source_type="evidence",
                    source_id=evidence.id,
                    title=f"{evidence.section or 'paper'} evidence",
                    field_path=None,
                    content=self._evidence_text(evidence.text),
                    score=1.0,
                )
            )

        for field_path in task.plan_fields[:6]:
            for row in query_evidence_by_field(self.paths.evidence_sqlite, field_path)[:2]:
                evidence_id = str(row.get("id") or "")
                if not evidence_id or evidence_id in seen_evidence_ids:
                    continue
                selected_evidence_ids.append(evidence_id)
                seen_evidence_ids.add(evidence_id)
                items.append(
                    RetrievedContextItem(
                        source_type="evidence",
                        source_id=evidence_id,
                        title=f"{row.get('section') or 'paper'} evidence",
                        field_path=field_path,
                        content=self._evidence_text(str(row.get("text") or "")),
                        score=0.9,
                    )
                )

        summary = f"{len(items)} context items, {len(selected_evidence_ids)} evidence snippets."
        return RetrievalReport(
            id=stable_id("retrieval", task.id, task.path),
            task_id=task.id,
            file_path=task.path,
            mode="generation",
            query_terms=self._query_terms_for_task(task),
            items=items[:14],
            selected_evidence_ids=selected_evidence_ids[:12],
            truncated=len(items) > 14,
            summary=summary,
        )

    def for_repair_task(
        self, repair_task: RepairTask, patch_plan: PatchPlan | None
    ) -> RetrievalReport:
        items: list[RetrievedContextItem] = [
            RetrievedContextItem(
                source_type="failure",
                source_id=repair_task.id,
                title="repair task",
                content=redact_text(repair_task.failure_summary),
                score=1.0,
            ),
            RetrievedContextItem(
                source_type="failure",
                source_id=repair_task.id,
                title="repair strategy",
                content=repair_task.repair_strategy,
                score=0.95,
            ),
        ]
        selected_evidence_ids: list[str] = []
        seen_evidence_ids: set[str] = set()
        seen_item_keys: set[tuple[str, str]] = {
            ("failure", "repair task"),
            ("failure", "repair strategy"),
        }

        if patch_plan is not None:
            items.append(
                RetrievedContextItem(
                    source_type="failure",
                    source_id=patch_plan.id,
                    title="patch plan",
                    content=json.dumps(
                        patch_plan.model_dump(mode="json"), ensure_ascii=False, indent=2
                    )[:2000],
                    score=0.9,
                )
            )

        for rel_path in repair_task.target_files[:6]:
            task = self.task_by_path.get(rel_path)
            if task is None:
                continue
            self._append_plan_items(task, items, seen_item_keys)
            self._append_coding_plan_item(task, items, seen_item_keys)
            self._append_code_implementation_item(task, items, seen_item_keys)
            if task.file_contract is not None:
                items.append(
                    RetrievedContextItem(
                        source_type="contract",
                        source_id=rel_path,
                        title=f"{rel_path} contract",
                        content=json.dumps(
                            task.file_contract.model_dump(mode="json"), ensure_ascii=False, indent=2
                        )[:2000],
                        score=0.9,
                    )
                )
            for evidence_id in task.relevant_evidence_ids[:6]:
                if evidence_id in seen_evidence_ids:
                    continue
                evidence = self.evidence_by_id.get(evidence_id)
                if evidence is None:
                    continue
                seen_evidence_ids.add(evidence_id)
                selected_evidence_ids.append(evidence_id)
                items.append(
                    RetrievedContextItem(
                        source_type="evidence",
                        source_id=evidence.id,
                        title=f"{rel_path} evidence",
                        content=self._evidence_text(evidence.text),
                        score=0.85,
                    )
                )

        summary = (
            f"{len(items)} repair context items, {len(selected_evidence_ids)} evidence snippets."
        )
        return RetrievalReport(
            id=stable_id("repair_context", repair_task.id, ",".join(repair_task.target_files)),
            task_id=repair_task.id,
            file_path=",".join(repair_task.target_files),
            mode="repair",
            query_terms=self._query_terms_for_repair(repair_task),
            items=items[:16],
            selected_evidence_ids=selected_evidence_ids[:12],
            truncated=len(items) > 16,
            summary=summary,
        )

    def _append_plan_items(
        self,
        task: FileGenerationTask,
        items: list[RetrievedContextItem],
        seen_item_keys: set[tuple[str, str]],
    ) -> None:
        candidates = [
            (
                "approved objective",
                json.dumps(
                    self.plan.objective.model_dump(mode="json"), ensure_ascii=False, indent=2
                ),
            ),
            (
                "implementation strategy",
                json.dumps(
                    self.plan.implementation_strategy.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
            ),
            (
                "data strategy",
                json.dumps(
                    self.plan.data_strategy.model_dump(mode="json"), ensure_ascii=False, indent=2
                ),
            ),
            (
                "verification strategy",
                json.dumps(
                    self.plan.verification_strategy.model_dump(mode="json"),
                    ensure_ascii=False,
                    indent=2,
                ),
            ),
        ]
        for title, content in candidates:
            key = ("plan", title)
            if key in seen_item_keys:
                continue
            seen_item_keys.add(key)
            items.append(
                RetrievedContextItem(
                    source_type="plan",
                    source_id=task.id,
                    title=title,
                    content=content[:1600],
                    score=0.8,
                )
            )

    def _append_coding_plan_item(
        self,
        task: FileGenerationTask,
        items: list[RetrievedContextItem],
        seen_item_keys: set[tuple[str, str]],
    ) -> None:
        if self.coding_plan is None:
            return
        module_plan = next(
            (item for item in self.coding_plan.module_plans if item.path == task.path), None
        )
        if module_plan is None:
            return
        key = ("coding_plan", task.path)
        if key in seen_item_keys:
            return
        seen_item_keys.add(key)
        items.append(
            RetrievedContextItem(
                source_type="coding_plan",
                source_id=module_plan.module_name,
                title=f"{module_plan.module_name} module plan",
                content=json.dumps(
                    module_plan.model_dump(mode="json"), ensure_ascii=False, indent=2
                )[:1800],
                score=0.85,
            )
        )

    def _append_code_implementation_item(
        self,
        task: FileGenerationTask,
        items: list[RetrievedContextItem],
        seen_item_keys: set[tuple[str, str]],
    ) -> None:
        if self.coding_plan is None or self.coding_plan.code_implementation_plan is None:
            return
        key = ("implementation_plan", task.path)
        if key in seen_item_keys:
            return
        seen_item_keys.add(key)
        plan_slice = self._implementation_slice(
            task.path, self.coding_plan.code_implementation_plan
        )
        items.append(
            RetrievedContextItem(
                source_type="implementation_plan",
                source_id=self.coding_plan.code_implementation_plan.id,
                title=f"{task.path} code implementation plan",
                content=json.dumps(plan_slice, ensure_ascii=False, indent=2)[:3200],
                score=0.95,
            )
        )

    def _implementation_slice(self, path: str, plan: CodeImplementationPlan) -> dict[str, Any]:
        if path.endswith("model.py"):
            return {
                "summary": plan.summary,
                "input_tensors": [item.model_dump(mode="json") for item in plan.input_tensors],
                "output_tensors": [item.model_dump(mode="json") for item in plan.output_tensors],
                "model_components": [
                    item.model_dump(mode="json") for item in plan.model_components
                ],
                "equation_notes": plan.equation_notes,
                "paper_alignment_checks": plan.paper_alignment_checks,
            }
        if path.endswith("losses.py"):
            return {
                "summary": plan.summary,
                "loss_terms": [item.model_dump(mode="json") for item in plan.loss_terms],
                "output_tensors": [item.model_dump(mode="json") for item in plan.output_tensors],
                "equation_notes": plan.equation_notes,
                "paper_alignment_checks": plan.paper_alignment_checks,
            }
        if path.endswith("dataset.py") or path.endswith("data.py"):
            return {
                "summary": plan.summary,
                "input_tensors": [item.model_dump(mode="json") for item in plan.input_tensors],
                "data_flow": plan.data_flow,
                "paper_alignment_checks": plan.paper_alignment_checks,
            }
        if path.endswith("train.py"):
            return {
                "summary": plan.summary,
                "data_flow": plan.data_flow,
                "training_flow": plan.training_flow,
                "loss_terms": [item.model_dump(mode="json") for item in plan.loss_terms],
                "paper_alignment_checks": plan.paper_alignment_checks,
            }
        return {
            "summary": plan.summary,
            "paper_alignment_checks": plan.paper_alignment_checks,
        }

    def _append_validation_items(
        self,
        task: FileGenerationTask,
        items: list[RetrievedContextItem],
        seen_item_keys: set[tuple[str, str]],
    ) -> None:
        for collection_name, collection in [
            ("assumption", self.validation.assumptions[:4]),
            ("missing_detail", self.validation.missing_details[:4]),
        ]:
            for item in collection:
                if task.plan_fields and not any(
                    field in item.field_path for field in task.plan_fields
                ):
                    continue
                title = f"{collection_name}: {item.field_path}"
                key = ("validation", title)
                if key in seen_item_keys:
                    continue
                seen_item_keys.add(key)
                items.append(
                    RetrievedContextItem(
                        source_type="validation",
                        source_id=item.id,
                        title=title,
                        field_path=item.field_path,
                        content=json.dumps(
                            item.model_dump(mode="json"), ensure_ascii=False, indent=2
                        )[:1400],
                        score=0.7,
                    )
                )

    def _query_terms_for_task(self, task: FileGenerationTask) -> list[str]:
        parts = [task.path, task.purpose, *task.plan_fields]
        return self._terms(parts)

    def _query_terms_for_repair(self, repair_task: RepairTask) -> list[str]:
        parts = [
            repair_task.failure_category or "repair",
            repair_task.failure_summary,
            *repair_task.target_files,
        ]
        return self._terms(parts)

    def _terms(self, parts: list[str]) -> list[str]:
        tokens: list[str] = []
        for part in parts:
            tokens.extend(re.findall(r"[A-Za-z0-9_./-]+", part.lower()))
        ordered = list(dict.fromkeys(token for token in tokens if len(token) > 2))
        return ordered[:20]

    def _evidence_text(self, text: str) -> str:
        normalized = " ".join((text or "").split())
        return normalized[:900]
