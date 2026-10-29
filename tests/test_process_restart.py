"""Real HTTP server processes share only durable MySQL state."""

import json
import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from uuid import uuid4

import httpx
import pytest


@contextmanager
def server(env, headers):
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "spec2repo.api.app:create_app",
            "--factory",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
        ],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}", headers=headers, timeout=2
        ) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                assert process.poll() is None, "API process exited during startup"
                try:
                    if client.get("/api/runs").status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("API did not become ready in 15 seconds")
            yield client
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


@pytest.mark.mysql
def test_task_survives_api_process_restart(mysql_engine, api_settings, spec_payload):
    # mysql_engine validates the dedicated schema and applies migrations.
    env = os.environ.copy()
    env["SPEC2REPO_DATABASE_URL"] = os.environ["SPEC2REPO_TEST_MYSQL_URL"]
    env["SPEC2REPO_API_KEYS"] = json.dumps(api_settings.api_keys)
    headers = {"X-API-Key": api_settings.api_keys["alice"], "Idempotency-Key": str(uuid4())}
    with server(env, headers) as client:
        created = client.post("/api/runs", json=spec_payload)
        assert created.status_code == 202
        run = created.json()
    with server(env, headers) as client:
        fetched = client.get(f"/api/runs/{run['id']}")
        assert fetched.status_code == 200
        assert fetched.json() == run
        replayed = client.post("/api/runs", json=spec_payload)
        assert replayed.status_code == 202
        assert replayed.json()["id"] == run["id"]
        assert replayed.headers["Idempotency-Replayed"] == "true"
