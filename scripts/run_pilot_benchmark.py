from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
TEST_PAPER_DIR = REPO_ROOT / "test_paper"
DEFAULT_BENCHMARK_DIR = REPO_ROOT / "benchmarks"


@dataclass(frozen=True)
class BenchmarkCase:
    name: str
    paper_file: str
    display_name: str
    expected_signals: tuple[str, ...]


CASES: dict[str, BenchmarkCase] = {
    "gcn": BenchmarkCase(
        name="gcn",
        paper_file="GCN.pdf",
        display_name="Graph Convolutional Networks",
        expected_signals=(
            "graph convolution",
            "semi-supervised",
            "citation",
            "cora",
            "citeseer",
            "pubmed",
            "kipf",
            "welling",
        ),
    ),
    "resnet": BenchmarkCase(
        name="resnet",
        paper_file="resnet.pdf",
        display_name="Deep Residual Learning for Image Recognition",
        expected_signals=(
            "residual",
            "shortcut",
            "identity",
            "bottleneck",
            "imagenet",
            "cifar",
            "sgd",
            "top-5",
        ),
    ),
    "transformer": BenchmarkCase(
        name="transformer",
        paper_file="attention.pdf",
        display_name="Attention Is All You Need",
        expected_signals=(
            "attention",
            "self-attention",
            "multi-head",
            "encoder",
            "decoder",
            "positional encoding",
            "wmt",
            "label smoothing",
        ),
    ),
    "unet": BenchmarkCase(
        name="unet",
        paper_file="UNET.pdf",
        display_name="U-Net: Convolutional Networks for Biomedical Image Segmentation",
        expected_signals=(
            "u-net",
            "biomedical",
            "segmentation",
            "contracting path",
            "expanding path",
            "skip connection",
            "up-convolution",
            "data augmentation",
        ),
    ),
    "pix2pix": BenchmarkCase(
        name="pix2pix",
        paper_file="pix2pix.pdf",
        display_name="Image-to-Image Translation with Conditional Adversarial Networks",
        expected_signals=(
            "image-to-image",
            "conditional adversarial",
            "generator",
            "discriminator",
            "l1 loss",
            "gan loss",
            "paired",
            "patchgan",
        ),
    ),
    "vae": BenchmarkCase(
        name="vae",
        paper_file="VAE.pdf",
        display_name="Auto-Encoding Variational Bayes",
        expected_signals=(
            "variational autoencoder",
            "encoder",
            "decoder",
            "latent",
            "reparameterization",
            "elbo",
            "kl divergence",
            "variational inference",
        ),
    ),
    "faster_rcnn": BenchmarkCase(
        name="faster_rcnn",
        paper_file="faster r-cnn.pdf",
        display_name="Faster R-CNN: Towards Real-Time Object Detection with Region Proposal Networks",
        expected_signals=(
            "faster r-cnn",
            "region proposal network",
            "rpn",
            "roi",
            "object detection",
            "anchor",
            "bounding box",
            "classification",
        ),
    ),
    "bert": BenchmarkCase(
        name="bert",
        paper_file="BERT.pdf",
        display_name="BERT: Pre-training of Deep Bidirectional Transformers for Language Understanding",
        expected_signals=(
            "bert",
            "bidirectional",
            "transformer encoder",
            "masked language model",
            "next sentence prediction",
            "pre-training",
            "fine-tuning",
            "wordpiece",
        ),
    ),
}


