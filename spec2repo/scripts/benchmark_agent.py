"""Opt-in real-model batches through shared Redis workers and independent verification.

Only example specs, generated code and tool feedback go to the configured provider.
Synthetic experiment-owner approval is automatic. No production data is consumed.
"""

import argparse
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from run_agent_experiment import read_env
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from spec2repo.api.app import create_app
from spec2repo.config import Settings
from spec2repo.harness.model import ChatModel
from spec2repo.runtime.agent_worker import AgentWorker
from spec2repo.runtime.delivery import Relay
from spec2repo.runtime.streams import Streams, redis_client
from spec2repo.sandbox.docker import DockerSandbox
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.database import make_engine
from spec2repo.storage.models import Run, RunEvent
from spec2repo.storage.verification import VerificationRepository
from spec2repo.storage.workspaces import Workspaces

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"SUCCEEDED", "FAILED", "CANCELLED", "WAITING_CLARIFICATION"}


class LimitedSandbox(DockerSandbox):
    """Experiment-only admission cap; verification keeps its own container lifecycle."""

    def __init__(self, spaces):
        super().__init__(spaces)
        self.slots = threading.BoundedSemaphore(16)
        self.counter_lock = threading.Lock()
        self.active = self.peak = 0

    def run(self, *args, authorized, stop, **kwargs):
        while not self.slots.acquire(timeout=.2):
            authorized()
        try:
            authorized()
            with self.counter_lock:
                self.active += 1
                self.peak = max(self.peak, self.active)
            try:
                return super().run(*args, authorized=authorized, stop=stop, **kwargs)
            finally:
                with self.counter_lock:
                    self.active -= 1
        finally:
            self.slots.release()


