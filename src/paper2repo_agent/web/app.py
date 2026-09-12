from __future__ import annotations

import sqlite3
import threading
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from paper2repo_agent.artifacts import (
    count_approval_iterations,
    list_approval_artifacts,
    make_revision_record,
    next_approval_iteration,
    prepare_run_dirs,
    write_approval_iteration,
    write_manifest,
)
from paper2repo_agent.config import Settings
from paper2repo_agent.evidence import (
    collect_evidence_refs,
    collect_plan_semantic_evidence_refs,
    write_evidence_databases,
    write_evidence_files,
)
from paper2repo_agent.graphs.execute import run_execute
from paper2repo_agent.graphs.plan import run_plan
from paper2repo_agent.hitl import apply_human_decisions_to_plan
from paper2repo_agent.llm import revise_plan_with_llm
from paper2repo_agent.review import build_human_review, render_human_review_markdown
from paper2repo_agent.schemas import (
    ApprovedPlan,
    Evidence,
    EvidenceRef,
    EvidenceStore,
    HumanDecision,
    HumanDecisions,
    HumanReview,
    PaperSpec,
    ReproductionPlan,
    RunManifest,
    SpecValidationResult,
    UserConstraints,
)
from paper2repo_agent.utils import load_jsonl, read_json, sha256_text, utc_now, write_json


PROJECT_ROOT = Path(__file__).resolve().parents[3]
STATIC_DIR = Path(__file__).resolve().parent / "static"


class PlanRequest(BaseModel):
    paper_path: str
    run_name: str
    source_type: Literal["auto", "pdf", "markdown", "text"] = "auto"
    fake_llm: bool = False
    initial_request: str | None = None
    constraints: UserConstraints = Field(default_factory=UserConstraints)


class ApproveRequest(BaseModel):
    approved_by: str | None = None
    decisions: list[HumanDecision] = Field(default_factory=list)
    user_notes: str | None = None
    fake_llm: bool = False


@dataclass
class JobRecord:
    id: str
    kind: str
    status: Literal["queued", "running", "succeeded", "failed"] = "queued"
    run_name: str | None = None
    message: str = ""
    error: str | None = None
    logs: list[str] = field(default_factory=list)
    summaries: list[dict[str, str]] = field(default_factory=list)
    outcome_status: str | None = None
    outcome_label: str | None = None


class JobStore:
    def __init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()
        self._counter = 0

    def create(self, kind: str, run_name: str | None) -> JobRecord:
        with self._lock:
            self._counter += 1
            job = JobRecord(id=f"job_{self._counter:04d}", kind=kind, run_name=run_name)
            self._jobs[job.id] = job
            return job

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            if job_id not in self._jobs:
                raise HTTPException(status_code=404, detail="任务不存在")
            return self._jobs[job_id]

    def list(self) -> list[JobRecord]:
        with self._lock:
            return list(reversed(self._jobs.values()))

    def update(self, job_id: str, **updates: Any) -> None:
        with self._lock:
            job = self._jobs[job_id]
            for key, value in updates.items():
                setattr(job, key, value)

    def log(self, job_id: str, message: str) -> None:
        with self._lock:
            job = self._jobs[job_id]
            job.logs.append(message)
            summary = _parse_job_summary(message)
            if summary is not None:
                job.summaries.append(summary)


