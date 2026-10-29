"""Opt-in real model experiment, isolated test DB, explicit synthetic owner approval.

Only the public example specification and generated workspace/tool feedback are sent
to the configured provider. Credentials are read into the host process, never copied.
"""

import argparse
import json
import time
from pathlib import Path
from uuid import uuid4

from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
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
from spec2repo.storage.database import make_engine
from spec2repo.storage.models import RunEvent
from spec2repo.storage.verification import VerificationRepository
from spec2repo.storage.workspaces import Workspaces

ROOT = Path(__file__).resolve().parents[1]


def read_env(path):
    values = {}
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-env", type=Path, required=True)
    parser.add_argument("--base-url", help="Explicit provider endpoint override; never edits .env")
    parser.add_argument("--verify", action="store_true", help="Run independent acceptance and bounded repairs")
    parser.add_argument("--spec", choices=["notes", "task"], default="notes")
    args = parser.parse_args()
    provider = read_env(args.model_env)
    model = ChatModel(
        args.base_url or provider.get("SPEC2REPO_MODEL_BASE_URL") or provider["OPENAI_BASE_URL"],
        provider.get("SPEC2REPO_MODEL_API_KEY") or provider["OPENAI_API_KEY"],
        provider.get("SPEC2REPO_MODEL") or provider["MODEL_NAME"],
    )
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
    streams = Streams(
        client, stream="spec2repo:test:live-agent:" + uuid4().hex, group="execution-v1"
    )
    spaces = Workspaces(ROOT / ".runtime" / "live-agent")
    docker = DockerSandbox(spaces)
    settings = Settings(url, {"experiment": uuid4().hex})
    api = TestClient(create_app(settings, engine=engine))
    headers = {"X-API-Key": settings.api_keys["experiment"], "Idempotency-Key": uuid4().hex}
    started = time.monotonic()
    run_id = None
    try:
        spec = json.loads((ROOT / f"examples/{args.spec}_api.spec.json").read_text(encoding="utf-8"))
        result = api.post("/api/runs", json=spec, headers=headers)
        result.raise_for_status()
        run_id = result.json()["id"]
        relay = Relay(engine, streams)
        # Isolate the live model experiment from every other test task.
        while relay.publish_one(run_id=run_id):
            pass
        worker = AgentWorker(engine, streams, "live-agent-experiment", spaces, docker, model)
        base = f"/api/runs/{run_id}"
        while api.get(base, headers=headers).json()["status"] == "QUEUED":
            worker.tick(idle_ms=1)
        verification = VerificationRepository(engine)
        review = api.get(base + "/review", headers=headers).json()
        if review["run"]["status"] == "WAITING_APPROVAL":
            plan = review["plans"][-1]
            approval = {key: plan[key] for key in ["revision", "spec_hash", "plan_hash"]}
            # This is a synthetic experiment owner. Production always uses the owner API.
            approval["approve"] = True
            approved = api.post(base + "/approval", headers=headers, json=approval)
            approved.raise_for_status()
            relay.publish_one(run_id=run_id)
            worker.tick(idle_ms=1)
        if args.verify:
            for _ in range(8):
                status = api.get(base, headers=headers).json()["status"]
                if status not in {"QUEUED", "WAITING_VERIFICATION"}:
                    break
                verification.schedule(run_id=run_id)
                while relay.publish_one(run_id=run_id):
                    pass
                worker.tick(idle_ms=1)
        review = api.get(base + "/review", headers=headers).json()
        with Session(engine) as session:
            events = session.scalars(
                select(RunEvent).where(RunEvent.run_id == run_id).order_by(RunEvent.id)
            ).all()
            trace = [{"type": event.event_type, "payload": event.payload} for event in events]
        report = {
            "model": model.model,
            "run_id": run_id,
            "status": review["run"]["status"],
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "budget": review["budget"],
            "snapshot_hash": review["snapshot_hash"],
            "image_id": docker.image,
            "spec": spec,
            "plans": review["plans"],
            "cost": None,
            "note": "Real model/tool execution; developer tests are not independent acceptance.",
            "events": trace,
            "verifications": verification.reports("experiment", run_id) if args.verify else [],
        }
        stage = "phase06" if args.verify else "phase05"
        output = ROOT / "docs" / "experiments" / f"{stage}-{run_id}.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {
                    k: report[k]
                    for k in ["model", "run_id", "status", "elapsed_seconds", "snapshot_hash"]
                },
                ensure_ascii=True,
            )
        )
        print("Report: " + str(output.relative_to(ROOT)))
        expected = "SUCCEEDED" if args.verify else "WAITING_VERIFICATION"
        return 0 if review["run"]["status"] == expected else 1
    finally:
        if run_id:
            for generation in range(1, 12):
                docker.remove(run_id, generation)
        client.delete(streams.stream, streams.dead)
        client.close()
        api.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
