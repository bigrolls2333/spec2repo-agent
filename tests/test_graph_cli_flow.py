from typer.testing import CliRunner

from paper2repo_agent.cli import app
from paper2repo_agent.graph import run_plan
from paper2repo_agent.schemas import ApprovedPlan, ReproductionPlan, RunManifest
from paper2repo_agent.utils import read_json

runner = CliRunner()


def test_plan_markdown_fake_llm_writes_artifacts(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text(
        "# Example Paper\n\n## Method\nWe propose a classifier.\n\n## Experiments\nAdamW is used.",
        encoding="utf-8",
    )
    out = tmp_path / "run"

    run_plan(paper, out, source_type="markdown", fake_llm=True)

    manifest = RunManifest.model_validate(read_json(out / "run_manifest.json"))
    assert manifest.status == "planned"
    assert manifest.approval_status == "awaiting_review"
    assert "planning/user_constraints.json" in manifest.artifact_hashes
    assert (out / "planning" / "parsed_paper.json").exists()
    assert (out / "planning" / "evidence_store.jsonl").exists()
    assert (out / "planning" / "evidence_refs.jsonl").exists()
    assert (out / "planning" / "evidence.sqlite").exists()
    assert (out / "planning" / "extracted_spec.json").exists()
    assert (out / "planning" / "user_constraints.json").exists()
    assert (out / "planning" / "approval_decisions.template.json").exists()
    assert (out / "planning" / "human_review.md").exists()


def test_interactive_plan_fake_llm_can_approve(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text("# Example Paper\n\n## Method\nWe propose a classifier.", encoding="utf-8")
    out = tmp_path / "run"

    result = runner.invoke(
        app,
        [
            "plan",
            str(paper),
            "--out",
            str(out),
            "--source-type",
            "markdown",
            "--fake-llm",
            "--interactive",
        ],
        input=_interactive_approve_input(),
    )

    assert result.exit_code == 0, result.stdout
    manifest = RunManifest.model_validate(read_json(out / "run_manifest.json"))
    approved = ApprovedPlan.model_validate(read_json(out / "approval" / "approved_plan.json"))
    assert manifest.status == "approved"
    assert manifest.approval_status == "approved"
    assert approved.execution_allowed is True
    assert (out / "approval" / "iterations" / "000" / "plan.json").exists()
    assert (out / "approval" / "latest_plan.json").exists()

    inspect = runner.invoke(app, ["inspect-run", str(out)])
    assert inspect.exit_code == 0
    assert "approved" in inspect.stdout


def test_interactive_plan_fake_llm_can_revise_then_approve(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text("# Example Paper\n\n## Method\nWe propose a classifier.", encoding="utf-8")
    out = tmp_path / "run"

    result = runner.invoke(
        app,
        [
            "plan",
            str(paper),
            "--out",
            str(out),
            "--source-type",
            "markdown",
            "--fake-llm",
            "--interactive",
        ],
        input=_interactive_revise_then_approve_input(),
    )

    assert result.exit_code == 0, result.stdout
    manifest = RunManifest.model_validate(read_json(out / "run_manifest.json"))
    latest_plan = ReproductionPlan.model_validate(read_json(out / "approval" / "latest_plan.json"))
    assert manifest.status == "approved"
    assert manifest.approval_iteration_count >= 2
    assert latest_plan.user_constraints.compute_target == "single_gpu"
    assert latest_plan.scope.device_target == "cuda"
    assert (out / "approval" / "iterations" / "000" / "plan.json").exists()
    assert (out / "approval" / "iterations" / "001" / "plan.json").exists()


def test_review_command_can_resume_existing_run(tmp_path):
    paper = tmp_path / "paper.md"
    paper.write_text("# Example Paper\n\n## Method\nWe propose a classifier.", encoding="utf-8")
    out = tmp_path / "run"
    run_plan(paper, out, source_type="markdown", fake_llm=True)

    result = runner.invoke(
        app,
        ["review", str(out), "--fake-llm"],
        input=_review_only_approve_input(),
    )

    assert result.exit_code == 0, result.stdout
    manifest = RunManifest.model_validate(read_json(out / "run_manifest.json"))
    assert manifest.approval_status == "approved"
    assert (out / "approval" / "approved_plan.json").exists()


def _interactive_approve_input() -> str:
    return (
        "\n".join(
            [
                "",
                "cpu_smoke",
                "smoke_test",
                "10",
                "3.10",
                "pip",
                "n",
                "n",
                "synthetic_only",
                *_accept_all_confirmations(8),
                "approve",
                "",
            ]
        )
        + "\n"
    )


def _interactive_revise_then_approve_input() -> str:
    return (
        "\n".join(
            [
                "",
                "cpu_smoke",
                "smoke_test",
                "10",
                "3.10",
                "pip",
                "n",
                "n",
                "synthetic_only",
                "override",
                "option",
                "2",
                "",
                *_accept_all_confirmations(7),
                "revise",
                "",
                *_accept_all_confirmations(8),
                "approve",
                "",
            ]
        )
        + "\n"
    )


def _review_only_approve_input() -> str:
    return (
        "\n".join(
            [
                *_accept_all_confirmations(8),
                "approve",
                "",
            ]
        )
        + "\n"
    )


def _accept_all_confirmations(count: int) -> list[str]:
    lines: list[str] = []
    for _ in range(count):
        lines.extend(["accept", ""])
    return lines
