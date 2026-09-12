"""Batch-orchestrate all papers in test_paper/ through bench.run_one.

Each paper runs in its own subprocess so a crash, OOM, or hang in one does
not corrupt the others; a per-paper wall-clock cap kills stragglers.

Usage:
    python -m bench.run_all [--dir test_paper] [--runs runs] [--label-round 1]
                            [--per-paper-timeout 2700] [--start 0] [--limit 9]

It writes a combined runs/bench_report.<label-round>.json at the end.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def list_papers(dir_: Path) -> list[Path]:
    return sorted(p for p in dir_.iterdir() if p.suffix.lower() == ".pdf")


def run_paper_subprocess(
    paper: Path, runs_dir: Path, label: str, source_type: str, timeout: int
) -> dict:
    """Run one paper in a fresh subprocess; return its compact result line dict."""
    cmd = [
        sys.executable,
        "-u",
        str(ROOT / "bench" / "run_one.py"),
        str(paper),
        str(runs_dir),
        "--label",
        label,
        "--source-type",
        source_type,
    ]
    t0 = time.time()
    env_log = runs_dir / f"{paper.stem}.{label}.log"
    runs_dir.mkdir(parents=True, exist_ok=True)
    try:
        with env_log.open("w", encoding="utf-8") as logf:
            proc = subprocess.run(
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                cwd=str(ROOT),
            )
        rc = proc.returncode
        timed_out = False
    except subprocess.TimeoutExpired:
        rc = -1
        timed_out = True
    elapsed = round(time.time() - t0, 1)

    # parse the compact JSON line at the end of the log (run_one prints it last)
    result_line: dict = {}
    try:
        for line in reversed(env_log.read_text(encoding="utf-8", errors="ignore").splitlines()):
            line = line.strip()
            if line.startswith("{") and '"paper"' in line:
                result_line = json.loads(line)
                break
    except Exception:  # noqa: BLE001
        pass

    return {
        "paper": paper.name,
        "label": label,
        "rc": rc,
        "timed_out": timed_out,
        "elapsed_s": elapsed,
        "status": result_line.get("status"),
        "phase": result_line.get("phase"),
        "error": result_line.get("error"),
        "total_elapsed_s": result_line.get("total_elapsed_s"),
        "plan_elapsed_s": result_line.get("plan_elapsed_s"),
        "execute_elapsed_s": result_line.get("execute_elapsed_s"),
        "metrics": result_line.get("metrics"),
        "log": str(env_log),
        "result_json": result_line.get("result_json"),
    }


def aggregate(results: list[dict]) -> dict:
    total = len(results)
    success = sum(1 for r in results if r.get("status") == "success")
    completed_no_pass = sum(1 for r in results if r.get("status") == "completed_no_pass")
    errors = sum(1 for r in results if r.get("status") == "error")
    timeouts = sum(1 for r in results if r.get("timed_out"))
    plan_err = sum(1 for r in results if r.get("phase") == "plan" and r.get("status") == "error")
    exec_err = sum(1 for r in results if r.get("phase") == "execute" and r.get("status") == "error")
    ver_passed = sum(
        1 for r in results if (r.get("metrics") or {}).get("verification_passed") is True
    )
    rollback_any = sum(1 for r in results if (r.get("metrics") or {}).get("rollback_count"))
    return {
        "total": total,
        "success": success,
        "completed_no_pass": completed_no_pass,
        "errors": errors,
        "timeouts": timeouts,
        "verification_passed": ver_passed,
        "runs_with_rollback": rollback_any,
        "plan_phase_errors": plan_err,
        "execute_phase_errors": exec_err,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", type=Path, default=ROOT / "test_paper")
    ap.add_argument("--runs", type=Path, default=ROOT / "runs")
    ap.add_argument("--label-round", default="r1")
    ap.add_argument("--per-paper-timeout", type=int, default=2700)  # 45 min
    ap.add_argument("--start", type=int, default=0)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--source-type", default="auto")
    ap.add_argument("--only", default="", help="substring filter on paper name")
    args = ap.parse_args()

    papers = list_papers(args.dir)
    if args.only:
        papers = [p for p in papers if args.only.lower() in p.name.lower()]
    if args.start:
        papers = papers[args.start :]
    if args.limit:
        papers = papers[: args.limit]

    print(f"[bench] running {len(papers)} paper(s)", flush=True)
    results: list[dict] = []
    for i, paper in enumerate(papers, 1):
        print(f"[bench] ({i}/{len(papers)}) {paper.name} ...", flush=True)
        r = run_paper_subprocess(
            paper,
            args.runs,
            args.label_round,
            args.source_type,
            args.per_paper_timeout,
        )
        print(
            f"[bench]  -> status={r.get('status')} rc={r.get('rc')} "
            f"elapsed={r.get('elapsed_s')}s timed_out={r.get('timed_out')}",
            flush=True,
        )
        if r.get("error"):
            print(f"[bench]     error: {r['error'][:200]}", flush=True)
        results.append(r)

    report = {
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "label_round": args.label_round,
        "per_paper_timeout_s": args.per_paper_timeout,
        "results": results,
        "summary": aggregate(results),
    }
    out = args.runs / f"bench_report.{args.label_round}.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"[bench] summary {json.dumps(report['summary'], ensure_ascii=False)}", flush=True)
    print(f"[bench] report -> {out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
