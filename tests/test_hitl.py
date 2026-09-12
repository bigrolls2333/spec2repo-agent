from paper2repo_agent.hitl import (
    apply_human_decisions_to_plan,
    build_merged_confirmations,
)
from paper2repo_agent.planner import draft_reproduction_plan
from paper2repo_agent.schemas import (
    ConfirmationProposal,
    ConfirmationProposalSet,
    Evidence,
    EvidenceStore,
    HumanDecision,
    HumanDecisions,
    PaperSpec,
    UserConstraints,
)
from paper2repo_agent.validation import validate_spec


def test_llm_confirmation_proposals_are_backend_validated_and_merged():
    spec = PaperSpec(id="spec_1")
    constraints = UserConstraints()
    validation = validate_spec(spec, constraints)
    store = EvidenceStore(
        run_id="run_1",
        paper_id="paper_1",
        items=[
            Evidence(
                id="ev_real",
                run_id="run_1",
                paper_id="paper_1",
                source="paper",
                section="Method",
                text="The model uses image and text inputs.",
            )
        ],
    )
    proposals = ConfirmationProposalSet(
        proposals=[
            ConfirmationProposal(
                field_path="data_strategy.synthetic_data_spec.input_shapes",
                question="Confirm synthetic input shape.",
                suggested_value=[{"name": "image", "shape": [3, 224, 224]}],
                options=[
                    [{"name": "image", "shape": [3, 256, 256]}],
                    [{"name": "image", "shape": [3, 256, 256]}],
                ],
                reason="Input shape affects generated tests.",
                decision_effect="The generated smoke fixtures will use this shape.",
                severity="required",
                evidence_ids=["ev_real", "ev_fake"],
            ),
            ConfirmationProposal(
                field_path="random.unindexed.path",
                question="Invalid field.",
                reason="Should be rejected.",
                decision_effect="No effect.",
            ),
            ConfirmationProposal(
                field_path="user_constraints.compute_target",
                question="LLM should not override policy wording.",
                suggested_value="multi_gpu",
                reason="Duplicate policy field.",
                decision_effect="No effect.",
            ),
        ]
    )

    items = build_merged_confirmations(
        validation,
        constraints,
        proposals,
        run_id="run_1",
        evidence_store=store,
    )
    by_path = {item.field_path: item for item in items}

    assert "user_constraints.compute_target" in by_path
    assert "user_constraints.dataset_policy" in by_path
    assert "user_constraints.allow_network" in by_path
    assert "data_strategy.synthetic_data_spec.input_shapes" in by_path
    assert "random.unindexed.path" not in by_path
    assert by_path["data_strategy.synthetic_data_spec.input_shapes"].evidence_ids == ["ev_real"]
    assert by_path["data_strategy.synthetic_data_spec.input_shapes"].options == [
        [{"name": "image", "shape": [3, 224, 224]}],
        [{"name": "image", "shape": [3, 256, 256]}],
    ]
    assert by_path["data_strategy.synthetic_data_spec.input_shapes"].id.startswith("confirm_")
    assert by_path["user_constraints.compute_target"].question.startswith(
        "Confirm the compute target"
    )


def test_human_decisions_update_execution_relevant_plan_fields():
    spec = PaperSpec(id="spec_1")
    constraints = UserConstraints()
    validation = validate_spec(spec, constraints)
    plan = draft_reproduction_plan(spec, validation, constraints)
    decisions = HumanDecisions(
        review_id="review_1",
        global_action="approve",
        decisions=[
            HumanDecision(
                item_id="user_constraints.dataset_policy",
                action="override",
                value="download_allowed",
            ),
            HumanDecision(
                item_id="user_constraints.allow_network",
                action="override",
                value="true",
            ),
            HumanDecision(
                item_id="user_constraints.allow_large_downloads",
                action="override",
                value=True,
            ),
            HumanDecision(
                item_id="user_constraints.max_runtime_minutes",
                action="override",
                value="45",
            ),
            HumanDecision(
                item_id="user_constraints.package_manager",
                action="override",
                value="uv",
            ),
            HumanDecision(
                item_id="training.batch_size",
                action="override",
                value="4",
            ),
        ],
    )

    updated = apply_human_decisions_to_plan(plan, decisions)

    assert updated.user_constraints.dataset_policy == "download_allowed"
    assert updated.scope.dataset_mode == "download"
    assert updated.data_strategy.mode == "download"
    assert updated.user_constraints.allow_network is True
    assert updated.user_constraints.allow_large_downloads is True
    assert updated.user_constraints.max_runtime_minutes == 45
    assert updated.scope.max_training_minutes == 45
    assert updated.user_constraints.package_manager == "uv"
    assert updated.dependency_strategy.package_manager == "uv"
    assert updated.data_strategy.synthetic_data_spec is not None
    assert updated.data_strategy.synthetic_data_spec.batch_size == 4


def test_human_decisions_keep_plan_typed_after_nested_dict_override():
    spec = PaperSpec(id="spec_1")
    constraints = UserConstraints()
    validation = validate_spec(spec, constraints)
    plan = draft_reproduction_plan(spec, validation, constraints)
    decisions = HumanDecisions(
        review_id="review_1",
        global_action="approve",
        decisions=[
            HumanDecision(
                item_id="data_strategy.synthetic_data_spec",
                action="override",
                value={
                    "input_shapes": [],
                    "num_samples": 8,
                    "batch_size": 2,
                    "task_type": "classification",
                },
            ),
            HumanDecision(
                item_id="training.batch_size",
                action="override",
                value="6",
            ),
        ],
    )

    updated = apply_human_decisions_to_plan(plan, decisions)

    assert updated.data_strategy.synthetic_data_spec is not None
    assert updated.data_strategy.synthetic_data_spec.batch_size == 6