def create_app(runs_dir: Path | None = None) -> FastAPI:
    app = FastAPI(title="Paper2Repo Agent Console", version="0.1.0")
    app.state.runs_dir = runs_dir or PROJECT_ROOT / "runs"
    app.state.jobs = JobStore()

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html")

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        runs_root = _runs_dir(app)
        return {
            "status": "ok",
            "runs_dir": str(runs_root),
            "run_count": len(_run_dirs(runs_root)),
        }

    @app.get("/api/runs")
    def list_runs() -> list[dict[str, Any]]:
        return [_run_summary(path) for path in _run_dirs(_runs_dir(app))]

    @app.get("/api/runs/{run_name}")
    def get_run(run_name: str) -> dict[str, Any]:
        run_dir = _safe_run_dir(app, run_name)
        paths = prepare_run_dirs(run_dir)
        manifest = _load_json_if_exists(paths.manifest)
        plan = _load_json_if_exists(paths.latest_plan) or _load_json_if_exists(paths.draft_plan)
        review = _load_json_if_exists(paths.latest_human_review_json) or _load_json_if_exists(
            paths.human_review_json
        )
        validation = _load_json_if_exists(paths.validation_result)
        execution_result = _load_json_if_exists(paths.execution_result)
        final_report = _load_json_if_exists(paths.final_report_json)
        return {
            "run_name": run_name,
            "run_dir": str(run_dir),
            "manifest": manifest,
            "plan": plan,
            "review": review,
            "validation": validation,
            "execution_result": execution_result,
            "final_report": final_report,
            "artifacts": _artifact_index(paths),
            "display_context": _display_context(paths, plan, review, validation),
        }

    @app.get("/api/runs/{run_name}/artifact")
    def get_artifact(run_name: str, path: str = Query(...)) -> Any:
        run_dir = _safe_run_dir(app, run_name)
        target = _safe_artifact_path(run_dir, path)
        if not target.exists() or not target.is_file():
            raise HTTPException(status_code=404, detail="产物不存在")
        if target.suffix.lower() in {".json"}:
            return read_json(target)
        return PlainTextResponse(target.read_text(encoding="utf-8", errors="replace"))

    @app.get("/api/runs/{run_name}/evidence")
    def query_evidence(
        run_name: str,
        field_path: str | None = None,
        evidence_id: str | None = None,
        q: str | None = None,
        limit: int = Query(50, ge=1, le=200),
    ) -> list[dict[str, Any]]:
        run_dir = _safe_run_dir(app, run_name)
        paths = prepare_run_dirs(run_dir)
        manifest = _load_json_if_exists(paths.manifest) or {}
        mysql_rows = _query_evidence_mysql_from_manifest(
            manifest,
            field_path=field_path,
            evidence_id=evidence_id,
            q=q,
            limit=limit,
        )
        if mysql_rows is not None:
            return mysql_rows
        if paths.evidence_sqlite.exists():
            return _query_evidence_sqlite(paths.evidence_sqlite, field_path, evidence_id, q, limit)
        return _query_evidence_jsonl(paths, field_path, evidence_id, q, limit)

    @app.post("/api/plan")
    def start_plan(request: PlanRequest) -> dict[str, Any]:
        paper_path = Path(request.paper_path).expanduser()
        if not paper_path.exists():
            raise HTTPException(status_code=400, detail="论文路径不存在")
        run_name = _safe_run_name(request.run_name)
        out_dir = _runs_dir(app) / run_name
        job = app.state.jobs.create("plan", run_name)
        app.state.jobs.log(job.id, "规划任务已排队，正在准备论文输入。")
        constraints = request.constraints.model_copy(
            update={
                "initial_request": (request.initial_request or "").strip()
                or request.constraints.initial_request
            }
        )

        def work() -> None:
            _run_job(
                app,
                job.id,
                lambda: run_plan(
                    paper_path=paper_path,
                    output_dir=out_dir,
                    source_type=request.source_type,
                    fake_llm=request.fake_llm,
                    verbose=True,
                    user_constraints=constraints,
                    progress_callback=lambda message: app.state.jobs.log(job.id, message),
                ),
            )

        threading.Thread(target=work, daemon=True).start()
        return _job_payload(job)

    @app.post("/api/runs/{run_name}/approve")
    def approve_run(run_name: str, request: ApproveRequest) -> dict[str, Any]:
        run_dir = _safe_run_dir(app, run_name)
        paths = prepare_run_dirs(run_dir)
        manifest = RunManifest.model_validate(read_json(paths.manifest))
        plan = ReproductionPlan.model_validate(
            read_json(paths.latest_plan if paths.latest_plan.exists() else paths.draft_plan)
        )
        review = HumanReview.model_validate(
            read_json(
                paths.latest_human_review_json
                if paths.latest_human_review_json.exists()
                else paths.human_review_json
            )
        )
        decisions = _materialize_review_decisions(review, request.decisions)
        human_decisions = HumanDecisions(
            review_id=review.id,
            decisions=decisions,
            global_action="approve",
            user_notes=request.user_notes,
        )
        approval_error = _approval_validation_error(review, human_decisions)
        if approval_error:
            raise HTTPException(status_code=400, detail=approval_error)
        plan = apply_human_decisions_to_plan(plan, human_decisions)
        approved = ApprovedPlan(
            plan=plan,
            human_decisions=human_decisions,
            frozen_at=utc_now(),
            approved_by=request.approved_by,
            plan_hash=sha256_text(plan.model_dump_json()),
            execution_allowed=True,
        )
        write_json(
            paths.latest_plan if paths.latest_plan.exists() else paths.draft_plan,
            plan.model_dump(mode="json"),
        )
        write_json(paths.human_decisions, human_decisions.model_dump(mode="json"))
        write_json(paths.approved_plan, approved.model_dump(mode="json"))
        write_manifest(
            paths=paths,
            run_id=manifest.run_id,
            status="approved",
            extra_files=[paths.human_decisions, paths.approved_plan],
            approved_plan=approved,
            approval_status="approved",
            approval_iteration_count=manifest.approval_iteration_count,
        )
        return {"status": "approved", "approved_plan": paths.approved_plan.as_posix()}

    @app.post("/api/runs/{run_name}/revise")
    def revise_run(run_name: str, request: ApproveRequest) -> dict[str, Any]:
        run_dir = _safe_run_dir(app, run_name)
        paths = prepare_run_dirs(run_dir)
        manifest = RunManifest.model_validate(read_json(paths.manifest))
        plan = ReproductionPlan.model_validate(
            read_json(paths.latest_plan if paths.latest_plan.exists() else paths.draft_plan)
        )
        review = HumanReview.model_validate(
            read_json(
                paths.latest_human_review_json
                if paths.latest_human_review_json.exists()
                else paths.human_review_json
            )
        )
        spec = PaperSpec.model_validate(read_json(paths.paper_spec))
        validation = SpecValidationResult.model_validate(read_json(paths.validation_result))
        constraints = UserConstraints.model_validate(read_json(paths.user_constraints))
        evidence_store = _load_evidence_store(paths, manifest)
        human_decisions = HumanDecisions(
            review_id=review.id,
            decisions=_materialize_review_decisions(review, request.decisions),
            global_action="revise",
            user_notes=request.user_notes,
        )
        revised_plan = revise_plan_with_llm(
            current_plan=plan,
            review=review,
            decisions=human_decisions,
            spec=spec,
            validation=validation,
            constraints=constraints,
            settings=Settings.load(),
            evidence_store=evidence_store,
            fake_llm=request.fake_llm,
        )
        revised_review = build_human_review(revised_plan, validation)
        semantic_refs = collect_plan_semantic_evidence_refs(revised_plan, evidence_store)
        revised_review.evidence_refs = sorted(
            {*revised_review.evidence_refs, *[ref.evidence_id for ref in semantic_refs]}
        )
        evidence_store.refs = list(
            {
                ref.id: ref
                for ref in [
                    *evidence_store.refs,
                    *collect_evidence_refs(
                        revised_plan,
                        artifact_type="plan",
                        artifact_id=revised_plan.id,
                        created_by="validator",
                    ),
                    *semantic_refs,
                    *collect_evidence_refs(
                        revised_review,
                        artifact_type="human_review",
                        artifact_id=revised_review.id,
                        created_by="validator",
                    ),
                ]
            }.values()
        )
        revised_markdown = render_human_review_markdown(revised_review, revised_plan)
        iteration = next_approval_iteration(paths)
        revision_record = make_revision_record(
            paths=paths,
            iteration=iteration,
            output_plan=revised_plan,
            status="revised",
            model_name="fake-llm" if request.fake_llm else Settings.load().model_name,
            input_plan=plan,
            decision_ref=paths.iteration_human_decisions(iteration),
            notes=request.user_notes,
        )
        written = write_approval_iteration(
            paths=paths,
            iteration=iteration,
            plan=revised_plan,
            human_review=revised_review,
            human_review_markdown=revised_markdown,
            revision_record=revision_record,
            human_decisions=human_decisions,
        )
        write_evidence_files(evidence_store, paths.planning_dir)
        write_evidence_databases(
            evidence_store,
            paths.evidence_sqlite,
            settings=Settings.load(),
            refs_path=paths.evidence_database_refs,
        )
        write_manifest(
            paths=paths,
            run_id=manifest.run_id,
            status="planned",
            extra_files=[*written, *list_approval_artifacts(paths)],
            approval_status="awaiting_review",
            approval_iteration_count=count_approval_iterations(paths),
        )
        return {
            "status": "revised",
            "iteration": iteration,
            "latest_plan": paths.latest_plan.as_posix(),
        }

    @app.post("/api/runs/{run_name}/execute")
    def start_execute(run_name: str) -> dict[str, Any]:
        run_dir = _safe_run_dir(app, run_name)
        if not (run_dir / "approval" / "approved_plan.json").exists():
            raise HTTPException(status_code=400, detail="执行前需要先确认计划")
        job = app.state.jobs.create("execute", run_name)
        app.state.jobs.log(job.id, "执行任务已排队，正在加载已确认计划。")

        def work() -> None:
            _run_job(
                app,
                job.id,
                lambda: run_execute(
                    run_dir=run_dir,
                    verbose=True,
                    progress_callback=lambda message: app.state.jobs.log(job.id, message),
                ),
            )

        threading.Thread(target=work, daemon=True).start()
        return _job_payload(job)

    @app.get("/api/jobs")
    def list_jobs() -> list[dict[str, Any]]:
        return [_job_payload(job) for job in app.state.jobs.list()]

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, Any]:
        return _job_payload(app.state.jobs.get(job_id))

    return app


