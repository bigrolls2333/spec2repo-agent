from typer.testing import CliRunner
from types import SimpleNamespace
from pathlib import Path

from paper2repo_agent.cli import app
from paper2repo_agent.graph import run_plan
from paper2repo_agent.graphs.execute.nodes import (
    _build_structured_file_contract,
    _failure_category,
    _generated_file_guardrail_error,
    _render_config,
    _render_losses,
    _render_model,
    _render_placeholder_file,
    _target_files_for_failed_check,
    _task,
    _undefined_name_issues,
    _verify_cross_file_api_contracts,
)
from paper2repo_agent.graphs.execute import run_execute
from paper2repo_agent.schemas import (
    CheckResult,
    CodeImplementationPlan,
    ExecutionCodingPlan,
    HumanReview,
    ImplementationCallableInterface,
    ImplementationDatasetInterface,
    ImplementationInterfaceField,
    ImplementationInterfaces,
    ImplementationModelForwardInterface,
    ImplementationLossTermPlan,
    ImplementationTensorContract,
    RunManifest,
)
from paper2repo_agent.utils import read_json, write_json


runner = CliRunner()


def test_execute_fake_run_writes_generated_repo_and_audit_artifacts(tmp_path, monkeypatch):
    monkeypatch.setenv("PAPER2REPO_EXECUTE_LLM_CODEGEN", "0")
    paper = tmp_path / "paper.md"
    paper.write_text(
        "# Example Paper\n\n## Method\nWe propose a classifier.\n\n## Experiments\nSynthetic smoke validation is acceptable.",
        encoding="utf-8",
    )
    out = tmp_path / "run"
    run_plan(paper, out, source_type="markdown", fake_llm=True, verbose=False)

    review = HumanReview.model_validate(read_json(out / "planning" / "human_review.json"))
    decision_file = tmp_path / "decisions.json"
    write_json(
        decision_file,
        {
            "review_id": review.id,
            "global_action": "approve",
            "decisions": [
                {
                    "item_id": item.field_path,
                    "action": "accept",
                    "value": item.suggested_value,
                }
                for item in review.confirmation_items
            ],
        },
    )
    approve = runner.invoke(app, ["approve", str(out), "--decision-file", str(decision_file)])
    assert approve.exit_code == 0, approve.stdout

    result = run_execute(out, verbose=False)

    assert result["execution_result"].final_status == "passed"
    assert (out / "generated_repo" / "requirements.txt").exists()
    assert (out / "generated_repo" / "reproduction_plan.json").exists()
    assert (out / "generated_repo" / "paper_trace.json").exists()
    assert (out / "generated_repo" / "scripts" / "check_artifacts.py").exists()
    assert (out / "execution" / "retrieval_reports.jsonl").exists()
    assert (out / "execution" / "file_contracts.json").exists()
    assert (out / "execution" / "command_specs.json").exists()
    assert (out / "execution" / "tool_calls.jsonl").exists()
    assert (out / "execution" / "command_results.jsonl").exists()
    assert (out / "execution" / "contract_verification.json").exists()
    assert (out / "final_report.md").exists()

    manifest = RunManifest.model_validate(read_json(out / "run_manifest.json"))
    assert manifest.status == "executed"
    assert manifest.retrieval_reports_ref == "execution/retrieval_reports.jsonl"
    assert manifest.file_contracts_ref == "execution/file_contracts.json"
    assert manifest.command_specs_ref == "execution/command_specs.json"
    assert manifest.tool_calls_ref == "execution/tool_calls.jsonl"
    assert manifest.contract_verification_ref == "execution/contract_verification.json"
    assert manifest.execution_result_ref == "execution/execution_result.json"
    assert "execution/retrieval_reports.jsonl" in manifest.artifact_hashes
    assert "execution/file_contracts.json" in manifest.artifact_hashes
    assert "execution/command_specs.json" in manifest.artifact_hashes
    assert "execution/tool_calls.jsonl" in manifest.artifact_hashes


