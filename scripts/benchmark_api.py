"""Bounded real-HTTP ingress benchmark; dedicated test schema, no model calls."""

import argparse
import asyncio
import json
import math
import os
import platform
import secrets
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx
from alembic import command
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url

from spec2repo.storage.database import make_engine
from spec2repo.storage.models import IdempotencyKey, OutboxEvent, Run, RunEvent

ROOT = Path(__file__).resolve().parents[1]


def percentile(values, fraction):
    return sorted(values)[max(0, math.ceil(len(values) * fraction) - 1)]


async def exercise(base, key, payload, concurrencies):
    results = []
    connections = max(64, *concurrencies)
    limits = httpx.Limits(max_connections=connections, max_keepalive_connections=connections)
    async with httpx.AsyncClient(
        base_url=base, headers={"X-API-Key": key}, limits=limits,
        timeout=15, trust_env=False,
    ) as client:
        for _ in range(100):
            response = await client.post(
                "/api/runs", json=payload, headers={"Idempotency-Key": uuid4().hex}
            )
            response.raise_for_status()
        for repeat in range(1, 4):
            for concurrency in concurrencies:
                samples, statuses, errors, ids = [], Counter(), Counter(), []
                started = time.perf_counter()
                deadline = started + 10

                async def sender(
                    deadline=deadline, statuses=statuses, ids=ids, errors=errors, samples=samples,
                ):
                    while time.perf_counter() < deadline:
                        before = time.perf_counter()
                        try:
                            response = await client.post(
                                "/api/runs", json=payload,
                                headers={"Idempotency-Key": uuid4().hex},
                            )
                            statuses[str(response.status_code)] += 1
                            if response.status_code == 202:
                                ids.append(response.json()["id"])
                        except httpx.HTTPError as exc:
                            errors[type(exc).__name__] += 1
                        samples.append((time.perf_counter() - before) * 1000)

                await asyncio.gather(*(sender() for _ in range(concurrency)))
                elapsed = time.perf_counter() - started
                row = {
                    "repeat": repeat, "concurrency": concurrency,
                    "elapsed_seconds": elapsed, "requests": len(samples),
                    "successful_requests": statuses["202"],
                    "success_rps": statuses["202"] / elapsed,
                    "status_counts": dict(statuses), "transport_errors": dict(errors),
                    "error_rate": 1 - statuses["202"] / len(samples),
                    "p50_ms": percentile(samples, .50),
                    "p95_ms": percentile(samples, .95),
                    "p99_ms": percentile(samples, .99),
                    "unique_response_ids": len(set(ids)),
                    "latencies_ms": samples,
                }
                results.append(row)
                print(json.dumps({k: v for k, v in row.items() if k != "latencies_ms"}),
                      flush=True)
        duplicate_key = "duplicate-" + uuid4().hex
        responses = await asyncio.gather(*(
            client.post("/api/runs", json=payload,
                        headers={"Idempotency-Key": duplicate_key}) for _ in range(64)
        ))
        duplicate = {
            "requests": 64,
            "status_counts": dict(Counter(str(r.status_code) for r in responses)),
            "unique_run_ids": len({r.json().get("id") for r in responses}),
            "replayed": sum(r.headers.get("Idempotency-Replayed") == "true" for r in responses),
            "key": duplicate_key,
        }
    return results, duplicate


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--concurrency", type=int, nargs="+", default=[8, 32, 64])
    args = parser.parse_args()
    if any(c < 1 or c > 128 for c in args.concurrency):
        parser.error("concurrency must be between 1 and 128")
    os.chdir(ROOT)
    url = next(line.split("=", 1)[1] for line in
               (ROOT / ".env.test").read_text().splitlines()
               if line.startswith("SPEC2REPO_TEST_MYSQL_URL="))
    parsed = make_url(url)
    if parsed.database != "spec2repo_test" or parsed.host not in {"localhost", "127.0.0.1"}:
        raise SystemExit("Only the local spec2repo_test database is allowed")
    engine = make_engine(url)
    config = Config(str(ROOT / "alembic.ini"))
    with engine.begin() as connection:
        config.attributes["connection"] = connection
        command.upgrade(config, "head")
    owner = "benchmark-" + uuid4().hex
    key = secrets.token_urlsafe(32)
    environment = os.environ.copy()
    environment.update(SPEC2REPO_DATABASE_URL=url,
                       SPEC2REPO_API_KEYS=json.dumps({owner: key}))
    with socket.socket() as port_socket:
        port_socket.bind(("127.0.0.1", 0))
        port = port_socket.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    log_dir = ROOT / ".runtime" / "benchmarks"
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f"{owner}.log").open("w") as log:
        process = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "spec2repo.api.app:create_app", "--factory",
             "--host", "127.0.0.1", "--port", str(port), "--workers", "1",
             "--no-access-log"], env=environment, stdout=log, stderr=log,
        )
        try:
            with httpx.Client(trust_env=False, timeout=1) as client:
                for _ in range(100):
                    try:
                        if client.get(base + "/openapi.json").status_code == 200:
                            break
                    except httpx.HTTPError:
                        pass
                    if process.poll() is not None:
                        raise RuntimeError("API exited; inspect local benchmark log")
                    time.sleep(.1)
                else:
                    raise RuntimeError("API readiness timeout")
            payload = json.loads((ROOT / "examples/notes_api.spec.json").read_text("utf-8"))
            results, duplicate = asyncio.run(exercise(base, key, payload, args.concurrency))
            with engine.connect() as connection:
                counts = {model.__tablename__: connection.scalar(
                    select(func.count()).select_from(model).where(
                        model.owner_id == owner if model in (Run, IdempotencyKey)
                        else model.run_id.in_(select(Run.id).where(Run.owner_id == owner))
                    )) for model in (Run, IdempotencyKey, OutboxEvent, RunEvent)}
                duplicate["database_keys"] = connection.scalar(select(func.count())
                    .select_from(IdempotencyKey).where(
                        IdempotencyKey.owner_id == owner,
                        IdempotencyKey.key == duplicate.pop("key")))
                database_version = connection.scalar(text("SELECT VERSION()"))
                durability = dict(connection.execute(text(
                    "SELECT @@innodb_flush_log_at_trx_commit AS flush_at_commit, "
                    "@@sync_binlog AS sync_binlog"
                )).mappings().one())
            expected = 100 + sum(r["successful_requests"] for r in results) + 1
            report = {
                "timestamp_utc": datetime.now(UTC).isoformat(), "owner": owner,
                "scope": "POST /api/runs: HTTP/auth/validation/MySQL transaction/outbox; "
                         "no relay, Redis consumption, model or sandbox execution",
                "environment": {"platform": platform.platform(),
                    "python": platform.python_version(), "logical_cpus": os.cpu_count(),
                    "mysql": database_version, "durability_settings": durability,
                    "mysql_storage": "Docker compose.test.yaml tmpfs /var/lib/mysql",
                    "api_workers": 1, "db_pool_size": 5, "db_max_overflow": 5,
                    "load_generator": "same-host httpx asyncio closed-loop, no retries",
                    "warmup_requests": 100, "round_target_seconds": 10},
                "rounds": results, "idempotency_burst": duplicate,
                "database_counts": counts, "expected_rows_per_table": expected,
                "counts_match": all(v == expected for v in counts.values()),
            }
            target = ROOT / "docs/experiments" / f"{owner}.json"
            target.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(f"Report: {target}", flush=True)
            if (
                not report["counts_match"] or duplicate["unique_run_ids"] != 1
                or duplicate["database_keys"] != 1 or duplicate["replayed"] != 63
                or duplicate["status_counts"] != {"202": 64}
                or any(r["error_rate"] for r in results)
            ):
                raise RuntimeError("Benchmark correctness check failed")
        finally:
            process.terminate()
            process.wait(timeout=15)
            engine.dispose()


if __name__ == "__main__":
    main()
