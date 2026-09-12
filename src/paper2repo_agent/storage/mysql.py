from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit, urlunsplit

from paper2repo_agent.schemas import EvidenceStore


def redact_database_uri(database_uri: str) -> str:
    parts = urlsplit(database_uri)
    if not parts.netloc:
        return database_uri
    userinfo, at, host = parts.netloc.rpartition("@")
    if not at:
        return database_uri
    username, colon, _password = userinfo.partition(":")
    redacted_userinfo = f"{username}:***" if colon else username
    return urlunsplit(
        (parts.scheme, f"{redacted_userinfo}@{host}", parts.path, parts.query, parts.fragment)
    )


def write_evidence_mysql(store: EvidenceStore, database_uri: str) -> None:
    engine = _create_engine(database_uri)
    with engine.begin() as connection:
        _ensure_schema(connection)
        evidence_rows = [_evidence_params(item) for item in store.items]
        ref_rows = [_ref_params(ref) for ref in store.refs]
        if evidence_rows:
            connection.execute(_text(_EVIDENCE_UPSERT), evidence_rows)
        if ref_rows:
            connection.execute(_text(_EVIDENCE_REF_UPSERT), ref_rows)


def query_evidence_mysql(
    database_uri: str,
    *,
    field_path: str | None = None,
    evidence_id: str | None = None,
    q: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    engine = _create_engine(database_uri)
    where: list[str] = []
    params: dict[str, Any] = {"limit": limit}
    if evidence_id:
        where.append("e.id = :evidence_id")
        params["evidence_id"] = evidence_id
    if field_path:
        where.append("r.field_path = :field_path")
        params["field_path"] = field_path
    if q:
        where.append("e.text LIKE :q")
        params["q"] = f"%{q}%"
    clause = "WHERE " + " AND ".join(where) if where else ""
    sql = f"""
        SELECT e.id, e.run_id, e.paper_id, e.section, e.page, e.text,
               r.field_path, r.artifact_type, r.relation
        FROM evidence e
        LEFT JOIN evidence_ref r ON e.id = r.evidence_id
        {clause}
        ORDER BY e.id
        LIMIT :limit
    """
    with engine.connect() as connection:
        rows = connection.execute(_text(sql), params).fetchall()
    return [dict(row._mapping) for row in rows]


def query_evidence_by_field_mysql(
    database_uri: str,
    field_path: str,
    *,
    limit: int = 50,
) -> list[dict[str, Any]]:
    return query_evidence_mysql(database_uri, field_path=field_path, limit=limit)


def query_refs_by_evidence_mysql(database_uri: str, evidence_id: str) -> list[dict[str, Any]]:
    engine = _create_engine(database_uri)
    with engine.connect() as connection:
        rows = connection.execute(
            _text("SELECT * FROM evidence_ref WHERE evidence_id = :evidence_id"),
            {"evidence_id": evidence_id},
        ).fetchall()
    return [dict(row._mapping) for row in rows]


def _create_engine(database_uri: str):
    try:
        from sqlalchemy import create_engine
    except ImportError as exc:
        raise RuntimeError(
            "MySQL evidence backend requires optional dependencies. "
            'Install with: pip install -e ".[mysql]"'
        ) from exc
    return create_engine(database_uri, future=True, pool_pre_ping=True)


def _text(sql: str):
    try:
        from sqlalchemy import text
    except ImportError as exc:
        raise RuntimeError(
            "MySQL evidence backend requires optional dependencies. "
            'Install with: pip install -e ".[mysql]"'
        ) from exc
    return text(sql)


def _ensure_schema(connection) -> None:
    for statement in _SCHEMA:
        connection.execute(_text(statement))


def _evidence_params(item) -> dict[str, Any]:
    return {
        "id": item.id,
        "run_id": item.run_id,
        "paper_id": item.paper_id,
        "source": item.source,
        "section": item.section,
        "page": item.page,
        "paragraph_id": item.paragraph_id,
        "chunk_id": item.chunk_id,
        "span_start": item.span_start,
        "span_end": item.span_end,
        "text": item.text,
        "confidence": item.confidence,
    }


def _ref_params(ref) -> dict[str, Any]:
    return {
        "id": ref.id,
        "evidence_id": ref.evidence_id,
        "artifact_type": ref.artifact_type,
        "artifact_id": ref.artifact_id,
        "field_path": ref.field_path,
        "relation": ref.relation,
        "created_by": ref.created_by,
    }


_SCHEMA = [
    """
    CREATE TABLE IF NOT EXISTS evidence (
        id VARCHAR(128) PRIMARY KEY,
        run_id VARCHAR(128) NOT NULL,
        paper_id VARCHAR(128) NOT NULL,
        source VARCHAR(32) NOT NULL,
        section VARCHAR(512),
        page INT,
        paragraph_id VARCHAR(128),
        chunk_id VARCHAR(128),
        span_start INT,
        span_end INT,
        text MEDIUMTEXT NOT NULL,
        confidence DOUBLE NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
        INDEX idx_evidence_run (run_id),
        INDEX idx_evidence_paper (paper_id),
        INDEX idx_evidence_section (section),
        INDEX idx_evidence_page (page)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS evidence_ref (
        id VARCHAR(128) PRIMARY KEY,
        evidence_id VARCHAR(128) NOT NULL,
        artifact_type VARCHAR(64) NOT NULL,
        artifact_id VARCHAR(128) NOT NULL,
        field_path VARCHAR(512) NOT NULL,
        relation VARCHAR(64) NOT NULL,
        created_by VARCHAR(64) NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        INDEX idx_ref_evidence (evidence_id),
        INDEX idx_ref_artifact (artifact_type, artifact_id),
        INDEX idx_ref_field (field_path),
        CONSTRAINT fk_evidence_ref_evidence
            FOREIGN KEY (evidence_id) REFERENCES evidence(id)
            ON DELETE CASCADE
    )
    """,
]

_EVIDENCE_UPSERT = """
INSERT INTO evidence (
    id, run_id, paper_id, source, section, page, paragraph_id, chunk_id,
    span_start, span_end, text, confidence
) VALUES (
    :id, :run_id, :paper_id, :source, :section, :page, :paragraph_id, :chunk_id,
    :span_start, :span_end, :text, :confidence
)
ON DUPLICATE KEY UPDATE
    run_id = VALUES(run_id),
    paper_id = VALUES(paper_id),
    source = VALUES(source),
    section = VALUES(section),
    page = VALUES(page),
    paragraph_id = VALUES(paragraph_id),
    chunk_id = VALUES(chunk_id),
    span_start = VALUES(span_start),
    span_end = VALUES(span_end),
    text = VALUES(text),
    confidence = VALUES(confidence),
    updated_at = CURRENT_TIMESTAMP
"""

_EVIDENCE_REF_UPSERT = """
INSERT INTO evidence_ref (
    id, evidence_id, artifact_type, artifact_id, field_path, relation, created_by
) VALUES (
    :id, :evidence_id, :artifact_type, :artifact_id, :field_path, :relation, :created_by
)
ON DUPLICATE KEY UPDATE
    evidence_id = VALUES(evidence_id),
    artifact_type = VALUES(artifact_type),
    artifact_id = VALUES(artifact_id),
    field_path = VALUES(field_path),
    relation = VALUES(relation),
    created_by = VALUES(created_by)
"""
