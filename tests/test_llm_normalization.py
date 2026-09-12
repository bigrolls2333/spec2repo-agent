from paper2repo_agent.llm import normalize_paper_spec_payload, normalize_reproduction_plan_payload
from paper2repo_agent.planner import draft_reproduction_plan
from paper2repo_agent.schemas import PaperSection, PaperSpec, ParsedPaper, UserConstraints
from paper2repo_agent.validation import validate_spec


def test_extract_spec_with_llm_merges_three_partial_calls(monkeypatch):
    from paper2repo_agent.config import Settings
    from paper2repo_agent import llm as llm_mod

    replies = [
        {"metadata": {"title": "Example"}, "task": {}, "model": {}},
        {"datasets": [], "training": {}, "evaluation": {}},
        {
            "dependencies": {},
            "implementation_notes": [],
            "claims_to_reproduce": [],
            "extraction_gaps": [],
            "source_policy_ref": None,
        },
    ]

    class FakeModel:
        def __init__(self):
            self.calls = 0

        def invoke(self, _messages):
            payload = replies[self.calls]
            self.calls += 1
            return type("Response", (), {"content": __import__("json").dumps(payload)})()

    model = FakeModel()
    monkeypatch.setattr(llm_mod, "build_model", lambda _settings: model)
    parsed = ParsedPaper(
        paper_id="paper-1",
        title="Example",
        raw_text="Example paper",
        sections=[PaperSection(id="s1", title="Abstract", text="Example paper")],
    )
    settings = Settings(
        openai_api_key="test",
        openai_base_url="https://example.invalid/v1",
        model_name="test-model",
        llm_temperature=0.0,
        mineru_base_url=None,
        mineru_api_token=None,
        mineru_model_source=None,
        modelscope_offline=None,
        modelscope_cache=None,
    )

    spec = llm_mod.extract_spec_with_llm(parsed, ["ev1"], settings)

    assert model.calls == 3
    assert spec.metadata.title.value == "Example"


def test_paper_spec_part_context_routes_and_bounds_sections():
    from paper2repo_agent.llm import _paper_spec_part_context

    parsed = ParsedPaper(
        paper_id="paper-1",
        title="Example",
        raw_text="raw",
        sections=[
            PaperSection(id="s1", title="Abstract", text="a" * 3000),
            PaperSection(id="s2", title="Introduction", text="intro"),
            PaperSection(id="s3", title="Training Details", text="train"),
            PaperSection(id="s4", title="Unrelated", text="skip-me"),
        ],
    )

    evidence_ids, context = _paper_spec_part_context(
        parsed,
        ["ev1", "ev2", "ev3", "ev4"],
        ("training",),
        max_sections=3,
        max_chars=2600,
    )

    assert evidence_ids == ["ev1", "ev2", "ev3"]
    assert "section=Abstract" in context
    assert "section=Introduction" in context
    assert "section=Training Details" in context
    assert "skip-me" not in context
    assert len(context) < 2800


def test_normalize_paper_spec_repairs_extracted_field_like_lists():
    payload = {
        "id": "spec_1",
        "implementation_notes": [
            {
                "value": "Teacher: 2-layer encoder",
                "source_type": "paper",
                "evidence_ids": ["ev_1"],
                "confidence": 0.9,
                "notes": None,
            }
        ],
        "claims_to_reproduce": [
            {
                "value": "State-of-the-art result",
                "source_type": "paper",
                "evidence_ids": ["ev_2"],
                "confidence": 0.9,
                "notes": None,
            }
        ],
        "extraction_gaps": [
            {
                "value": "CLIP model version missing",
                "source_type": "paper",
                "evidence_ids": ["ev_3"],
                "confidence": 0.7,
                "notes": None,
            }
        ],
    }

    normalized = normalize_paper_spec_payload(payload)
    spec = PaperSpec.model_validate(normalized)

    assert spec.implementation_notes[0].note == "Teacher: 2-layer encoder"
    assert spec.implementation_notes[0].target == "implementation"
    assert spec.claims_to_reproduce[0].description == "State-of-the-art result"
    assert spec.extraction_gaps[0].description == "CLIP model version missing"