def main() -> int:
    args = parse_args()
    selected_cases = [CASES[name] for name in args.cases]
    run_id = args.run_id or datetime.now().strftime("pilot_%Y%m%d_%H%M%S")
    benchmark_dir = (args.output_dir or DEFAULT_BENCHMARK_DIR) / run_id
    benchmark_dir.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for case in selected_cases:
        if args.summarize_existing:
            result = summarize_existing_case(case=case, benchmark_dir=benchmark_dir)
        else:
            result = run_case(case=case, benchmark_dir=benchmark_dir, args=args)
        results.append(result)
        write_summary_files(benchmark_dir, results)
        if result["status"] != "passed" and args.stop_on_failure:
            break

    write_summary_files(benchmark_dir, results)
    print(f"Benchmark directory: {benchmark_dir}")
    print(f"Summary: {benchmark_dir / 'benchmark_summary.md'}")
    return 0 if all(item["status"] == "passed" for item in results) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Paper2Repo pilot benchmark.")
    parser.add_argument(
        "--cases",
        nargs="+",
        default=list(CASES),
        choices=sorted(CASES),
        help="Cases to run.",
    )
    parser.add_argument("--run-id", default="", help="Benchmark run id. Defaults to timestamp.")
    parser.add_argument("--output-dir", type=Path, default=None, help="Benchmark output directory.")
    parser.add_argument(
        "--fake-llm", action="store_true", help="Use deterministic fake LLM output."
    )
    parser.add_argument("--skip-execute", action="store_true", help="Run plan and approval only.")
    parser.add_argument(
        "--summarize-existing",
        action="store_true",
        help="Regenerate summaries from existing run dirs.",
    )
    parser.add_argument(
        "--force", action="store_true", help="Allow reusing an existing case directory."
    )
    parser.add_argument(
        "--stop-on-failure", action="store_true", help="Stop after the first failed case."
    )
    parser.add_argument("--timeout-plan", type=int, default=1800, help="Plan timeout in seconds.")
    parser.add_argument(
        "--timeout-approve", type=int, default=300, help="Approve timeout in seconds."
    )
    parser.add_argument(
        "--timeout-execute", type=int, default=2400, help="Execute timeout in seconds."
    )
    parser.add_argument(
        "--approved-by", default="pilot-benchmark", help="Approver name for auto approval."
    )
    return parser.parse_args()


def run_case(case: BenchmarkCase, benchmark_dir: Path, args: argparse.Namespace) -> dict[str, Any]:
    paper_path = TEST_PAPER_DIR / case.paper_file
    case_dir = benchmark_dir / case.name
    run_dir = case_dir / "run"
    logs_dir = case_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    if case_dir.exists() and any(case_dir.iterdir()) and not args.force:
        existing_summary = case_dir / "case_summary.json"
        if existing_summary.exists():
            return read_json(existing_summary)

    result: dict[str, Any] = {
        "case": case.name,
        "display_name": case.display_name,
        "paper_path": str(paper_path.relative_to(REPO_ROOT)),
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "status": "running",
        "steps": {},
        "artifacts": {},
        "signals": {},
        "error": "",
    }

    if not paper_path.exists():
        result["status"] = "failed"
        result["error"] = f"Paper file not found: {paper_path}"
        write_case_summary(case_dir, result)
        return result

    plan_cmd = [
        sys.executable,
        "-m",
        "paper2repo_agent.cli",
        "plan",
        str(paper_path),
        "--source-type",
        "pdf",
        "--out",
        str(run_dir),
        "--quiet",
    ]
    if args.fake_llm:
        plan_cmd.append("--fake-llm")
    result["steps"]["plan"] = run_command(plan_cmd, logs_dir / "01_plan.log", args.timeout_plan)
    if result["steps"]["plan"]["status"] != "passed":
        return finalize_case(case, case_dir, run_dir, result, "failed")

    decision_file = run_dir / "planning" / "approval_decisions.template.json"
    approve_cmd = [
        sys.executable,
        "-m",
        "paper2repo_agent.cli",
        "approve",
        str(run_dir),
        "--decision-file",
        str(decision_file),
        "--approved-by",
        args.approved_by,
    ]
    result["steps"]["approve"] = run_command(
        approve_cmd, logs_dir / "02_approve.log", args.timeout_approve
    )
    if result["steps"]["approve"]["status"] != "passed":
        return finalize_case(case, case_dir, run_dir, result, "failed")

    if args.skip_execute:
        return finalize_case(case, case_dir, run_dir, result, "planned")

    execute_cmd = [
        sys.executable,
        "-m",
        "paper2repo_agent.cli",
        "execute",
        str(run_dir),
        "--quiet",
    ]
    result["steps"]["execute"] = run_command(
        execute_cmd, logs_dir / "03_execute.log", args.timeout_execute
    )
    if result["steps"]["execute"]["status"] != "passed":
        return finalize_case(case, case_dir, run_dir, result, "failed")

    verification = read_json(run_dir / "execution" / "verification_result.json")
    final_report = read_json(run_dir / "final_report.json")
    execution_result = read_json(run_dir / "execution" / "execution_result.json")
    verification_passed = bool(verification.get("passed"))
    final_status = (
        final_report.get("status")
        or execution_result.get("final_status")
        or execution_result.get("status")
        or ""
    )
    result["verification_passed"] = verification_passed
    result["final_status"] = final_status
    return finalize_case(
        case, case_dir, run_dir, result, "passed" if verification_passed else "failed"
    )


