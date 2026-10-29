"""Independently verify an existing real-generation snapshot, without any model request.

Import provenance is explicit: this is not a new generation run or a repeated sample.
The original budget is retained. A failed verdict may schedule repair, but this script
does not call a model or execute that repair.
"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from run_agent_experiment import read_env
from sqlalchemy.engine import make_url

from spec2repo.api.app import create_app
from spec2repo.config import Settings
from spec2repo.domain.agent import ApprovalDecision, ImplementationPlan
from spec2repo.domain.spec import CoreSpec
from spec2repo.runtime.agent_worker import AgentWorker
from spec2repo.runtime.delivery import Relay
from spec2repo.runtime.streams import Streams, redis_client
from spec2repo.sandbox.docker import DockerSandbox
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.database import make_engine
from spec2repo.storage.execution import ExecutionRepository
from spec2repo.storage.models import OutboxEvent
from spec2repo.storage.runs import RunRepository
from spec2repo.storage.verification import VerificationRepository
from spec2repo.storage.workspaces import Workspaces

ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, default=ROOT / ".runtime/live-agent")
    parser.add_argument("--spec", type=Path, default=ROOT / "examples/notes_api.spec.json")
    args = parser.parse_args()
    original = json.loads(args.report.read_text(encoding="utf-8"))
    spec = CoreSpec.model_validate_json(args.spec.read_text(encoding="utf-8"))
    source_spec = original.get("spec")
    if source_spec and CoreSpec.model_validate(source_spec).fingerprint() != spec.fingerprint():
        raise ValueError("Original report specification mismatch")
    spaces = Workspaces(args.workspace_root)
    spaces.verify(original["snapshot_hash"])
    config = read_env(ROOT / ".env.test")
    url = config["SPEC2REPO_TEST_MYSQL_URL"]
    if make_url(url).database != "spec2repo_test":
        raise ValueError("Only isolated test database is allowed")
    engine = make_engine(url)
    migration = Config(str(ROOT / "alembic.ini"))
    with engine.begin() as connection:
        migration.attributes["connection"] = connection
        command.upgrade(migration, "head")
    client = redis_client("redis://127.0.0.1:16380/15")
    streams = Streams(client, stream="spec2repo:test:acceptance:" + uuid4().hex, group="execution-v1")
    docker = DockerSandbox(spaces)
    agent, verification = AgentRepository(engine), VerificationRepository(engine)
    settings = Settings(url, {"experiment": uuid4().hex})
    api = TestClient(create_app(settings, engine=engine, workspace_root=spaces.root))
    run_id, started = None, time.monotonic()

    def claim(run_id):
        with engine.connect() as connection:
            row = connection.execute(OutboxEvent.__table__.select().where(
                OutboxEvent.run_id == run_id
            ).order_by(OutboxEvent.created_at.desc())).mappings().first()
        outcome, lease = ExecutionRepository(engine).claim(
            row["id"], run_id, row["payload"]["version"], row["payload"]["generation"], "artifact-import"
        )
        assert outcome == "claimed"
        return lease

    class NoModel:
        def complete(self, *args, **kwargs):
            raise AssertionError("This experiment must not call a model")

    try:
        run = RunRepository(engine).create("experiment", uuid4().hex, spec)[0]
        run_id = run["id"]
        lease = claim(run_id)
        plan = ImplementationPlan.model_validate({"summary": "Import historical generation for acceptance",
            "tasks": [{"id": "T1", "description": "Verify the existing immutable source artifact",
                       "requirement_ids": [r.id for r in spec.requirements],
                       "acceptance_ids": [a.id for a in spec.acceptance_criteria], "files": ["app.py"]}]})
        budget = original["budget"]
        for _ in range(budget["calls"]):
            agent.reserve(lease, 1, 1)
        with agent.sessions.begin() as session:
            current, _ = agent.execution.guard(session, lease)
            state = agent.state(session, current)
            state.reserved_tokens = budget["reserved_tokens"]
            state.reserved_seconds = budget["reserved_seconds"]
        agent.record(lease, "experiment.artifact_imported", {
            "source_run_id": original["run_id"], "source_snapshot_hash": original["snapshot_hash"],
            "source_model": original["model"],
        })
        agent.propose(lease, plan)
        agent.decide("experiment", run_id, ApprovalDecision(
            revision=1, spec_hash=spec.fingerprint(), plan_hash=plan.fingerprint(spec.fingerprint()),
            approve=True,
        ))
        agent.finish(claim(run_id), original["snapshot_hash"])
        verification.schedule(run_id=run_id)
        relay = Relay(engine, streams)
        while relay.publish_one(run_id=run_id):
            pass
        worker = AgentWorker(engine, streams, "acceptance-experiment", spaces, docker, NoModel())
        for _ in range(8):
            worker.tick(idle_ms=1)
            if verification.reports("experiment", run_id):
                break
        rows = verification.reports("experiment", run_id)
        review = agent.review("experiment", run_id)
        artifact = None
        if review["run"]["status"] == "SUCCEEDED":
            artifact = verification.artifact("experiment", run_id)
            response = api.get(f"/api/runs/{run_id}/artifacts/repository.zip",
                               headers={"X-API-Key": settings.api_keys["experiment"]})
            assert response.status_code == 200
        report = {"run_id": run_id, "source_run_id": original["run_id"],
                  "source_report": args.report.name, "model": original["model"],
                  "new_model_calls": 0, "source_budget": budget, "status": review["run"]["status"],
                  "elapsed_seconds": round(time.monotonic() - started, 2),
                  "verifications": rows, "artifact": artifact,
                  "note": "Independent acceptance of one historical real-model artifact; not a new sample."}
        output = ROOT / "docs/experiments" / f"phase06-{run_id}.json"
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps({"status": report["status"], "report": str(output),
                          "checks": len(rows[-1]["checks"]) if rows else 0}, ensure_ascii=True))
        return 0 if report["status"] == "SUCCEEDED" else 1
    finally:
        if run_id:
            for generation in range(1, 5):
                docker.remove(run_id, generation)
        client.delete(streams.stream, streams.dead)
        client.close()
        api.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
