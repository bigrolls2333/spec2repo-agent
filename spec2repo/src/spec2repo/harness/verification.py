"""Platform-owned HTTP acceptance; generated code never hosts its own judge."""

import hashlib
import json
import time
from pathlib import Path

from spec2repo.harness import acceptance_runner
from spec2repo.sandbox.docker import SandboxError, SandboxStopped

# Exact CoreSpec fingerprints: no title/id-only matching and no inferred test generation.
SUITES = {
    "522d740b81f061c3dca74d0a6e6bfdf4821d2ac75200bc42e0627e7969a5dfda": "notes",
    "7fb9554252416e05ec8fa5fe215728c3e808e5ed497db8b1621ad747a94c5e35": "task",
}


class Verifier:
    VERSION = "python-api-http-v1"

    def __init__(self, workspaces, sandbox):
        self.spaces, self.docker = workspaces, sandbox
        self.source = Path(acceptance_runner.__file__).read_text(encoding="utf-8")
        # Bind checks, orchestration, registry and image to the evidence identity.
        material = self.source + Path(__file__).read_text(encoding="utf-8") + sandbox.image
        self.suite_hash = hashlib.sha256(material.encode()).hexdigest()

    def args(self, lease, argv, *, judge=False):
        name = self.docker.name(lease.run_id, lease.generation)
        args = [
            "create", "--name", name + ("-verify" if judge else ""),
            "--label", f"spec2repo.run={lease.run_id}",
            "--label", f"spec2repo.generation={lease.generation}",
            "--network", "container:" + name if judge else "none",
            "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--user", "65534:65534", "--memory", "256m", "--memory-swap", "256m",
            "--cpus", "1", "--pids-limit", "64", "--init",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
            "--log-driver", "local", "--log-opt", "max-size=1m",
            "--log-opt", "max-file=1", "--log-opt", "compress=false",
        ]
        if not judge:
            workspace = self.spaces.workspace(lease.run_id, lease.generation)
            if workspace.is_symlink() or "," in str(workspace):
                raise SandboxError("Invalid verification mount")
            args += ["--mount", f"type=bind,source={workspace},target=/workspace",
                     "--workdir", "/workspace",
                     "--env", "NOTES_DB=/workspace/acceptance.sqlite",
                     "--env", "DATABASE_PATH=/workspace/acceptance.sqlite", "--env", "PORT=8000"]
        else:
            # No generated files, shared PID namespace, app stdout or host secrets in the judge.
            args += ["--workdir", "/tmp"]
        return [*args, self.docker.image, *argv]

    def judge(self, lease, case, phase, saved, authorized, stop):
        authorized()
        name = self.docker.name(lease.run_id, lease.generation) + "-verify"
        argv = ["python", "-I", "-c", self.source, case, phase, json.dumps(saved)]
        self.docker.command(self.args(lease, argv, judge=True))
        authorized()
        self.docker.command(["start", name])
        deadline = time.monotonic() + 45
        try:
            while True:
                authorized()
                if stop.is_set():
                    raise SandboxStopped("Verification ownership lost")
                if time.monotonic() >= deadline:
                    raise SandboxError("Verifier deadline exceeded")
                info = self.docker.inspect(lease.run_id, lease.generation, verifier=True)
                if info is None:
                    raise SandboxError("Verifier disappeared")
                if not info["State"]["Running"]:
                    if info["State"]["ExitCode"] != 0:
                        raise SandboxError("Verifier process failed")
                    output = self.docker.command(["logs", "--tail", "1", name])
                    if len(output) > 65536:
                        raise SandboxError("Verifier report exceeds limit")
                    try:
                        return json.loads(output)
                    except ValueError:
                        raise SandboxError("Invalid verifier report") from None
                stop.wait(0.2)
        finally:
            if self.docker.inspect(lease.run_id, lease.generation, verifier=True):
                self.docker.command(["rm", "-f", name])

    def run(self, lease, context, authorized, stop):
        report = {"spec_hash": lease.spec_hash, "snapshot_hash": context["snapshot_hash"],
                  "suite_hash": self.suite_hash, "suite_version": self.VERSION,
                  "image_id": self.docker.image, "outcome": "ERROR", "checks": [], "feedback": []}
        case = SUITES.get(lease.spec_hash)
        if case is None:
            return {**report, "reason": "No frozen suite for this exact specification"}
        workspace = self.spaces.create(lease)
        self.spaces.restore(context["snapshot_hash"], workspace)
        # Acceptance uses its own fresh database, not one shipped by the generated repository.
        database = workspace / "acceptance.sqlite"
        for path in (database, workspace / "acceptance.sqlite-wal", workspace / "acceptance.sqlite-shm"):
            if path.exists():
                path.unlink()
        name = self.docker.name(lease.run_id, lease.generation)
        try:
            authorized()
            self.docker.command(self.args(lease, [
                "python", "-m", "uvicorn", "app:app", "--host", "127.0.0.1", "--port", "8000",
            ]))
            authorized()
            self.docker.command(["start", name])
            try:
                first = self.judge(lease, case, "main", [], authorized, stop)
            except SandboxError:
                authorized()
                app = self.docker.inspect(lease.run_id, lease.generation)
                if app is None or app["State"]["Running"]:
                    raise
                first = {"startup_failed": True, "checks": [], "saved": []}
            checks = first["checks"]
            if not first.get("startup_failed") and not first.get("contract_failed"):
                authorized()
                self.docker.command(["restart", "--time", "1", name])
                second = self.judge(lease, case, "restart", first["saved"], authorized, stop)
                checks += second["checks"]
                invalid = second.get("startup_failed", False)
            else:
                invalid = True
            covered = {row["acceptance_id"] for row in checks}
            required = {a["id"] for a in context["spec"]["acceptance_criteria"]}
            passed = bool(checks) and not invalid and covered == required and all(
                row["passed"] for row in checks
            )
            # Only contract-level observations go back to the repair model, never frozen inputs.
            feedback = [{k: row[k] for k in ("acceptance_id", "requirement_ids", "expected", "actual")}
                        for row in checks if not row["passed"] and not row["held_out"]]
            if not passed and not feedback:
                feedback = [{"acceptance_id": "contract", "requirement_ids": [
                    r["id"] for r in context["spec"]["requirements"]
                ], "expected": "Start app:app and satisfy the specified contract, including boundaries",
                    "actual": "Startup, coverage or frozen regression mismatch"}]
            authorized()
            return {**report, "outcome": "PASSED" if passed else "FAILED",
                    "checks": checks, "feedback": feedback[:12]}
        finally:
            self.docker.remove(lease.run_id, lease.generation)