def test_render_placeholder_python_file_is_compile_safe():
    state = {
        "approved_plan": SimpleNamespace(
            plan=SimpleNamespace(
                paper_title='Example "paper"',
                implementation_strategy=SimpleNamespace(
                    architecture_summary='Teacher-student "hashing" module',
                ),
            )
        )
    }
    rendered = _render_placeholder_file("src/example_pkg/teacher.py", state)

    compile(rendered, "teacher.py", "exec")

    assert rendered.startswith('"""Generated placeholder module.')
    assert "Paper title: Example" in rendered
    assert "__all__: list[str] = []" in rendered


def _multimodal_execute_state():
    def value(item):
        return SimpleNamespace(value=item)

    spec = SimpleNamespace(
        task=SimpleNamespace(
            task_type=value("cross_modal_retrieval"),
            output_modality=value("binary_code"),
            input_modality=value(["image", "text"]),
        ),
        model=SimpleNamespace(
            architecture_name=value("BERT"),
            architecture_family=value("two_stream"),
            components=[
                SimpleNamespace(name="full6", hyperparameters={"units": value(4096)}),
                SimpleNamespace(name="text_full1", hyperparameters={"units": value(1000)}),
                SimpleNamespace(name="full8", hyperparameters={"units": value(32)}),
            ],
            inputs=[],
            outputs=[],
            losses=[],
        ),
        training=SimpleNamespace(batch_size=value(2), learning_rate=value(1e-3)),
        datasets=[],
        evaluation=SimpleNamespace(metrics=[], primary_metric=value("mAP")),
    )
    plan = SimpleNamespace(
        data_strategy=SimpleNamespace(
            datasets=[],
            mode="synthetic",
            synthetic_data_spec=SimpleNamespace(
                input_shapes=[SimpleNamespace(shape=[512])],
                num_classes=24,
                num_samples=8,
                batch_size=2,
                task_type="cross_modal_retrieval",
            ),
        ),
        implementation_strategy=SimpleNamespace(architecture_summary="two-stream hashing"),
    )
    return {
        "paper_spec": spec,
        "approved_plan": SimpleNamespace(plan=plan),
        "execution_policy": SimpleNamespace(package_name="example_pkg"),
        "coding_plan": None,
        "file_tasks": [],
        "paths": SimpleNamespace(generated_repo=Path(".")),
    }


def test_render_model_uses_mlp_for_vector_multimodal_inputs():
    state = _multimodal_execute_state()
    rendered = _render_model(state["approved_plan"].plan, state["paper_spec"], [512], 24)

    compile(rendered, "model.py", "exec")

    assert "nn.Conv2d" not in rendered
    assert "flattened_dim = math.prod(input_shape)" in rendered
    assert "return self.backbone(image.float())" in rendered


def test_render_config_exports_model_specific_aliases_and_mapping_access():
    state = _multimodal_execute_state()
    rendered = _render_config(
        [512],
        24,
        "classification",
        8,
        2,
        1e-3,
        state["approved_plan"].plan,
        state["paper_spec"],
    )
    namespace = {}

    exec(rendered, namespace)

    config = namespace["DEFAULT_CONFIG"]
    assert namespace["Config"] is namespace["SmokeConfig"]
    assert namespace["BertConfig"] is namespace["SmokeConfig"]
    assert namespace["BERTConfig"] is namespace["SmokeConfig"]
    assert config["num_classes"] == 24
    assert config.get("num_classes") == 24
    assert config.get("missing", "fallback") == "fallback"


def test_render_losses_exposes_paper_specific_aliases():
    state = _multimodal_execute_state()
    state["paper_spec"].model.losses = [
        SimpleNamespace(name="KL divergence", formula="KL(q||p)"),
        SimpleNamespace(name="Cross entropy with label smoothing", formula="CE"),
    ]
    rendered = _render_losses(state["paper_spec"])

    compile(rendered, "losses.py", "exec")

    assert "def cross_entropy_loss" in rendered
    assert "def cross_entropy_with_label_smoothing" in rendered
    assert "def weighted_cross_entropy" in rendered
    assert "def kl_divergence" in rendered
    assert "def cGAN_loss" in rendered
    assert "def cgan_loss" in rendered
    assert "def L1_loss" in rendered
    assert "def l1_loss" in rendered


