from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from paper2repo_agent.config import Settings
from paper2repo_agent.schemas import (
    Evidence,
    EvidenceRef,
    EvidenceStore,
    ParsedPaper,
    ReproductionPlan,
)
from paper2repo_agent.utils import ensure_dir, stable_id, write_json, write_jsonl


@dataclass(frozen=True)
class EvidenceDatabaseWriteResult:
    database_refs: dict[str, str]
    warnings: list[str]


def build_evidence_store(parsed: ParsedPaper, run_id: str) -> EvidenceStore:
    items: list[Evidence] = []
    for index, section in enumerate(parsed.sections):
        evidence_id = stable_id("ev", run_id, parsed.paper_id, section.id, index)
        items.append(
            Evidence(
                id=evidence_id,
                run_id=run_id,
                paper_id=parsed.paper_id,
                source="paper",
                section=section.title,
                page=section.page_start,
                paragraph_id=None,
                chunk_id=section.id,
                text=section.text[:12000],
                confidence=1.0,
            )
        )
    if not items and parsed.raw_text:
        items.append(
            Evidence(
                id=stable_id("ev", run_id, parsed.paper_id, "raw"),
                run_id=run_id,
                paper_id=parsed.paper_id,
                source="paper",
                section="full_text",
                text=parsed.raw_text[:12000],
                confidence=1.0,
            )
        )
    return EvidenceStore(run_id=run_id, paper_id=parsed.paper_id, items=items)


def make_ref(
    *,
    evidence_id: str,
    artifact_type: str,
    artifact_id: str,
    field_path: str,
    relation: str = "supports",
    created_by: str = "llm",
) -> EvidenceRef:
    return EvidenceRef(
        id=stable_id("eref", evidence_id, artifact_type, artifact_id, field_path, relation),
        evidence_id=evidence_id,
        artifact_type=artifact_type,
        artifact_id=artifact_id,
        field_path=field_path,
        relation=relation,
        created_by=created_by,
    )


def collect_evidence_refs(
    artifact: BaseModel,
    *,
    artifact_type: str,
    artifact_id: str,
    created_by: str,
) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    payload = artifact.model_dump(mode="json")

    def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            evidence_ids = value.get("evidence_ids")
            if isinstance(evidence_ids, list):
                for evidence_id in evidence_ids:
                    if isinstance(evidence_id, str) and evidence_id:
                        refs.append(
                            make_ref(
                                evidence_id=evidence_id,
                                artifact_type=artifact_type,
                                artifact_id=artifact_id,
                                field_path=path or "$",
                                relation="supports",
                                created_by=created_by,
                            )
                        )
            for key, child in value.items():
                child_path = f"{path}.{key}" if path else key
                walk(child, child_path)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                walk(child, f"{path}[{index}]")

    walk(payload, "")
    deduped: dict[str, EvidenceRef] = {ref.id: ref for ref in refs}
    return list(deduped.values())


def collect_plan_semantic_evidence_refs(
    plan: ReproductionPlan,
    store: EvidenceStore,
) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    refs.extend(
        _refs_for_terms(
            store=store,
            artifact_id=plan.id,
            field_path="implementation_strategy.architecture_summary",
            terms=_terms_from_text(plan.implementation_strategy.architecture_summary),
        )
    )
    synthetic = plan.data_strategy.synthetic_data_spec
    if synthetic is not None:
        for index, tensor in enumerate(synthetic.input_shapes):
            refs.extend(
                _refs_for_terms(
                    store=store,
                    artifact_id=plan.id,
                    field_path=f"data_strategy.synthetic_data_spec.input_shapes[{index}]",
                    terms=[tensor.name, tensor.description, "modality"],
                )
            )
    deduped: dict[str, EvidenceRef] = {ref.id: ref for ref in refs}
    return list(deduped.values())


def _refs_for_terms(
    *,
    store: EvidenceStore,
    artifact_id: str,
    field_path: str,
    terms: list[str],
) -> list[EvidenceRef]:
    refs: list[EvidenceRef] = []
    for item in _matching_evidence_items(store, terms)[:3]:
        refs.append(
            make_ref(
                evidence_id=item.id,
                artifact_type="plan",
                artifact_id=artifact_id,
                field_path=field_path,
                relation="supports",
                created_by="validator",
            )
        )
    return refs


def _matching_evidence_items(store: EvidenceStore, terms: list[str]) -> list[Evidence]:
    normalized_terms = [term.lower() for term in terms if term and len(term.strip()) > 2]
    scored: list[tuple[int, Evidence]] = []
    for item in store.items:
        text = f"{item.section or ''}\n{item.text}".lower()
        score = sum(1 for term in normalized_terms if term in text)
        if score > 0:
            scored.append((score, item))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in scored]


def _terms_from_text(text: str) -> list[str]:
    terms = ["dcmh", "cross-modal", "cross modal", "hash", "cnn", "text modality"]
    for token in text.replace(":", " ").replace(",", " ").split():
        token = token.strip().lower()
        if len(token) > 4:
            terms.append(token)
    return list(dict.fromkeys(terms))