def summarize_existing_case(case: BenchmarkCase, benchmark_dir: Path) -> dict[str, Any]:
    case_dir = benchmark_dir / case.name
    run_dir = case_dir / "run"
    old_summary = read_json(case_dir / "case_summary.json")
    result: dict[str, Any] = {
        "case": case.name,
        "display_name": case.display_name,
        "paper_path": str((TEST_PAPER_DIR / case.paper_file).relative_to(REPO_ROOT)),
        "run_dir": str(run_dir.relative_to(REPO_ROOT)),
        "status": "failed",
        "steps": old_summary.get("steps", {}),
        "artifacts": {},
        "signals": {},
        "error": "",
    }
    if not run_dir.exists():
        result["status"] = "missing"
        result["error"] = f"Run dir not found: {run_dir}"
        write_case_summary(case_dir, result)
        return result

    verification = read_json(run_dir / "execution" / "verification_result.json")
    verification_passed = bool(verification.get("passed"))
    status = "passed" if verification_passed else "failed"
    return finalize_case(case, case_dir, run_dir, result, status)


def run_command(command: list[str], log_path: Path, timeout: int) -> dict[str, Any]:
    started = time.perf_counter()
    env = os.environ.copy()
    src_path = str(REPO_ROOT / "src")
    env["PYTHONPATH"] = src_path + os.pathsep + env.get("PYTHONPATH", "")
    try:
        completed = subprocess.run(
            command,
            cwd=REPO_ROOT,
            env=env,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        duration = time.perf_counter() - started
        log_path.write_text(
            format_command_log(
                command, completed.stdout, completed.stderr, completed.returncode, duration
            ),
            encoding="utf-8",
        )
        return {
            "status": "passed" if completed.returncode == 0 else "failed",
            "returncode": completed.returncode,
            "duration_seconds": round(duration, 2),
            "log_path": str(log_path.relative_to(REPO_ROOT)),
            "command": subprocess.list2cmdline(command),
        }
    except subprocess.TimeoutExpired as exc:
        duration = time.perf_counter() - started
        stdout = decode_partial_output(exc.stdout)
        stderr = decode_partial_output(exc.stderr)
        log_path.write_text(
            format_command_log(
                command, stdout, stderr, returncode=None, duration=duration, timed_out=True
            ),
            encoding="utf-8",
        )
        return {
            "status": "timeout",
            "returncode": None,
            "duration_seconds": round(duration, 2),
            "log_path": str(log_path.relative_to(REPO_ROOT)),
            "command": subprocess.list2cmdline(command),
        }


def finalize_case(
    case: BenchmarkCase,
    case_dir: Path,
    run_dir: Path,
    result: dict[str, Any],
    status: str,
) -> dict[str, Any]:
    result["status"] = status
    result["artifacts"] = collect_artifacts(run_dir)
    result.update(collect_paper_summary(run_dir))
    result["signals"] = collect_signal_matches(case, run_dir)
    write_case_summary(case_dir, result)
    return result


def collect_artifacts(run_dir: Path) -> dict[str, bool]:
    rel_paths = [
        "run_manifest.json",
        "planning/parsed_paper.json",
        "planning/evidence_store.jsonl",
        "planning/evidence.sqlite",
        "planning/extracted_spec.json",
        "planning/validation_result.json",
        "planning/draft_plan.json",
        "planning/human_review.md",
        "planning/approval_decisions.template.json",
        "approval/approved_plan.json",
        "generated_repo/README.md",
        "execution/execution_policy.json",
        "execution/codebase_design.json",
        "execution/coding_plan.json",
        "execution/file_generation_tasks.json",
        "execution/file_contracts.json",
        "execution/verification_result.json",
        "execution/failure_digest.json",
        "execution/patch_plan.json",
        "execution/tool_calls.jsonl",
        "execution/execution_result.json",
        "final_report.md",
        "final_report.json",
    ]
    return {path: (run_dir / path).exists() for path in rel_paths}


def collect_paper_summary(run_dir: Path) -> dict[str, Any]:
    spec = read_json(run_dir / "planning" / "extracted_spec.json")
    validation = read_json(run_dir / "planning" / "validation_result.json")
    verification = read_json(run_dir / "execution" / "verification_result.json")
    final_report = read_json(run_dir / "final_report.json")
    execution_result = read_json(run_dir / "execution" / "execution_result.json")
    return {
        "paper_title": field_value(nested_get(spec, ["metadata", "title"]))
        or field_value(spec.get("title"))
        or "",
        "task_type": field_value(nested_get(spec, ["task", "task_type"])) or "",
        "model_name": (
            field_value(nested_get(spec, ["model", "architecture_name"]))
            or field_value(nested_get(spec, ["model", "name"]))
            or ""
        ),
        "support_level": validation.get("support_level", ""),
        "blocking_issues": len(validation.get("blocking_issues", [])),
        "warnings": len(validation.get("warnings", [])),
        "verification_passed": verification.get("passed"),
        "verification_summary": verification.get("summary", ""),
        "final_status": final_report.get("status") or execution_result.get("final_status") or "",
        "failed_commands": collect_failed_commands(run_dir),
    }


def collect_signal_matches(case: BenchmarkCase, run_dir: Path) -> dict[str, Any]:
    text_parts: list[str] = []
    for rel_path in [
        "planning/extracted_spec.json",
        "planning/draft_plan.json",
        "planning/human_review.md",
        "approval/approved_plan.json",
        "execution/codebase_design.json",
        "execution/coding_plan.json",
        "generated_repo/README.md",
        "generated_repo/paper_trace.json",
        "final_report.md",
    ]:
        path = run_dir / rel_path
        if path.exists():
            text_parts.append(path.read_text(encoding="utf-8", errors="replace"))
    text = "\n".join(text_parts).lower()
    matched = [term for term in case.expected_signals if term.lower() in text]
    missing = [term for term in case.expected_signals if term.lower() not in text]
    total = len(case.expected_signals)
    return {
        "matched": matched,
        "missing": missing,
        "score": round(len(matched) / total, 3) if total else 0.0,
        "matched_count": len(matched),
        "total": total,
    }


def write_case_summary(case_dir: Path, result: dict[str, Any]) -> None:
    case_dir.mkdir(parents=True, exist_ok=True)
    write_json(case_dir / "case_summary.json", result)
    (case_dir / "case_summary.md").write_text(render_case_markdown(result), encoding="utf-8")


def write_summary_files(benchmark_dir: Path, results: list[dict[str, Any]]) -> None:
    payload = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "repo_root": str(REPO_ROOT),
        "case_count": len(results),
        "passed_count": sum(1 for item in results if item.get("status") == "passed"),
        "planned_count": sum(1 for item in results if item.get("status") == "planned"),
        "failed_count": sum(1 for item in results if item.get("status") == "failed"),
        "cases": results,
    }
    write_json(benchmark_dir / "benchmark_summary.json", payload)
    (benchmark_dir / "benchmark_summary.md").write_text(
        render_benchmark_markdown(payload), encoding="utf-8"
    )