def test_cross_file_api_verifier_catches_missing_provider_export():
    state = _multimodal_execute_state()
    sources = {
        "src/example_pkg/losses.py": "def compute_loss(logits, target):\n    return logits\n",
        "src/example_pkg/train.py": (
            "from example_pkg.losses import weighted_cross_entropy\n\n"
            "def run_smoke():\n"
            "    return weighted_cross_entropy(1, 1)\n"
        ),
    }

    records = _verify_cross_file_api_contracts(state, sources)
    issues = [issue for record in records for issue in record.issues]

    assert any(
        issue.category == "missing_symbol" and issue.symbol == "weighted_cross_entropy"
        for issue in issues
    )
    assert any(issue.provider_path == "src/example_pkg/losses.py" for issue in issues)


def test_cross_file_api_verifier_catches_unexpected_keyword_call():
    state = _multimodal_execute_state()
    sources = {
        "src/example_pkg/dataset.py": "def build_train_dataloader(config):\n    return config\n",
        "src/example_pkg/train.py": (
            "from example_pkg.dataset import build_train_dataloader\n\n"
            "def run_smoke():\n"
            "    return build_train_dataloader(batch_size=2)\n"
        ),
    }

    records = _verify_cross_file_api_contracts(state, sources)
    issues = [issue for record in records for issue in record.issues]

    assert any(issue.category == "signature_mismatch" for issue in issues)
    assert any(issue.symbol == "build_train_dataloader" for issue in issues)


def test_cross_file_api_verifier_catches_missing_required_positional_call():
    state = _multimodal_execute_state()
    sources = {
        "src/example_pkg/dataset.py": (
            "def build_train_dataloader(batch_size, num_classes, input_shape):\n"
            "    return batch_size\n"
        ),
        "src/example_pkg/train.py": (
            "from example_pkg.dataset import build_train_dataloader\n\n"
            "def run_smoke(config):\n"
            "    return build_train_dataloader(config)\n"
        ),
    }

    records = _verify_cross_file_api_contracts(state, sources)
    issues = [issue for record in records for issue in record.issues]

    assert any(issue.category == "signature_mismatch" for issue in issues)
    assert any("num_classes" in issue.message for issue in issues)


def test_cross_file_api_verifier_catches_return_unpack_arity_mismatch():
    state = _multimodal_execute_state()
    sources = {
        "src/example_pkg/dataset.py": "def build_train_dataloader(config):\n    return config\n",
        "src/example_pkg/train.py": (
            "from example_pkg.dataset import build_train_dataloader\n\n"
            "def run_smoke(config):\n"
            "    loader, bundle = build_train_dataloader(config)\n"
            "    return loader\n"
        ),
    }

    records = _verify_cross_file_api_contracts(state, sources)
    issues = [issue for record in records for issue in record.issues]

    assert any(issue.category == "integration_mismatch" for issue in issues)
    assert any("returns arity" in issue.message for issue in issues)


def test_failure_category_classifies_required_positional_args_as_signature_mismatch():
    check = CheckResult(
        check_id="pytest_smoke",
        status="failed",
        stderr_tail=(
            "TypeError: build_train_dataloader() missing 3 required positional arguments: "
            "'batch_size', 'num_classes', and 'input_shape'"
        ),
    )

    assert _failure_category(check) == "signature_mismatch"


def test_undefined_name_guardrail_catches_gcn_style_return_drift():
    issues = _undefined_name_issues(
        "src/example_pkg/dataset.py",
        "def build_synthetic_dataset():\n"
        "    adj = 1\n"
        "    labels = 2\n"
        "    return adjacency, labels\n",
    )

    assert any(issue.symbol == "adjacency" for issue in issues)