def test_normalize_paper_spec_backfills_cross_modal_hashing_core_fields():
    payload = {
        "id": "spec_1",
        "metadata": {
            "title": {
                "value": "Deep Cross-Modal Hashing",
                "value_type": "str",
                "source_type": "paper",
                "evidence_ids": ["ev_title"],
                "confidence": 0.9,
            }
        },
        "implementation_notes": [
            {
                "id": "note_1",
                "target": "model",
                "note": "DCMH uses deep neural networks for image-text cross-modal hashing.",
                "evidence_ids": ["ev_model"],
            }
        ],
        "claims_to_reproduce": [],
        "extraction_gaps": [],
    }

    normalized = normalize_paper_spec_payload(payload)
    spec = PaperSpec.model_validate(normalized)

    assert spec.task.task_type.value == "cross_modal_retrieval"
    assert spec.task.input_modality.value == "image,text"
    assert spec.model.architecture_name.value == "DCMH"
    assert spec.model.architecture_name.evidence_ids == ["ev_model"]


def test_normalize_paper_spec_filters_non_backend_evidence_ids():
    payload = {
        "id": "spec_1",
        "metadata": {
            "title": {
                "value": "Backend-owned evidence IDs",
                "value_type": "str",
                "source_type": "paper",
                "evidence_ids": ["ev_valid", "ev_id", "evidence_1", "ev_valid"],
                "confidence": 0.9,
            }
        },
        "implementation_notes": [
            {
                "id": "llm_note_id",
                "target": "model",
                "note": "Use the paper-backed encoder.",
                "evidence_ids": ["ev_valid", "made_up"],
            }
        ],
        "claims_to_reproduce": [
            {
                "id": "llm_claim_id",
                "description": "Reproduce the architecture.",
                "claim_type": "result",
                "feasibility": "certain",
                "evidence_ids": ["ev_valid", "fake_ev"],
            }
        ],
        "extraction_gaps": [],
    }

    normalized = normalize_paper_spec_payload(payload, allowed_evidence_ids=["ev_valid"])
    spec = PaperSpec.model_validate(normalized)

    assert spec.metadata.title.evidence_ids == ["ev_valid"]
    assert spec.implementation_notes[0].evidence_ids == ["ev_valid"]
    assert spec.claims_to_reproduce[0].evidence_ids == ["ev_valid"]
    assert spec.claims_to_reproduce[0].claim_type == "architecture"
    assert spec.claims_to_reproduce[0].feasibility == "medium"


def test_normalize_paper_spec_overrides_llm_primary_like_ids():
    payload = {
        "id": "spec_1",
        "implementation_notes": [
            {
                "id": "llm_note_id",
                "target": "model",
                "note": "Stable note.",
                "evidence_ids": ["ev_1"],
            }
        ],
        "claims_to_reproduce": [
            {
                "id": "llm_claim_id",
                "description": "Stable claim.",
                "evidence_ids": ["ev_1"],
            }
        ],
        "extraction_gaps": [
            {
                "id": "llm_gap_id",
                "field_path": "training.epochs",
                "description": "Epochs missing.",
                "evidence_ids": ["ev_1"],
            }
        ],
    }

    normalized = normalize_paper_spec_payload(payload, allowed_evidence_ids=["ev_1"])
    spec = PaperSpec.model_validate(normalized)

    assert spec.implementation_notes[0].id != "llm_note_id"
    assert spec.implementation_notes[0].id.startswith("impl_note_")
    assert spec.claims_to_reproduce[0].id != "llm_claim_id"
    assert spec.claims_to_reproduce[0].id.startswith("claim_")
    assert spec.extraction_gaps[0].id != "llm_gap_id"
    assert spec.extraction_gaps[0].id.startswith("gap_")