def render_case_markdown(result: dict[str, Any]) -> str:
    lines = [
        f"# {result['display_name']}",
        "",
        f"- Case: `{result['case']}`",
        f"- Status: `{result['status']}`",
        f"- Run dir: `{result['run_dir']}`",
        f"- Paper title: {result.get('paper_title') or ''}",
        f"- Task type: {result.get('task_type') or ''}",
        f"- Model name: {result.get('model_name') or ''}",
        f"- Support level: `{result.get('support_level') or ''}`",
        f"- Verification passed: `{result.get('verification_passed')}`",
        "",
        "## Failed Commands",
        "",
    ]
    failed_commands = result.get("failed_commands", [])
    if failed_commands:
        lines.extend(["| Command | Exit Code | Cause |", "|---|---:|---|"])
        for item in failed_commands:
            lines.append(
                f"| `{item.get('command_id')}` | `{item.get('exit_code')}` | {safe_md(item.get('cause', ''))} |"
            )
    else:
        lines.append("No failed command recorded.")
    lines.extend(
        [
            "",
            "## Steps",
            "",
            "| Step | Status | Return Code | Seconds | Log |",
            "|---|---:|---:|---:|---|",
        ]
    )
    for step, data in result.get("steps", {}).items():
        lines.append(
            f"| {step} | {data.get('status')} | {data.get('returncode')} | "
            f"{data.get('duration_seconds')} | `{data.get('log_path')}` |"
        )
    signals = result.get("signals", {})
    lines.extend(
        [
            "",
            "## Alignment Signals",
            "",
            f"- Score: `{signals.get('score', 0)}` ({signals.get('matched_count', 0)}/{signals.get('total', 0)})",
            f"- Matched: {', '.join(signals.get('matched', []))}",
            f"- Missing: {', '.join(signals.get('missing', []))}",
            "",
            "## Artifact Checklist",
            "",
            "| Artifact | Exists |",
            "|---|---:|",
        ]
    )
    for artifact, exists in result.get("artifacts", {}).items():
        lines.append(f"| `{artifact}` | `{exists}` |")
    return "\n".join(lines) + "\n"