def test_generated_file_guardrail_rejects_missing_multimodal_loss_helper():
    state = _multimodal_execute_state()
    task = _task(
        "src/example_pkg/losses.py",
        "Loss builders aligned with extracted training objectives",
        "hybrid",
        [],
        [],
    )
    task.file_contract = _build_structured_file_contract(task, state)
    content = """from __future__ import annotations

import torch
from torch import nn


def build_loss() -> nn.Module:
    return nn.CrossEntropyLoss()


def compute_loss(logits: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    return build_loss()(logits, target)
"""

    error = _generated_file_guardrail_error(state, task, content)

    assert error is not None
    assert "compute_cross_modal_hashing_loss" in error


def test_code_plan_loss_interface_displaces_generic_loss_contract():
    state = _multimodal_execute_state()
    ci = CodeImplementationPlan(
        id="ci_hash",
        summary="Cross-modal hashing interface.",
        input_tensors=[
            ImplementationTensorContract(name="image_features", role="input", shape=["B", 4096]),
            ImplementationTensorContract(name="text_features", role="input", shape=["B", 300]),
        ],
        output_tensors=[
            ImplementationTensorContract(name="image_hash_codes", role="output", shape=["B", 64]),
            ImplementationTensorContract(name="text_hash_codes", role="output", shape=["B", 64]),
        ],
        loss_terms=[
            ImplementationLossTermPlan(
                name="alignment_loss",
                target_file="src/example_pkg/losses.py",
                loss_type="bce",
                required_inputs=["image_hash_codes", "text_hash_codes", "S_meta"],
            )
        ],
        interfaces=ImplementationInterfaces(
            dataset_batch=ImplementationDatasetInterface(
                fields=[
                    ImplementationInterfaceField(name="image", shape=["B", 4096]),
                    ImplementationInterfaceField(name="text", shape=["B", 300]),
                    ImplementationInterfaceField(name="similarity_target", shape=["B", "B"]),
                ]
            ),
            model_forward=ImplementationModelForwardInterface(
                call='outputs = model(batch["image"], batch["text"])',
                output_type="dict",
                fields=[
                    ImplementationInterfaceField(name="image_hash_codes", shape=["B", 64]),
                    ImplementationInterfaceField(name="text_hash_codes", shape=["B", 64]),
                ],
            ),
            loss=ImplementationCallableInterface(
                call="loss = compute_cross_modal_hashing_loss(outputs, similarity_target)",
                consumes=[
                    "outputs.image_hash_codes",
                    "outputs.text_hash_codes",
                    "dataset_batch.similarity_target",
                ],
                produces=["loss"],
            ),
        ),
    )
    state["coding_plan"] = ExecutionCodingPlan(
        id="cp_hash",
        package_name="example_pkg",
        architecture_summary="hashing",
        code_implementation_plan=ci,
    )
    task = _task(
        "src/example_pkg/losses.py",
        "Loss builders aligned with extracted training objectives",
        "hybrid",
        [],
        [],
    )

    contract = _build_structured_file_contract(task, state)
    functions = {fn.name: fn.required_params for fn in contract.required_functions}

    assert functions["compute_cross_modal_hashing_loss"] == ["outputs", "similarity_target"]
    assert functions.get("compute_loss") != ["logits", "target"]
    assert any("CodeImplementationPlan interfaces" in line for line in contract.runtime_constraints)


def test_target_files_for_pytest_failure_merges_fallback_modules():
    state = _multimodal_execute_state()
    state["file_tasks"] = [
        _task("src/example_pkg/dataset.py", "", "hybrid", [], []),
        _task("src/example_pkg/model.py", "", "hybrid", [], []),
        _task("src/example_pkg/losses.py", "", "hybrid", [], []),
        _task("src/example_pkg/evaluate.py", "", "hybrid", [], []),
        _task("src/example_pkg/train.py", "", "hybrid", [], []),
        _task("tests/test_smoke.py", "", "hybrid", [], []),
    ]
    check = CheckResult(
        check_id="pytest_smoke",
        status="failed",
        command="python -m pytest -q",
        target_files=["src/example_pkg/losses.py", "src/example_pkg/train.py"],
        stdout_tail="ImportError and RuntimeError occurred",
    )

    targets = _target_files_for_failed_check(check, state)

    assert "src/example_pkg/model.py" in targets
    assert "src/example_pkg/losses.py" in targets
    assert "tests/test_smoke.py" in targets