app = create_app()


def _approval_validation_error(review: HumanReview, decisions: HumanDecisions) -> str | None:
    if decisions.global_action != "approve":
        return None
    decision_map = {decision.item_id: decision for decision in decisions.decisions}
    for item in review.confirmation_items:
        decision = decision_map.get(item.field_path)
        if decision is None:
            return f"确认项缺少用户决策：{item.field_path}"
        if item.blocks_execution_if_unanswered and decision.action in {"defer", "reject"}:
            return f"阻塞型确认项需要接受或改写：{item.field_path}"
        if decision.action == "override" and (decision.value is None or decision.value == ""):
            return f"改写确认项需要填写值：{item.field_path}"
    return None


def _materialize_review_decisions(
    review: HumanReview,
    supplied: list[HumanDecision] | None,
) -> list[HumanDecision]:
    supplied = supplied or []
    supplied_map = {decision.item_id: decision for decision in supplied if decision.item_id}
    ordered: list[HumanDecision] = []
    seen: set[str] = set()
    for item in review.confirmation_items:
        key = item.field_path
        decision = supplied_map.get(key)
        if decision is None:
            decision = HumanDecision(
                item_id=key,
                action="accept",
                value=item.suggested_value,
                note=None,
            )
        ordered.append(decision)
        seen.add(key)
    for decision in supplied:
        if decision.item_id not in seen:
            ordered.append(decision)
    return ordered