def test_normalize_paper_spec_repairs_extracted_field_like_model_components():
    payload = {
        "id": "spec_1",
        "model": {
            "components": [
                {
                    "name": {
                        "value": "image_feature_extractor",
                        "source_type": "paper",
                        "evidence_ids": ["ev_1"],
                        "confidence": 0.9,
                    },
                    "component_type": {
                        "value": "cnn_encoder",
                        "source_type": "inferred",
                        "evidence_ids": ["ev_1"],
                        "confidence": 0.8,
                    },
                    "description": {
                        "value": "CNN image encoder with stacked conv blocks.",
                        "source_type": "paper",
                        "evidence_ids": ["ev_1"],
                        "confidence": 0.9,
                    },
                    "inputs": {
                        "value": "raw image pixels",
                        "source_type": "paper",
                        "evidence_ids": ["ev_1"],
                        "confidence": 0.9,
                    },
                    "outputs": {
                        "value": "image feature vector",
                        "source_type": "paper",
                        "evidence_ids": ["ev_1"],
                        "confidence": 0.9,
                    },
                    "hyperparameters": {
                        "value": "Exact filter sizes are not fully specified in the provided evidence.",
                        "source_type": "paper",
                        "evidence_ids": ["ev_1"],
                        "confidence": 0.7,
                    },
                }
            ]
        },
    }

    normalized = normalize_paper_spec_payload(payload, allowed_evidence_ids=["ev_1"])
    spec = PaperSpec.model_validate(normalized)

    component = spec.model.components[0]
    assert component.name == "image_feature_extractor"
    assert component.component_type == "cnn_encoder"
    assert component.description == "CNN image encoder with stacked conv blocks."
    assert component.inputs == ["raw image pixels"]
    assert component.outputs == ["image feature vector"]
    assert component.hyperparameters["details"].value.startswith("Exact filter sizes")
    assert component.hyperparameters["details"].evidence_ids == ["ev_1"]


def test_normalize_paper_spec_repairs_losses_and_equations():
    payload = {
        "id": "spec_1",
        "model": {
            "losses": [
                {
                    "name": "ranking_loss",
                    "loss_type": "contrastive",
                    "formula": "L = ...",
                    "weight": 1.0,
                    "evidence_ids": ["ev_loss"],
                },
                {
                    "name": "quantization_loss",
                    "weight": "eta",
                    "evidence_ids": ["ev_loss"],
                },
            ],
            "equations": [
                {
                    "latex": {"value": "\\mathcal{L}=... ", "evidence_ids": ["ev_eq"]},
                    "meaning": {
                        "value": "Defines the total loss minimized during training.",
                        "evidence_ids": ["ev_eq"],
                    },
                },
                {
                    "latex": {"value": "\\mathbf{b}=... ", "evidence_ids": ["ev_eq2"]},
                    "meaning": "Used in backpropagation over image CNN parameters.",
                },
            ],
        },
    }

    normalized = normalize_paper_spec_payload(
        payload,
        allowed_evidence_ids=["ev_loss", "ev_eq", "ev_eq2"],
    )
    spec = PaperSpec.model_validate(normalized)

    assert spec.model.losses[0].weight.value == 1.0
    assert spec.model.losses[0].weight.value_type == "float"
    assert spec.model.losses[0].evidence_ids == ["ev_loss"]
    assert spec.model.losses[1].weight.value == "eta"
    assert spec.model.losses[1].weight.value_type == "str"
    assert spec.model.equations[0].latex == "\\mathcal{L}=..."
    assert spec.model.equations[0].meaning.startswith("Defines the total loss")
    assert spec.model.equations[0].implementation_relevance == "required"
    assert spec.model.equations[1].implementation_relevance == "required"


