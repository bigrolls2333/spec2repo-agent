from paper2repo_agent.schemas import (
    ExtractedField,
    HumanReview,
    PaperSpec,
    PlanRevisionRecord,
    RunManifest,
)


def test_paper_spec_roundtrip_has_no_canonical_validation_fields():
    spec = PaperSpec(id="spec_1")
    dumped = spec.model_dump_json()
    loaded = PaperSpec.model_validate_json(dumped)

    assert loaded.id == "spec_1"
    assert "missing_details" not in loaded.model_fields
    assert "assumptions" not in loaded.model_fields


def test_extracted_field_uses_evidence_ids():
    field = ExtractedField(value="AdamW", value_type="str", evidence_ids=["ev_1"])

    assert field.evidence_ids == ["ev_1"]
    assert "evidence" not in field.model_fields


def test_review_and_manifest_roundtrip():
    review = HumanReview(
        id="review_1",
        plan_id="plan_1",
        summary="summary",
        plan_overview=["fact 1"],
        scope_summary={"goal": "run_smoke_test"},
        decision_requirements=["user_constraints.compute_target"],
        evidence_refs=["ev_1"],
    )
    manifest = RunManifest(
        run_id="run_1",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        status="planned",
        approval_status="awaiting_review",
        approval_iteration_count=1,
        latest_plan_ref="approval/latest_plan.json",
        latest_review_ref="approval/latest_human_review.md",
    )
    revision = PlanRevisionRecord(
        id="revision_000",
        iteration=0,
        output_plan_ref="approval/iterations/000/plan.json",
        output_plan_hash="abc",
        created_at="2026-01-01T00:00:00+00:00",
    )

    assert HumanReview.model_validate_json(review.model_dump_json()).plan_overview == ["fact 1"]
    assert RunManifest.model_validate_json(manifest.model_dump_json()).latest_plan_ref is not None
    assert PlanRevisionRecord.model_validate_json(revision.model_dump_json()).iteration == 0
