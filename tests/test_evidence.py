from paper2repo_agent.evidence import (
    build_evidence_store,
    make_ref,
    query_evidence_by_field,
    query_refs_by_evidence,
    write_evidence_files,
    write_evidence_sqlite,
)
from paper2repo_agent.parser import parse_markdown_text


def test_evidence_jsonl_and_sqlite_roundtrip(tmp_path):
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

    evidence_path, refs_path = write_evidence_files(store, tmp_path)
    sqlite_path = tmp_path / "evidence.sqlite"
    write_evidence_sqlite(store, sqlite_path)

    assert evidence_path.exists()
    assert refs_path.exists()
    assert sqlite_path.exists()
    assert query_evidence_by_field(sqlite_path, "training.optimizer")[0]["id"] == store.items[0].id
    refs = query_refs_by_evidence(sqlite_path, store.items[0].id)
    assert refs[0]["field_path"] == "training.optimizer"
