"""Drive a single paper end-to-end through plan + auto-approve + execute.

Usage (run as a subprocess via run_all.py so one crash can't kill the batch):
    python -m bench.run_one <paper.pdf> <runs_dir> [--label <n>] [--source-type auto]

Emits a per-paper result JSON next to the run directory:
    <runs_dir>/<paper-stem>.<label>/{result.json}
where result.json records status, timings, and key execution metrics
(verification passed, agent trace count, P3 rollback triggered, etc.).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

# Make the package importable when run as a script from repo root.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for p in (str(ROOT), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)

from paper2repo_agent.graphs.plan.workflow import run_plan  # noqa: E402
from paper2repo_agent.graphs.execute.workflow import run_execute  # noqa: E402
from paper2repo_agent.schemas import HumanReview  # noqa: E402
from paper2repo_agent.utils import read_json, write_json  # noqa: E402
from paper2repo_agent.cli import app as cli_app  # noqa: E402
from typer.testing import CliRunner  # noqa: E402


def _approve_run(run_dir: Path) -> tuple[bool, str]:
    """Auto-approve the human review by accepting every suggested value.

    Drives the real CLI ``approve`` command (via CliRunner) so manifest refs,
    iteration records, and the approved-plan artifact are written exactly as
    in interactive use — no reimplementation drift.
    """
    review_path = run_dir / "planning" / "human_review.json"
    if not review_path.exists():
        return False, f"missing {review_path}"
    try:
        review = HumanReview.model_validate(read_json(review_path))
    except Exception as exc:  # noqa: BLE001
        return False, f"failed to parse human_review.json: {exc!r}"
    decisions = [
        {
            "item_id": item.field_path,
            "action": "accept",
            "value": item.suggested_value,
        }
        for item in review.confirmation_items
    ]
    payload = {
        "review_id": review.id,
        "global_action": "approve",
        "decisions": decisions,
    }
    decision_file = run_dir / "planning" / "auto_decisions.json"
    write_json(decision_file, payload)
    runner = CliRunner()
    res = runner.invoke(
        cli_app,
        ["approve", str(run_dir), "--decision-file", str(decision_file)],
    )
    if res.exit_code != 0:
        return False, f"approve exit={res.exit_code}: {res.stdout[-500:]}"
    approved_path = run_dir / "approval" / "approved_plan.json"  # paths.approved_plan
    if not approved_path.exists():
        return False, f"approve exit=0 but {approved_path} missing; stdout={res.stdout[-300:]}"
    return True, f"approved ({len(decisions)} items)"


def _collect_exec_metrics(run_dir: Path, exec_state: dict) -> dict:
    """Pull a compact, resume-worthy metric set from the execution result."""
    metrics: dict = {}
    metrics["execution_status"] = exec_state.get("execution_status")
    ver = exec_state.get("verification_result")
    metrics["verification_passed"] = bool(getattr(ver, "passed", None)) if ver is not None else None
    metrics["verification_summary"] = (getattr(ver, "summary", "") if ver is not None else "")[:300]

    agent_traces = exec_state.get("agent_traces") or []
    metrics["agent_trace_count"] = len(agent_traces)
    statuses: dict[str, int] = {}
    for t in agent_traces:
        st = getattr(t, "final_status", "success")
        statuses[st] = statuses.get(st, 0) + 1
    metrics["agent_final_status_breakdown"] = statuses
    metrics["plan_assumption_invalid_count"] = sum(
        1 for t in agent_traces if getattr(t, "final_status", "") == "plan_assumption_invalid"
    )

    pf = exec_state.get("plan_feedback")
    if pf is not None:
        metrics["plan_feedback"] = {
            "confirmed": pf.confirmed_count,
            "falsified": pf.falsified_count,
            "inconclusive": pf.inconclusive_count,
            "untested": pf.untested_count,
            "rollback_triggered_flag": pf.rollback_triggered,
        }
    metrics["rollback_count"] = exec_state.get("rollback_count")
    metrics["rollback_triggered"] = exec_state.get("rollback_triggered")
    metrics["plan_trace_revisions"] = len(exec_state.get("plan_trace_revisions") or [])

    # Generated repo stats
    repo_dir = exec_state.get("repo_dir") or str(run_dir / "execution" / "generated_repo")
    repo_path = Path(repo_dir)
    try:
        if repo_path.exists():
            py_files = list(repo_path.rglob("*.py"))
            metrics["generated_py_file_count"] = len(py_files)
            total_loc = 0
            for f in py_files:
                try:
                    total_loc += sum(1 for _ in f.open(encoding="utf-8", errors="ignore"))
                except OSError:
                    pass
            metrics["generated_py_loc"] = total_loc
        else:
            metrics["generated_py_file_count"] = 0
    except Exception:  # noqa: BLE001
        pass

    # cost report
    cr = exec_state.get("cost_report")
    if cr is not None:
        metrics["llm_call_count"] = len(
            getattr(cr, "llm_calls", []) or getattr(cr, "llm_call_records", []) or []
        )
        try:
            metrics["total_cost"] = getattr(cr, "total_cost_usd", None) or getattr(
                cr, "total_cost", None
            )
        except Exception:  # noqa: BLE001
            metrics["total_cost"] = None
    return metrics


def run_one(paper_path: Path, runs_dir: Path, *, label: str, source_type: str) -> dict:
    runs_dir.mkdir(parents=True, exist_ok=True)
    paper_stem = paper_path.stem
    run_dir = runs_dir / f"{paper_stem}.{label}"
    run_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "paper": paper_path.name,
        "paper_stem": paper_stem,
        "label": label,
        "run_dir": str(run_dir),
        "source_type": source_type,
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    # ── plan ────────────────────────────────────────────────────────────────
    t0 = time.time()
    try:
        plan_state = run_plan(
            paper_path=paper_path,
            output_dir=run_dir,
            source_type=source_type,
            fake_llm=False,
            verbose=False,
        )
    except Exception as exc:  # noqa: BLE001
        result["phase"] = "plan"
        result["status"] = "error"
        result["error"] = repr(exc)
        result["traceback"] = traceback.format_exc()[-2000:]
        result["elapsed_s"] = round(time.time() - t0, 1)
        return result
    result["plan_elapsed_s"] = round(time.time() - t0, 1)
    result["plan_status"] = plan_state.get("plan_status") or plan_state.get("status")
    mp = run_dir / "planning" / "planning_trace.json"
    try:
        if mp.exists():
            pt = read_json(mp)
            result["planning_trace_step_count"] = len(pt.get("steps", []))
    except Exception:  # noqa: BLE001
        pass

    # ── approve ─────────────────────────────────────────────────────────────
    tA = time.time()
    ok, msg = _approve_run(run_dir)
    result["approve_ok"] = ok
    result["approve_msg"] = msg
    result["approve_elapsed_s"] = round(time.time() - tA, 2)
    if not ok:
        result["phase"] = "approve"
        result["status"] = "error"
        result["error"] = msg
        result["elapsed_s"] = round(time.time() - t0, 1)
        return result

    # ── execute ──────────────────────────────────────────────────────────────
    tE = time.time()
    try:
        exec_state = run_execute(run_dir, verbose=False, progress_callback=None)
    except Exception as exc:  # noqa: BLE001
        result["phase"] = "execute"
        result["status"] = "error"
        result["error"] = repr(exc)
        result["traceback"] = traceback.format_exc()[-2000:]
        result["elapsed_s"] = round(time.time() - t0, 1)
        return result
    result["execute_elapsed_s"] = round(time.time() - tE, 1)
    result["total_elapsed_s"] = round(time.time() - t0, 1)

    try:
        result["metrics"] = _collect_exec_metrics(run_dir, dict(exec_state))
    except Exception as exc:  # noqa: BLE001
        result["metrics_collection_error"] = repr(exc)

    # determine a top-level success-ish status
    m = result.get("metrics", {})
    if m.get("execution_status") == "success" or m.get("verification_passed"):
        result["status"] = "success"
    else:
        result["status"] = "completed_no_pass"
    result["phase"] = "execute"
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paper_path", type=Path)
    ap.add_argument("runs_dir", type=Path)
    ap.add_argument("--label", default="run")
    ap.add_argument("--source-type", default="auto")
    args = ap.parse_args()

    result = run_one(
        args.paper_path,
        args.runs_dir,
        label=args.label,
        source_type=args.source_type,
    )
    out_path = Path(result["run_dir"]) / "result.json"
    write_json(out_path, result)
    # also print a compact line for the orchestrator
    print(
        json.dumps(
            {
                "paper": result.get("paper"),
                "label": result.get("label"),
                "status": result.get("status"),
                "phase": result.get("phase"),
                "total_elapsed_s": result.get("total_elapsed_s"),
                "plan_elapsed_s": result.get("plan_elapsed_s"),
                "execute_elapsed_s": result.get("execute_elapsed_s"),
                "error": result.get("error"),
                "metrics": result.get("metrics"),
                "result_json": str(out_path),
            },
            ensure_ascii=False,
            default=str,
        )
    )
    return 0 if result.get("status") in {"success", "completed_no_pass"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
