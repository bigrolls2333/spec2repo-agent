"""Plan a frozen profile comparison by default. --execute explicitly enables paid calls."""

import argparse
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from benchmark_agent import ROOT, batch
from run_agent_experiment import read_env
from sqlalchemy.engine import make_url

from spec2repo.harness.evaluation import summarize
from spec2repo.harness.model import ChatModel
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.database import make_engine


def code_hash():
    digest = hashlib.sha256()
    paths = [*(ROOT / "src/spec2repo").rglob("*.py"), *(ROOT / "scripts").glob("*.py")]
    for path in sorted(paths):
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def plan(repeats, selected_cases=("notes", "task"), profiles=("baseline", "memory", "guarded")):
    profiles = list(profiles)
    cases = [{"case": case, "spec_sha256": hashlib.sha256(
        (ROOT / f"examples/{case}_api.spec.json").read_bytes()).hexdigest()}
        for case in selected_cases]
    # Rotate profile order across case/repeat blocks to reduce fixed-order bias.
    trials = []
    for repeat in range(repeats):
        for index, case in enumerate(cases):
            offset = (repeat + index) % len(profiles)
            trials.extend({**case, "repeat": repeat + 1, "profile": profile}
                          for profile in profiles[offset:] + profiles[:offset])
    return {"version": "spec2repo-eval-v1", "code_hash": code_hash(), "trials": trials,
            "concurrency": 1, "repeats": repeats,
            "budget_per_task": {"calls": AgentRepository.MAX_CALLS,
                                "reserved_tokens": AgentRepository.MAX_RESERVED_TOKENS,
                                "reserved_seconds": AgentRepository.MAX_RESERVED_SECONDS},
            "max_output_tokens": ChatModel.MAX_OUTPUT,
            "limitations": ["Two public fixed cases; not a held-out generalization benchmark",
                            "Finite repeated trials, not sustained throughput"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--model-env", type=Path)
    parser.add_argument("--repeats", type=int, choices=[1, 2, 3], default=2)
    parser.add_argument("--cases", nargs="+", choices=["notes", "task"], default=["notes", "task"])
    parser.add_argument("--profiles", nargs="+", choices=["baseline", "memory", "guarded"],
                        default=["baseline", "memory", "guarded"])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if len(set(args.cases)) != len(args.cases) or len(set(args.profiles)) != len(args.profiles):
        parser.error("Cases and profiles must be unique")
    report = {"plan": plan(args.repeats, args.cases, args.profiles), "executed": False, "results": []}
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        parser.error("Output exists; use a new path to preserve experiment evidence")
    if args.execute and not args.model_env:
        parser.error("--execute requires --model-env")
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if not args.execute:
        print(f"Plan only: {len(report['plan']['trials'])} tasks; no model calls. {output}")
        return
    provider = read_env(args.model_env.resolve())
    config = (provider.get("SPEC2REPO_MODEL_BASE_URL") or provider["OPENAI_BASE_URL"],
              provider.get("SPEC2REPO_MODEL_API_KEY") or provider["OPENAI_API_KEY"],
              provider.get("SPEC2REPO_MODEL") or provider["MODEL_NAME"])
    model = ChatModel(*config)
    report.update(model=model.model, provider_host=model.provider_host, executed=True)
    url = read_env(ROOT / ".env.test")["SPEC2REPO_TEST_MYSQL_URL"]
    parsed = make_url(url)
    if parsed.database != "spec2repo_test" or parsed.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Only dedicated local test database is allowed")
    os.chdir(ROOT)
    engine = make_engine(url)
    try:
        migration = Config(str(ROOT / "alembic.ini"))
        with engine.begin() as connection:
            migration.attributes["connection"] = connection
            command.upgrade(migration, "head")
        for trial in report["plan"]["trials"]:
            actual = hashlib.sha256((ROOT / f"examples/{trial['case']}_api.spec.json").read_bytes()).hexdigest()
            if actual != trial["spec_sha256"] or code_hash() != report["plan"]["code_hash"]:
                raise ValueError("Specification or harness changed during evaluation")
            result = batch(engine, config, 1, uuid4().hex,
                           profile=trial["profile"], case=trial["case"])
            report["results"].append({"trial": trial, "batch": result,
                                      "metrics": summarize(result["runs"])})
            report["by_profile"] = {p: summarize([
                run for r in report["results"] if r["trial"]["profile"] == p
                for run in r["batch"]["runs"]
            ]) for p in args.profiles}
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            if result["aborted"] or result["cleanup_errors"]:
                break
    finally:
        report["complete"] = len(report["results"]) == len(report["plan"]["trials"])
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        engine.dispose()
    print(f"Report: {output}")


if __name__ == "__main__":
    main()