def render_benchmark_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Paper2Repo Pilot Benchmark",
        "",
        f"- Created at: `{payload['created_at']}`",
        f"- Case count: `{payload['case_count']}`",
        f"- Passed: `{payload['passed_count']}`",
        f"- Planned only: `{payload['planned_count']}`",
        f"- Failed: `{payload['failed_count']}`",
        "",
        "## Results",
        "",
        "| Case | Status | Paper Title | Task | Model | Support | Verification | Signal Score | Failed Commands | Run Dir |",
        "|---|---:|---|---|---|---:|---:|---:|---|---|",
    ]
    for item in payload.get("cases", []):
        signals = item.get("signals", {})
        lines.append(
            f"| {item.get('case')} | {item.get('status')} | {safe_md(item.get('paper_title', ''))} | "
            f"{safe_md(item.get('task_type', ''))} | {safe_md(item.get('model_name', ''))} | "
            f"{item.get('support_level', '')} | {item.get('verification_passed')} | "
            f"{signals.get('score', '')} | {format_failed_commands(item.get('failed_commands', []))} | "
            f"`{item.get('run_dir')}` |"
        )
    lines.extend(["", "## Step Logs", ""])
    for item in payload.get("cases", []):
        lines.append(f"### {item.get('case')}")
        for step, data in item.get("steps", {}).items():
            lines.append(f"- {step}: `{data.get('status')}`, log `{data.get('log_path')}`")
        lines.append("")
    return "\n".join(lines)


def format_command_log(
    command: list[str],
    stdout: str,
    stderr: str,
    returncode: int | None,
    duration: float,
    timed_out: bool = False,
) -> str:
    header = [
        f"$ {subprocess.list2cmdline(command)}",
        f"returncode: {returncode}",
        f"duration_seconds: {duration:.2f}",
        f"timed_out: {timed_out}",
        "",
        "=== STDOUT ===",
        stdout or "",
        "",
        "=== STDERR ===",
        stderr or "",
        "",
    ]
    return "\n".join(header)


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def field_value(value: Any) -> Any:
    if isinstance(value, dict) and "value" in value:
        return value.get("value")
    return value


def nested_get(data: dict[str, Any], keys: list[str]) -> Any:
    current: Any = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def collect_failed_commands(run_dir: Path) -> list[dict[str, Any]]:
    failed: list[dict[str, Any]] = []
    for row in read_jsonl(run_dir / "execution" / "command_results.jsonl"):
        if row.get("status") != "failed":
            continue
        failed.append(
            {
                "command_id": row.get("command_id"),
                "exit_code": row.get("exit_code"),
                "cause": extract_error_cause(
                    row.get("stdout_tail") or row.get("stderr_tail") or ""
                ),
            }
        )
    return failed


def extract_error_cause(text: str) -> str:
    exception_markers = [
        "ImportError:",
        "ModuleNotFoundError:",
        "NameError:",
        "TypeError:",
        "ValueError:",
        "AssertionError:",
    ]
    fallback_markers = [
        "FAILED ",
        "ERROR ",
    ]
    lines = [line.strip() for line in text.splitlines()]
    for stripped in lines:
        if any(marker in stripped for marker in exception_markers):
            return stripped[:300]
    for stripped in lines:
        if any(marker in stripped for marker in fallback_markers):
            return stripped[:300]
    return text.replace("\n", " ")[:300]


def decode_partial_output(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def safe_md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")


def format_failed_commands(commands: list[dict[str, Any]]) -> str:
    if not commands:
        return ""
    parts = []
    for item in commands:
        command_id = item.get("command_id") or "unknown"
        cause = safe_md(item.get("cause") or "")
        parts.append(f"`{command_id}`: {cause}")
    return "<br>".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