def test_normalize_paper_spec_repairs_training_and_evaluation_lists():
    payload = {
        "id": "spec_1",
        "training": {
            "augmentation": {
                "value": None,
                "notes": "Random crop and horizontal flip",
                "evidence_ids": ["ev_aug"],
            }
        },
        "evaluation": {
            "metrics": {
                "value": None,
                "notes": "mAP",
                "evidence_ids": ["ev_metric"],
            },
            "baselines": {
                "value": None,
                "notes": "DCMH baseline",
                "evidence_ids": ["ev_base"],
            },
            "target_results": {
                "value": 0.823,
                "metric_name": "mAP",
                "dataset": "MIRFlickr",
                "evidence_ids": ["ev_target"],
            },
        },
    }

    normalized = normalize_paper_spec_payload(
        payload,
        allowed_evidence_ids=["ev_aug", "ev_metric", "ev_base", "ev_target"],
    )
    spec = PaperSpec.model_validate(normalized)

    assert len(spec.training.augmentation) == 1
    assert spec.training.augmentation[0].description == "Random crop and horizontal flip"
    assert spec.training.augmentation[0].evidence_ids == ["ev_aug"]
    assert len(spec.evaluation.metrics) == 1
    assert spec.evaluation.metrics[0].name == "mAP"
    assert spec.evaluation.metrics[0].evidence_ids == ["ev_metric"]
    assert len(spec.evaluation.baselines) == 1
    assert spec.evaluation.baselines[0].name == "DCMH baseline"
    assert spec.evaluation.baselines[0].evidence_ids == ["ev_base"]
    assert len(spec.evaluation.target_results) == 1
    assert spec.evaluation.target_results[0].metric_name == "mAP"
    assert spec.evaluation.target_results[0].value == 0.823
    assert spec.evaluation.target_results[0].dataset == "MIRFlickr"
    assert spec.evaluation.target_results[0].evidence_ids == ["ev_target"]


def test_normalize_paper_spec_repairs_dependency_lists():
    payload = {
        "id": "spec_1",
        "dependencies": {
            "packages": {
                "value": None,
                "notes": "torch",
                "source_type": "paper",
                "evidence_ids": ["ev_pkg"],
            },
            "system_requirements": {
                "value": None,
                "notes": "CUDA toolkit",
                "source_type": "paper",
                "evidence_ids": ["ev_sys"],
            },
        },
    }

    normalized = normalize_paper_spec_payload(
        payload,
        allowed_evidence_ids=["ev_pkg", "ev_sys"],
    )
    spec = PaperSpec.model_validate(normalized)

    assert len(spec.dependencies.packages) == 1
    assert spec.dependencies.packages[0].name == "torch"
    assert spec.dependencies.packages[0].source_type == "paper"
    assert len(spec.dependencies.system_requirements) == 1
    assert spec.dependencies.system_requirements[0].name == "CUDA toolkit"
    assert spec.dependencies.system_requirements[0].source_type == "paper"
    assert spec.dependencies.system_requirements[0].evidence_ids == ["ev_sys"]


def test_normalize_paper_spec_repairs_dataset_entries():
    payload = {
        "id": "spec_1",
        "datasets": [
            {"name": "MIRFLICKR-25K", "download_url": None, "license": None},
            {"name": "IAPR TC-12", "download_url": None, "license": None},
            {"name": "NUS-WIDE", "download_url": None, "license": None},
        ],
    }

    normalized = normalize_paper_spec_payload(payload)
    spec = PaperSpec.model_validate(normalized)

    assert [dataset.name.value for dataset in spec.datasets] == [
        "MIRFLICKR-25K",
        "IAPR TC-12",
        "NUS-WIDE",
    ]
    assert spec.datasets[0].download_url.value is None
    assert spec.datasets[0].download_url.source_type == "missing"
    assert spec.datasets[0].license.value is None
    assert spec.datasets[0].license.source_type == "missing"


