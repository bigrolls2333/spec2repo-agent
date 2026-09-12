from __future__ import annotations

from types import SimpleNamespace

import pytest

from paper2repo_agent.evidence import (
    build_evidence_store,
    make_ref,
    write_evidence_databases,
)
from paper2repo_agent.parser import parse_markdown_text
from paper2repo_agent.storage import mysql
from paper2repo_agent.web.app import _query_evidence_mysql_from_manifest


def _store():
    parsed = parse_markdown_text(
        "# Method\nThe model uses AdamW.",
        paper_id="paper_1",
        title="Example",
    )
    store = build_evidence_store(parsed, "run_1")
    store.refs.append(
        make_ref(
            evidence_id=store.items[0].id,
            artifact_type="paper_spec",
            artifact_id="spec_1",
            field_path="training.optimizer",
        )
    )
    return store


def test_write_evidence_databases_defaults_to_sqlite(tmp_path):
    store = _store()
    sqlite_path = tmp_path / "evidence.sqlite"
    refs_path = tmp_path / "evidence_database_refs.json"
    settings = SimpleNamespace(evidence_backend="sqlite", mysql_uri=None, mysql_required=False)

    result = write_evidence_databases(store, sqlite_path, settings=settings, refs_path=refs_path)

    assert sqlite_path.exists()
    assert refs_path.exists()
    assert result.database_refs == {"evidence": sqlite_path.as_posix()}
    assert result.warnings == []


def test_write_evidence_databases_mysql_optional_failure_falls_back(tmp_path, monkeypatch):
    store = _store()
    sqlite_path = tmp_path / "evidence.sqlite"
    settings = SimpleNamespace(
        evidence_backend="mysql",
        mysql_uri="mysql+pymysql://paper2repo:example-password@127.0.0.1:3306/paper2repo",
        mysql_required=False,
    )

    def fail_write(_store, _uri):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(mysql, "write_evidence_mysql", fail_write)

    result = write_evidence_databases(store, sqlite_path, settings=settings)

    assert sqlite_path.exists()
    assert result.database_refs == {"evidence": sqlite_path.as_posix()}
    assert "connection refused" in result.warnings[0]


def test_write_evidence_databases_mysql_required_raises(tmp_path, monkeypatch):
    store = _store()
    settings = SimpleNamespace(
        evidence_backend="mysql",
        mysql_uri="mysql+pymysql://paper2repo:example-password@127.0.0.1:3306/paper2repo",
        mysql_required=True,
    )
    monkeypatch.setattr(
        mysql,
        "write_evidence_mysql",
        lambda _store, _uri: (_ for _ in ()).throw(RuntimeError("down")),
    )

    with pytest.raises(RuntimeError, match="MySQL evidence sync failed"):
        write_evidence_databases(store, tmp_path / "evidence.sqlite", settings=settings)


def test_write_evidence_mysql_creates_schema_and_uses_upsert(monkeypatch):
    store = _store()
    executed: list[tuple[str, object]] = []

    class FakeConnection:
        def execute(self, statement, params=None):
            executed.append((statement, params))

    class FakeBegin:
        def __enter__(self):
            return FakeConnection()

        def __exit__(self, *_args):
            return False

    class FakeEngine:
        def begin(self):
            return FakeBegin()

    monkeypatch.setattr(mysql, "_create_engine", lambda _uri: FakeEngine())
    monkeypatch.setattr(mysql, "_text", lambda sql: sql)

    mysql.write_evidence_mysql(
        store, "mysql+pymysql://paper2repo:example-password@127.0.0.1:3306/paper2repo"
    )

    sql_text = "\n".join(statement for statement, _params in executed)
    assert "CREATE TABLE IF NOT EXISTS evidence" in sql_text
    assert "CREATE TABLE IF NOT EXISTS evidence_ref" in sql_text
    assert "ON DUPLICATE KEY UPDATE" in sql_text
    assert any(
        isinstance(params, list) and params and params[0]["id"] == store.items[0].id
        for _sql, params in executed
    )


def test_web_evidence_query_uses_mysql_ref_when_configured(monkeypatch):
    rows = [{"id": "ev_1", "field_path": "training.optimizer"}]
    monkeypatch.setenv(
        "PAPER2REPO_MYSQL_URI",
        "mysql+pymysql://paper2repo:example-password@127.0.0.1:3306/paper2repo",
    )
    monkeypatch.setattr(mysql, "query_evidence_mysql", lambda *_args, **_kwargs: rows)

    result = _query_evidence_mysql_from_manifest(
        {
            "database_refs": {
                "evidence_mysql": "mysql+pymysql://paper2repo:***@127.0.0.1:3306/paper2repo"
            }
        },
        field_path="training.optimizer",
        evidence_id=None,
        q=None,
        limit=50,
    )

    assert result == rows