def write_evidence_files(store: EvidenceStore, directory: Path) -> tuple[Path, Path]:
    ensure_dir(directory)
    evidence_path = directory / "evidence_store.jsonl"
    refs_path = directory / "evidence_refs.jsonl"
    write_jsonl(evidence_path, [item.model_dump(mode="json") for item in store.items])
    write_jsonl(refs_path, [ref.model_dump(mode="json") for ref in store.refs])
    return evidence_path, refs_path


def write_evidence_sqlite(store: EvidenceStore, sqlite_path: Path) -> None:
    ensure_dir(sqlite_path.parent)
    connection = sqlite3.connect(sqlite_path)
    try:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence (
                id TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                paper_id TEXT NOT NULL,
                source TEXT NOT NULL,
                section TEXT,
                page INTEGER,
                paragraph_id TEXT,
                chunk_id TEXT,
                span_start INTEGER,
                span_end INTEGER,
                text TEXT NOT NULL,
                confidence REAL NOT NULL
            )
            """
        )
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS evidence_ref (
                id TEXT PRIMARY KEY,
                evidence_id TEXT NOT NULL,
                artifact_type TEXT NOT NULL,
                artifact_id TEXT NOT NULL,
                field_path TEXT NOT NULL,
                relation TEXT NOT NULL,
                created_by TEXT NOT NULL
            )
            """
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_run ON evidence(run_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_paper ON evidence(paper_id)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_section ON evidence(section)")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_evidence_page ON evidence(page)")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ref_evidence ON evidence_ref(evidence_id)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_ref_artifact "
            "ON evidence_ref(artifact_type, artifact_id)"
        )
        connection.execute("CREATE INDEX IF NOT EXISTS idx_ref_field ON evidence_ref(field_path)")

        connection.executemany(
            """
            INSERT OR REPLACE INTO evidence (
                id, run_id, paper_id, source, section, page, paragraph_id, chunk_id,
                span_start, span_end, text, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    item.id,
                    item.run_id,
                    item.paper_id,
                    item.source,
                    item.section,
                    item.page,
                    item.paragraph_id,
                    item.chunk_id,
                    item.span_start,
                    item.span_end,
                    item.text,
                    item.confidence,
                )
                for item in store.items
            ],
        )
        connection.executemany(
            """
            INSERT OR REPLACE INTO evidence_ref (
                id, evidence_id, artifact_type, artifact_id, field_path, relation, created_by
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    ref.id,
                    ref.evidence_id,
                    ref.artifact_type,
                    ref.artifact_id,
                    ref.field_path,
                    ref.relation,
                    ref.created_by,
                )
                for ref in store.refs
            ],
        )
        connection.commit()
    finally:
        connection.close()


def write_evidence_databases(
    store: EvidenceStore,
    sqlite_path: Path,
    *,
    settings: Settings | None = None,
    refs_path: Path | None = None,
) -> EvidenceDatabaseWriteResult:
    settings = settings or Settings.load()
    write_evidence_sqlite(store, sqlite_path)
    database_refs = {"evidence": sqlite_path.as_posix()}
    warnings: list[str] = []
    backend = (settings.evidence_backend or "sqlite").lower()
    mysql_uri = settings.mysql_uri
    if backend == "mysql" and mysql_uri:
        try:
            from paper2repo_agent.storage.mysql import redact_database_uri, write_evidence_mysql

            write_evidence_mysql(store, mysql_uri)
            database_refs["evidence_mysql"] = redact_database_uri(mysql_uri)
        except Exception as exc:
            message = f"MySQL evidence sync failed: {exc}"
            if settings.mysql_required:
                raise RuntimeError(message) from exc
            warnings.append(message)
    elif backend == "mysql" and not mysql_uri:
        message = "PAPER2REPO_EVIDENCE_BACKEND=mysql but PAPER2REPO_MYSQL_URI is not configured."
        if settings.mysql_required:
            raise RuntimeError(message)
        warnings.append(message)
    result = EvidenceDatabaseWriteResult(database_refs=database_refs, warnings=warnings)
    if refs_path is not None:
        ensure_dir(refs_path.parent)
        write_json(
            refs_path,
            {
                "database_refs": database_refs,
                "warnings": warnings,
            },
        )
    return result


def query_evidence_by_field(sqlite_path: Path, field_path: str) -> list[dict[str, object]]:
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            """
            SELECT e.*
            FROM evidence e
            JOIN evidence_ref r ON r.evidence_id = e.id
            WHERE r.field_path = ?
            """,
            (field_path,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()


def query_refs_by_evidence(sqlite_path: Path, evidence_id: str) -> list[dict[str, object]]:
    connection = sqlite3.connect(sqlite_path)
    connection.row_factory = sqlite3.Row
    try:
        rows = connection.execute(
            "SELECT * FROM evidence_ref WHERE evidence_id = ?",
            (evidence_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        connection.close()