def _runs_dir(app: FastAPI) -> Path:
    root = Path(app.state.runs_dir)
    root.mkdir(parents=True, exist_ok=True)
    return root


def _run_dirs(runs_root: Path) -> list[Path]:
    if not runs_root.exists():
        return []
    return sorted(
        [
            path
            for path in runs_root.iterdir()
            if path.is_dir() and (path / "run_manifest.json").exists()
        ],
        key=lambda path: (path / "run_manifest.json").stat().st_mtime,
        reverse=True,
    )


def _safe_run_name(value: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in value.strip())
    if not cleaned:
        raise HTTPException(status_code=400, detail="运行名称不能为空")
    return cleaned


def _safe_run_dir(app: FastAPI, run_name: str) -> Path:
    root = _runs_dir(app).resolve()
    run_dir = (root / _safe_run_name(run_name)).resolve()
    if root not in run_dir.parents and run_dir != root:
        raise HTTPException(status_code=400, detail="运行名称不合法")
    if not run_dir.exists():
        raise HTTPException(status_code=404, detail="运行记录不存在")
    return run_dir


def _safe_artifact_path(run_dir: Path, rel_path: str) -> Path:
    root = run_dir.resolve()
    target = (root / rel_path).resolve()
    if root not in target.parents and target != root:
        raise HTTPException(status_code=400, detail="产物路径不合法")
    return target


