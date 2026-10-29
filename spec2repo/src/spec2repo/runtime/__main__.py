"""Run relay, leased fixture execution, simulation or recovery services."""

import argparse
import os
import time

from redis.exceptions import RedisError
from sqlalchemy.exc import SQLAlchemyError

from spec2repo.harness.model import ChatModel
from spec2repo.runtime.agent_worker import AgentWorker
from spec2repo.runtime.delivery import Relay, SimulationWorker
from spec2repo.runtime.executor import ExecutionWorker, recover_and_cleanup
from spec2repo.runtime.streams import Streams, redis_client
from spec2repo.sandbox.docker import DockerSandbox, SandboxError
from spec2repo.storage.database import make_engine
from spec2repo.storage.execution import ExecutionRepository
from spec2repo.storage.workspaces import Workspaces


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=["relay", "worker", "recovery"])
    parser.add_argument("--simulate", action="store_true")
    parser.add_argument("--execute-fixture", action="store_true")
    parser.add_argument("--agent", action="store_true")
    parser.add_argument("--lease-seconds", type=int, default=60)
    parser.add_argument("--heartbeat-seconds", type=float, default=10)
    parser.add_argument("--fixture-delay", type=float, default=0)
    parser.add_argument("--consumer", default=f"worker-{os.getpid()}")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--idle-ms", type=int, default=30000)
    args = parser.parse_args()
    if args.role == "worker" and sum([args.simulate, args.execute_fixture, args.agent]) != 1:
        parser.error("Choose exactly one of --simulate, --execute-fixture or --agent")
    if args.idle_ms < 1:
        parser.error("--idle-ms must be positive")
    engine = make_engine(os.environ["SPEC2REPO_DATABASE_URL"])
    client = redis_client(os.environ["SPEC2REPO_REDIS_URL"])
    streams = Streams(
        client,
        stream=os.environ.get("SPEC2REPO_STREAM", "spec2repo:dispatch"),
        group="execution-v1" if args.execute_fixture or args.agent else "dispatch-simulation",
    )
    relay = Relay(engine, streams)
    worker = SimulationWorker(engine, streams, args.consumer)
    execution = ExecutionRepository(engine, lease_seconds=args.lease_seconds)
    sandbox = None
    if args.execute_fixture or args.agent or args.role == "recovery":
        workspaces = Workspaces(os.environ.get("SPEC2REPO_WORKSPACE_ROOT", ".runtime"))
        sandbox = DockerSandbox(
            workspaces, image=os.environ.get("SPEC2REPO_RUNTIME_IMAGE", "spec2repo-runtime:phase4")
        )
    if args.execute_fixture:
        worker = ExecutionWorker(
            engine,
            streams,
            args.consumer,
            workspaces,
            sandbox,
            lease_seconds=args.lease_seconds,
            heartbeat_seconds=args.heartbeat_seconds,
            fixture_delay=args.fixture_delay,
        )
    if args.agent:
        worker = AgentWorker(
            engine, streams, args.consumer, workspaces, sandbox, ChatModel.from_env(),
            lease_seconds=args.lease_seconds, heartbeat_seconds=args.heartbeat_seconds,
        )
    next_reconcile = 0.0
    failures = 0
    try:
        while True:
            try:
                if args.role == "recovery":
                    work = recover_and_cleanup(execution, sandbox)
                    if not args.once:
                        time.sleep(1)
                elif args.role == "relay":
                    if time.monotonic() >= next_reconcile:
                        if args.simulate:
                            relay.reconcile()
                        else:
                            execution.reconcile()
                        next_reconcile = time.monotonic() + 10
                    work = relay.publish_one()
                else:
                    work = worker.tick(idle_ms=args.idle_ms)
                failures = 0
                if args.once:
                    return 0
                if not work:
                    time.sleep(0.2)
            except (RedisError, SQLAlchemyError, SandboxError) as exc:
                # Keep connection URLs and SQL parameters out of console output.
                print(f"dispatch retry: {type(exc).__name__}", flush=True)
                if args.once:
                    return 1
                failures += 1
                time.sleep(min(5, 0.2 * (2 ** min(failures, 5))))
    except KeyboardInterrupt:
        return 0
    finally:
        client.close()
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