def test_normalize_paper_spec_repairs_scalar_extracted_field_sections():
    payload = {
        "id": "spec_1",
        "metadata": {
            "title": "Deep Hashing Paper",
            "authors": ["Alice", "Bob"],
            "year": "2024",
            "venue": None,
        },
        "task": {
            "domain": "cross-modal retrieval",
            "task_type": {"value": "image-text retrieval", "evidence_ids": ["ev_task"]},
            "input_modality": "image",
            "output_modality": "text",
            "problem_formulation": None,
        },
        "model": {
            "architecture_name": "DCMH",
            "architecture_family": None,
            "framework_hint": "pytorch",
            "initialization": None,
        },
        "training": {
            "optimizer": "Adam",
            "learning_rate": "0.0001",
            "batch_size": "32",
            "epochs": None,
            "distributed": False,
        },
        "evaluation": {
            "primary_metric": "mAP",
            "evaluation_protocol": None,
        },
        "dependencies": {
            "framework": "pytorch",
            "framework_version": None,
            "python_version": "3.10",
        },
        "claims_to_reproduce": [
            {
                "description": "Match the reported retrieval metric.",
                "required_data": "MIRFLICKR-25K",
                "required_compute": 1,
            }
        ],
    }

    normalized = normalize_paper_spec_payload(payload, allowed_evidence_ids=["ev_task"])
    spec = PaperSpec.model_validate(normalized)

    assert spec.metadata.title.value == "Deep Hashing Paper"
    assert spec.metadata.authors.value == ["Alice", "Bob"]
    assert spec.metadata.year.value == "2024"
    assert spec.metadata.venue.source_type == "missing"
    assert spec.task.task_type.value == "image-text retrieval"
    assert spec.task.task_type.evidence_ids == ["ev_task"]
    assert spec.task.problem_formulation.source_type == "missing"
    assert spec.model.architecture_name.value == "DCMH"
    assert spec.model.architecture_family.source_type == "missing"
    assert spec.training.optimizer.value == "Adam"
    assert spec.training.learning_rate.value == "0.0001"
    assert spec.training.batch_size.value == "32"
    assert spec.training.epochs.source_type == "missing"
    assert spec.training.distributed.value is False
    assert spec.evaluation.primary_metric.value == "mAP"
    assert spec.evaluation.evaluation_protocol.source_type == "missing"
    assert spec.dependencies.framework.value == "pytorch"
    assert spec.dependencies.framework_version.source_type == "missing"
    assert spec.dependencies.python_version.value == "3.10"
    assert spec.claims_to_reproduce[0].required_data == ["MIRFLICKR-25K"]
    assert spec.claims_to_reproduce[0].required_compute == "1"


def test_normalize_reproduction_plan_maps_goal_and_scope_aliases():
    spec = PaperSpec(id="spec_1")
    constraints = UserConstraints(
        reproduction_goal="small_scale_result", compute_target="single_gpu"
    )
    validation = validate_spec(spec, constraints)
    fallback = draft_reproduction_plan(spec, validation, constraints)

    payload = {
        "objective": {"goal": "small_scale_result"},
        "scope": {
            "device_target": "single_gpu",
            "dataset_mode": "download_allowed",
        },
    }

    normalized = normalize_reproduction_plan_payload(payload, fallback)
    assert normalized["objective"]["goal"] == "small_scale_reproduction"
    assert normalized["scope"]["device_target"] == "cuda"
    assert normalized["scope"]["dataset_mode"] == "download"
    assert normalized["id"] == fallback.id


def test_normalize_reproduction_plan_keeps_validation_confirmations():
    spec = PaperSpec(id="spec_1")
    constraints = UserConstraints()
    validation = validate_spec(spec, constraints)
    fallback = draft_reproduction_plan(spec, validation, constraints)
    payload = {
        "required_confirmations": [
            {
                "id": "llm_batch",
                "field_path": "training.batch_size",
                "question": "LLM changed this",
                "suggested_value": 2,
                "reason": "LLM suggestion",
            }
        ],
        "data_strategy": {
            "synthetic_data_spec": {
                "batch_size": 2,
                "input_shapes": [{"modality": "image", "shape": [3, 224, 224]}],
            }
        },
    }

    normalized = normalize_reproduction_plan_payload(payload, fallback)
    assert normalized["required_confirmations"][0]["id"] == validation.required_confirmations[0].id
    assert normalized["data_strategy"]["synthetic_data_spec"]["input_shapes"][0]["name"] == "image"