def batch(engine, model_config, size, batch_id, *, profile="guarded", case="notes"):
    owner = "bench-" + batch_id
    settings = Settings(str(engine.url.render_as_string(hide_password=False)), {owner: uuid4().hex})
    api = TestClient(create_app(settings, engine=engine))
    headers = {"X-API-Key": settings.api_keys[owner]}
    redis = redis_client("redis://127.0.0.1:16380/15")
    streams = Streams(redis, stream="spec2repo:test:agent-benchmark:" + batch_id,
                      group="execution-v1")
    spaces = Workspaces(ROOT / ".runtime" / "agent-benchmark" / batch_id)
    sandbox = LimitedSandbox(spaces)
    relay = Relay(engine, streams)
    verification = VerificationRepository(engine)
    halt = threading.Event()
    pool = ThreadPoolExecutor(max_workers=size)
    futures, starts, completed, last_status, aborted = [], {}, {}, {}, None

    def consume(index):
        worker = AgentWorker(engine, streams, f"bench-worker-{index}", spaces, sandbox,
                             ChatModel(*model_config), profile=profile)
        while not halt.is_set():
            worker.tick()
            halt.wait(.05)

    started = time.monotonic()
    try:
        if case not in {"notes", "task"}:
            raise ValueError("Unknown frozen case")
        spec = json.loads((ROOT / f"examples/{case}_api.spec.json").read_text("utf-8"))
        for _ in range(size):
            before = time.monotonic()
            response = api.post("/api/runs", json=spec,
                                headers={**headers, "Idempotency-Key": uuid4().hex})
            response.raise_for_status()
            starts[response.json()["id"]] = before
        futures = [pool.submit(consume, i) for i in range(size)]
        while len(completed) < size:
            if time.monotonic() - started > 900:
                aborted = "batch_deadline_900_seconds"
                break
            for future in futures:
                if future.done():
                    future.result()  # Surface worker infrastructure errors.
            for run_id, run_started in starts.items():
                if run_id in completed:
                    continue
                base = f"/api/runs/{run_id}"
                response = api.get(base + "/review", headers=headers)
                response.raise_for_status()
                review = response.json()
                status = review["run"]["status"]
                if last_status.get(run_id) != status:
                    print(json.dumps({"batch": batch_id, "workers": size,
                                      "run_id": run_id, "status": status}), flush=True)
                    last_status[run_id] = status
                if status in TERMINAL:
                    completed[run_id] = time.monotonic() - run_started
                    continue
                if status == "WAITING_APPROVAL":
                    plan = review["plans"][-1]
                    decision = {k: plan[k] for k in ("revision", "spec_hash", "plan_hash")}
                    response = api.post(base + "/approval", headers=headers,
                                        json={**decision, "approve": True})
                    response.raise_for_status()
                if status == "WAITING_VERIFICATION":
                    verification.schedule(run_id=run_id)
                while relay.publish_one(run_id=run_id):
                    pass
            time.sleep(.2)
    except Exception as exc:  # noqa: BLE001 -- persist failures without exposing credentials
        aborted = type(exc).__name__  # Never print credential-bearing exception strings.
    finally:
        elapsed = time.monotonic() - started
        halt.set()
        if aborted:
            for run_id in starts:
                api.post(f"/api/runs/{run_id}/cancel", headers=headers)
        pool.shutdown(wait=True)
        cleanup_errors = []
        with Session(engine) as session:
            generations = [(r.id, r.generation) for r in session.scalars(
                select(Run).where(Run.id.in_(starts))) ]
        for run_id, generation in generations:
            try:
                for gen in range(1, generation + 1):
                    sandbox.remove(run_id, gen)
            except Exception as exc:  # noqa: BLE001 -- report each cleanup failure
                cleanup_errors.append(type(exc).__name__)
        rows = []
        for run_id in starts:
            review = api.get(f"/api/runs/{run_id}/review", headers=headers).json()
            with Session(engine) as session:
                events = [{"type": e.event_type, "payload": e.payload} for e in
                          session.scalars(select(RunEvent).where(RunEvent.run_id == run_id)
                                          .order_by(RunEvent.id))]
            reports = verification.reports(owner, run_id)
            accepted = review["run"]["status"] == "SUCCEEDED" and bool(reports) and (
                reports[-1]["outcome"] == "PASSED")
            rows.append({"run_id": run_id, "status": review["run"]["status"],
                         "accepted": accepted, "elapsed_seconds": completed.get(run_id),
                         "budget": review["budget"], "events": events,
                         "snapshot_hash": review["snapshot_hash"], "verifications": reports})
        successes = sum(r["accepted"] for r in rows)
        result = {"batch_id": batch_id, "profile": profile, "case": case,
                  "concurrency": size, "tasks": size,
                  "elapsed_seconds": elapsed, "successes": successes,
                  "success_rate": successes / size,
                  "successful_tasks_per_minute": successes * 60 / elapsed,
                  "aborted": aborted, "cleanup_errors": cleanup_errors,
                  "peak_tool_slots": sandbox.peak,
                  "image_id": sandbox.image, "runs": rows}
        redis.delete(streams.stream, streams.dead)
        redis.close()
        api.close()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-env", type=Path, required=True)
    parser.add_argument("--base-url")
    parser.add_argument("--profile", choices=["baseline", "memory", "guarded"], default="guarded")
    parser.add_argument("--case", choices=["notes", "task"], default="notes")
    parser.add_argument("--concurrency", nargs="+", type=int, default=[1, 2, 4])
    parser.add_argument("--budget-multiplier", type=int, choices=[1, 2], default=1)
    parser.add_argument("--max-output-tokens", type=int, choices=[4096, 8192], default=4096)
    args = parser.parse_args()
    if any(n < 1 or n > 16 for n in args.concurrency) or sum(args.concurrency) > 21:
        parser.error("At most 21 tasks, maximum 16 concurrent workers")
    AgentRepository.MAX_CALLS *= args.budget_multiplier
    AgentRepository.MAX_RESERVED_TOKENS *= args.budget_multiplier
    AgentRepository.MAX_RESERVED_SECONDS *= args.budget_multiplier
    ChatModel.MAX_OUTPUT = args.max_output_tokens
    os.chdir(ROOT)
    provider = read_env(args.model_env.resolve())
    model_config = (
        args.base_url or provider.get("SPEC2REPO_MODEL_BASE_URL") or provider["OPENAI_BASE_URL"],
        provider.get("SPEC2REPO_MODEL_API_KEY") or provider["OPENAI_API_KEY"],
        provider.get("SPEC2REPO_MODEL") or provider["MODEL_NAME"],
    )
    model = ChatModel(*model_config)  # Validate locally, no model call.
    url = read_env(ROOT / ".env.test")["SPEC2REPO_TEST_MYSQL_URL"]
    parsed = make_url(url)
    if parsed.database != "spec2repo_test" or parsed.host not in {"127.0.0.1", "localhost"}:
        raise ValueError("Only the local dedicated test database is allowed")
    engine = make_engine(url)
    migration = Config(str(ROOT / "alembic.ini"))
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        command.upgrade(migration, "head")
    experiment = uuid4().hex
    output = ROOT / "docs/experiments" / f"agent-benchmark-{experiment}.json"
    report = {
        "timestamp_utc": datetime.now(UTC).isoformat(), "model": model.model,
        "provider_host": model.provider_host,
        "scope": "real model, shared Redis stream/consumer group, concurrent worker threads, "
                 "Docker tools, independent acceptance and bounded repairs; "
                 "in-process TestClient submission, synthetic automatic plan approval",
        "spec": f"{args.case}_api.spec.json", "profile": args.profile, "cost": None,
        "max_output_tokens": ChatModel.MAX_OUTPUT,
        "tool_container_slots": 16,
        "tool_slot_scope": "workspace tools only; verification containers are separate",
        "budget_per_task": {"calls": AgentRepository.MAX_CALLS,
                            "reserved_tokens": AgentRepository.MAX_RESERVED_TOKENS,
                            "reserved_seconds": AgentRepository.MAX_RESERVED_SECONDS},
        "batches": [],
    }
    try:
        for concurrency in args.concurrency:
            result = batch(engine, model_config, concurrency, uuid4().hex,
                           profile=args.profile, case=args.case)
            report["batches"].append(result)
            output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({k: v for k, v in result.items() if k != "runs"}), flush=True)
            # Task/model failures count as outcomes; stop on orchestration/cleanup failure.
            if result["aborted"] or result["cleanup_errors"]:
                break
    finally:
        engine.dispose()
    print("Report: " + str(output.relative_to(ROOT)), flush=True)


if __name__ == "__main__":
    main()
