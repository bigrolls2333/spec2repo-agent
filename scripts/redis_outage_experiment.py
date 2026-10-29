"""Stop only the dedicated test Redis, create via HTTP, recover and consume.

Run after scripts/run_mysql_tests.py has applied migrations. No business tables
are cleared. Compose up is attempted in finally even when assertions fail.
"""

import json
import subprocess
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from spec2repo.api.app import create_app
from spec2repo.config import Settings
from spec2repo.runtime.delivery import Relay, SimulationWorker
from spec2repo.runtime.streams import Streams, redis_client
from spec2repo.storage.database import make_engine
from spec2repo.storage.models import DispatchReceipt, OutboxEvent

root = Path(__file__).resolve().parents[1]
config = dict(
    line.split("=", 1) for line in (root / ".env.test").read_text().splitlines() if "=" in line
)
database_url = config["SPEC2REPO_TEST_MYSQL_URL"]
parsed = make_url(database_url)
if parsed.database != "spec2repo_test" or parsed.host != "127.0.0.1" or parsed.port != 13307:
    raise SystemExit("This experiment only targets the local dedicated test database")
engine = make_engine(database_url)
client = redis_client("redis://127.0.0.1:16380/15")
streams = Streams(client, stream=f"spec2repo:test:outage:{uuid4()}")
relay = Relay(engine, streams)
compose = ["docker", "compose", "--env-file", ".env.test", "-f", "compose.test.yaml"]
key = uuid4().hex
settings = Settings(database_url, {"outage-test": key})
headers = {"X-API-Key": key, "Idempotency-Key": uuid4().hex}


def docker(*args):
    subprocess.run(
        [*compose, *args],
        cwd=root,
        check=True,
        timeout=90,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


try:
    client.ping()
    docker("stop", "redis-test")
    try:
        with TestClient(create_app(settings, engine=engine)) as api:
            spec = json.loads((root / "examples/task_api.spec.json").read_text(encoding="utf-8"))
            response = api.post("/api/runs", json=spec, headers=headers)
            assert response.status_code == 202
            run = response.json()
            assert api.get(f"/api/runs/{run['id']}", headers=headers).status_code == 200
        try:
            relay.publish_one()
        except RedisError:
            pass
        else:
            raise AssertionError("Expected Redis connection failure")
        with Session(engine) as session:
            outbox = session.scalar(select(OutboxEvent).where(OutboxEvent.run_id == run["id"]))
            eid = outbox.id
            assert outbox.published_at is None
        print("Redis stopped: HTTP create/query succeeded; publication remains pending.")
    finally:
        docker("up", "-d", "--wait", "redis-test")
    # Publish all remaining test records; they only produce simulated receipts.
    for _ in range(100):
        if not relay.publish_one():
            break
    worker = SimulationWorker(engine, streams, "outage-recovery")
    for _ in range(100):
        worker.tick()
        with Session(engine) as session:
            if session.get(DispatchReceipt, eid):
                break
    else:
        raise AssertionError("Recovered dispatch was not consumed")
    print("Redis restored: original run_id consumed with one durable simulation receipt.")
finally:
    client.delete(streams.stream, streams.dead)
    client.close()
    engine.dispose()