def _load_json_if_exists(path: Path) -> Any | None:
    if path.exists() and path.is_file():
        return read_json(path)
    return None


FIELD_LABELS = {
    "selected_claims": "复现主张",
    "user_constraints.compute_target": "计算资源范围",
    "user_constraints.dataset_policy": "数据策略",
    "user_constraints.allow_network": "是否允许联网",
    "user_constraints.allow_large_downloads": "是否允许大文件下载",
    "user_constraints.max_runtime_minutes": "运行时间预算",
    "scope.metric_reproduction": "是否尝试指标复现",
    "scope.dataset_mode": "数据模式",
    "scope.device_target": "目标设备",
    "implementation_strategy.architecture_summary": "架构实现摘要",
    "data_strategy.datasets": "目标数据集",
    "verification_strategy.pass_criteria": "验证通过标准",
    "training.batch_size": "小规模验证 batch size",
}


def _display_context(
    paths,
    plan: dict[str, Any] | None,
    review: dict[str, Any] | None,
    validation: dict[str, Any] | None,
) -> dict[str, Any]:
    paper_spec = _load_json_if_exists(paths.paper_spec) or {}
    evidence_rows = load_jsonl(paths.evidence_store) if paths.evidence_store.exists() else []
    ref_rows = load_jsonl(paths.evidence_refs) if paths.evidence_refs.exists() else []

    evidence = {
        str(item.get("id")): {
            "id": item.get("id"),
            "section": item.get("section"),
            "page": item.get("page"),
            "source": item.get("source"),
            "text": _preview_text(item.get("text"), 360),
        }
        for item in evidence_rows
        if item.get("id")
    }

    claims: dict[str, Any] = {}
    for claim in paper_spec.get("claims_to_reproduce", []) or []:
        if not isinstance(claim, dict):
            continue
        claim_id = str(claim.get("id") or "")
        if not claim_id:
            continue
        evidence_ids = [str(item) for item in claim.get("evidence_ids", []) or []]
        claims[claim_id] = {
            "id": claim_id,
            "description": claim.get("description") or claim_id,
            "claim_type": claim.get("claim_type"),
            "feasibility": claim.get("feasibility"),
            "required_data": claim.get("required_data") or [],
            "required_compute": claim.get("required_compute"),
            "evidence_ids": evidence_ids,
            "evidence": [evidence[item] for item in evidence_ids if item in evidence][:3],
        }

    fields: dict[str, Any] = {}
    for field_path, label in FIELD_LABELS.items():
        fields[field_path] = {"field_path": field_path, "label": label, "evidence_ids": []}

    for item in (review or {}).get("confirmation_items", []) or []:
        if not isinstance(item, dict):
            continue
        field_path = str(item.get("field_path") or "")
        if not field_path:
            continue
        entry = fields.setdefault(
            field_path,
            {
                "field_path": field_path,
                "label": _humanize_field_path(field_path),
                "evidence_ids": [],
            },
        )
        entry["question"] = item.get("question")
        entry["reason"] = item.get("reason")
        entry["evidence_ids"] = _dedupe_strings(
            [*entry.get("evidence_ids", []), *(item.get("evidence_ids") or [])]
        )

    for issue_group in ("missing_details", "assumptions", "unsupported_features"):
        for item in (validation or {}).get(issue_group, []) or []:
            if not isinstance(item, dict):
                continue
            field_path = str(item.get("field_path") or item.get("feature") or "")
            if not field_path:
                continue
            entry = fields.setdefault(
                field_path,
                {
                    "field_path": field_path,
                    "label": _humanize_field_path(field_path),
                    "evidence_ids": [],
                },
            )
            entry["evidence_ids"] = _dedupe_strings(
                [*entry.get("evidence_ids", []), *(item.get("evidence_ids") or [])]
            )

    for ref in ref_rows:
        field_path = str(ref.get("field_path") or "")
        evidence_id = str(ref.get("evidence_id") or "")
        if not field_path or not evidence_id:
            continue
        entry = fields.setdefault(
            field_path,
            {
                "field_path": field_path,
                "label": _humanize_field_path(field_path),
                "evidence_ids": [],
            },
        )
        entry["evidence_ids"] = _dedupe_strings([*entry.get("evidence_ids", []), evidence_id])

    for entry in fields.values():
        entry["evidence"] = [
            evidence[item] for item in entry.get("evidence_ids", []) if item in evidence
        ][:3]

    selected_claims = (plan or {}).get("selected_claims") or []
    return {
        "claims": claims,
        "evidence": evidence,
        "fields": fields,
        "selected_claims": [
            claims.get(str(item), {"id": item, "description": str(item)})
            for item in selected_claims
        ],
    }


def _preview_text(value: Any, limit: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "..."


def _humanize_field_path(field_path: str) -> str:
    return FIELD_LABELS.get(field_path) or field_path.replace("_", " ").replace(".", " / ")


def _dedupe_strings(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _run_summary(run_dir: Path) -> dict[str, Any]:
    manifest = _load_json_if_exists(run_dir / "run_manifest.json") or {}
    return {
        "run_name": run_dir.name,
        "run_dir": str(run_dir),
        "status": manifest.get("status", "unknown"),
        "approval_status": manifest.get("approval_status", "not_started"),
        "updated_at": manifest.get("updated_at"),
        "paper_spec_ref": manifest.get("paper_spec_ref"),
        "latest_plan_ref": manifest.get("latest_plan_ref") or manifest.get("draft_plan_ref"),
        "approved_plan_ref": manifest.get("approved_plan_ref"),
        "execution_result_ref": manifest.get("execution_result_ref"),
        "final_report_ref": manifest.get("final_report_ref"),
    }


def _artifact_index(paths) -> dict[str, str | None]:
    candidates = {
        "manifest": paths.manifest,
        "parsed_paper": paths.parsed_paper,
        "evidence_store": paths.evidence_store,
        "evidence_refs": paths.evidence_refs,
        "paper_spec": paths.paper_spec,
        "validation_result": paths.validation_result,
        "draft_plan": paths.draft_plan,
        "human_review": paths.human_review_md,
        "latest_plan": paths.latest_plan,
        "latest_human_review": paths.latest_human_review_md,
        "approved_plan": paths.approved_plan,
        "execution_policy": paths.execution_policy,
        "file_generation_tasks": paths.file_generation_tasks,
        "generated_files": paths.generated_files,
        "provenance": paths.provenance,
        "command_specs": paths.command_specs,
        "tool_calls": paths.tool_calls,
        "command_results": paths.command_results,
        "verification_result": paths.verification_result,
        "execution_result": paths.execution_result,
        "final_report": paths.final_report_md,
    }
    return {
        key: path.relative_to(paths.run_dir).as_posix() if path.exists() else None
        for key, path in candidates.items()
    }


def _query_evidence_sqlite(
    sqlite_path: Path,
    field_path: str | None,
    evidence_id: str | None,
    q: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    where: list[str] = []
    params: list[Any] = []
    if evidence_id:
        where.append("e.id = ?")
        params.append(evidence_id)
    if field_path:
        where.append("r.field_path = ?")
        params.append(field_path)
    if q:
        where.append("e.text LIKE ?")
        params.append(f"%{q}%")
    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT e.id, e.run_id, e.paper_id, e.section, e.page, e.text,
               r.field_path, r.artifact_type, r.relation
        FROM evidence e
        LEFT JOIN evidence_ref r ON e.id = r.evidence_id
        {clause}
        ORDER BY e.id
        LIMIT ?
    """
    params.append(limit)
    with sqlite3.connect(sqlite_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def _query_evidence_mysql_from_manifest(
    manifest: dict[str, Any],
    *,
    field_path: str | None,
    evidence_id: str | None,
    q: str | None,
    limit: int,
) -> list[dict[str, Any]] | None:
    database_refs = manifest.get("database_refs") if isinstance(manifest, dict) else None
    if not isinstance(database_refs, dict) or not database_refs.get("evidence_mysql"):
        return None
    settings = Settings.load()
    if not settings.mysql_uri:
        return None
    try:
        from paper2repo_agent.storage.mysql import query_evidence_mysql

        return query_evidence_mysql(
            settings.mysql_uri,
            field_path=field_path,
            evidence_id=evidence_id,
            q=q,
            limit=limit,
        )
    except Exception:
        return None


def _query_evidence_jsonl(
    paths,
    field_path: str | None,
    evidence_id: str | None,
    q: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    evidence_rows = load_jsonl(paths.evidence_store)
    ref_rows = load_jsonl(paths.evidence_refs)
    refs_by_evidence: dict[str, list[dict[str, Any]]] = {}
    for ref in ref_rows:
        refs_by_evidence.setdefault(ref.get("evidence_id", ""), []).append(ref)
    rows: list[dict[str, Any]] = []
    for item in evidence_rows:
        if evidence_id and item.get("id") != evidence_id:
            continue
        if q and q.lower() not in str(item.get("text", "")).lower():
            continue
        refs = refs_by_evidence.get(item.get("id", ""), [{}])
        for ref in refs:
            if field_path and ref.get("field_path") != field_path:
                continue
            rows.append({**item, **{k: v for k, v in ref.items() if k not in item}})
            if len(rows) >= limit:
                return rows
    return rows


def _load_evidence_store(paths, manifest: RunManifest) -> EvidenceStore:
    evidence_rows = load_jsonl(paths.evidence_store) if paths.evidence_store.exists() else []
    ref_rows = load_jsonl(paths.evidence_refs) if paths.evidence_refs.exists() else []
    evidence_items = [Evidence.model_validate(row) for row in evidence_rows]
    refs = [EvidenceRef.model_validate(row) for row in ref_rows]
    paper_id = evidence_items[0].paper_id if evidence_items else "paper"
    return EvidenceStore(
        run_id=manifest.run_id,
        paper_id=paper_id,
        items=evidence_items,
        refs=refs,
    )


def _run_job(app: FastAPI, job_id: str, action) -> None:
    app.state.jobs.update(job_id, status="running", message="运行中")
    try:
        action()
    except Exception as exc:  # noqa: BLE001
        app.state.jobs.update(
            job_id,
            status="failed",
            message="失败",
            error=f"{exc.__class__.__name__}: {exc}",
        )
        app.state.jobs.log(job_id, traceback.format_exc(limit=8))
        return
    outcome_status, outcome_label = _resolve_job_outcome(app, app.state.jobs.get(job_id))
    app.state.jobs.log(job_id, "任务完成。")
    app.state.jobs.update(
        job_id,
        status="succeeded",
        message="已完成",
        outcome_status=outcome_status,
        outcome_label=outcome_label,
    )


def _job_payload(job: JobRecord) -> dict[str, Any]:
    return {
        "id": job.id,
        "kind": job.kind,
        "status": job.status,
        "run_name": job.run_name,
        "message": job.message,
        "error": job.error,
        "outcome_status": job.outcome_status,
        "outcome_label": job.outcome_label,
        "logs": job.logs[-20:],
        "summaries": job.summaries[-8:],
    }


def _parse_job_summary(message: str) -> dict[str, str] | None:
    prefix = "LLM summary"
    if not message.startswith(prefix):
        return None
    stage = "LLM"
    text = message[len(prefix) :].strip()
    if text.startswith("[") and "]:" in text:
        stage, text = text[1:].split("]:", 1)
        text = text.strip()
    elif text.startswith(":"):
        text = text[1:].strip()
    if not text:
        return None
    return {"stage": stage.strip() or "LLM", "text": text[:700]}


def _resolve_job_outcome(app: FastAPI, job: JobRecord) -> tuple[str | None, str | None]:
    if not job.run_name:
        return None, None
    try:
        run_dir = _safe_run_dir(app, job.run_name)
    except HTTPException:
        return None, None
    manifest = _load_json_if_exists(run_dir / "run_manifest.json") or {}
    execution_result = _load_json_if_exists(run_dir / "execution" / "execution_result.json") or {}
    final_report = _load_json_if_exists(run_dir / "final_report.json") or {}
    if job.kind == "execute":
        return (
            str(execution_result.get("final_status") or manifest.get("status") or "unknown"),
            str(final_report.get("status") or manifest.get("status") or "unknown"),
        )
    return (
        str(manifest.get("status") or "unknown"),
        str(manifest.get("approval_status") or manifest.get("status") or "unknown"),
    )
