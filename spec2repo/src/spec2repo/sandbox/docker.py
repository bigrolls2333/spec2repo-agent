"""Bounded commands in a fixed image with only one attempt directory mounted."""

import json
import subprocess
import time
from uuid import UUID


class SandboxError(RuntimeError):
    pass


class SandboxStopped(SandboxError):
    pass


class CommandFailed(SandboxError):
    def __init__(self, exit_code, output):
        super().__init__("Container command failed")
        self.exit_code, self.output = exit_code, output


class DockerSandbox:
    def __init__(self, workspaces, *, image="spec2repo-runtime:phase4"):
        self.workspaces = workspaces
        # Resolve a mutable local tag once to its immutable image ID. Never auto-pull.
        result = self.command(["image", "inspect", image, "--format", "{{.Id}}"])
        self.image = result.strip()
        if not self.image.startswith("sha256:"):
            raise SandboxError("Fixed local runtime image is required")

    @staticmethod
    def command(args, *, timeout=15):
        try:
            result = subprocess.run(
                ["docker", *args],
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(f"Docker control failed: {type(exc).__name__}") from None
        if result.returncode:
            # Do not echo arbitrary tool output or host configuration.
            raise SandboxError("Docker control operation failed")
        return result.stdout + result.stderr if args[0] == "logs" else result.stdout

    @staticmethod
    def name(run_id, generation):
        return f"s2r-{UUID(run_id).hex}-{int(generation)}"

    def inspect(self, run_id, generation, *, verifier=False, invocation=None):
        suffix = "-tool-" + UUID(invocation).hex if invocation else ""
        name = self.name(run_id, generation) + ("-verify" if verifier else suffix)
        # ls is used to distinguish missing containers from daemon failures.
        ids = self.command(["ps", "-aq", "--filter", f"name=^/{name}$"]).strip()
        if not ids:
            return None
        info = json.loads(self.command(["inspect", name]))[0]
        labels = info["Config"].get("Labels") or {}
        if labels.get("spec2repo.run") != str(UUID(run_id)) or labels.get(
            "spec2repo.generation"
        ) != str(generation):
            raise SandboxError("Container ownership label mismatch")
        return info

    def remove(self, run_id, generation):
        # Concurrent tool containers share lease labels but have unique invocation names.
        tools = self.command([
            "ps", "-aq", "--filter", f"label=spec2repo.run={UUID(run_id)}",
            "--filter", f"label=spec2repo.generation={int(generation)}",
            "--filter", "label=spec2repo.tool",
        ]).split()
        for container in tools:
            self.command(["rm", "-f", container])
        for verifier in (True, False):
            if self.inspect(run_id, generation, verifier=verifier) is not None:
                name = self.name(run_id, generation) + ("-verify" if verifier else "")
                try:
                    self.command(["rm", "-f", name])
                except SandboxError:
                    if self.inspect(run_id, generation, verifier=verifier) is not None:
                        raise

    def run(self, lease, argv, *, authorized, stop, timeout=30, invocation=None):
        workspace = self.workspaces.workspace(lease.run_id, lease.generation)
        if not workspace.is_dir() or workspace.is_symlink() or "," in str(workspace):
            raise SandboxError("Invalid attempt mount")
        if not argv or not all(isinstance(value, str) for value in argv):
            raise SandboxError("Command must be an argument vector")
        name = self.name(lease.run_id, lease.generation)
        if invocation:
            name += "-tool-" + UUID(invocation).hex
        authorized()
        if stop.is_set():
            raise SandboxStopped("Execution stopped before container creation")
        args = [
            "create",
            "--name",
            name,
            "--label",
            f"spec2repo.run={lease.run_id}",
            "--label",
            f"spec2repo.generation={lease.generation}",
            "--network",
            "none",
            "--read-only",
            "--cap-drop",
            "ALL",
            "--security-opt",
            "no-new-privileges",
            "--user",
            "65534:65534",
            "--memory",
            "256m",
            "--memory-swap",
            "256m",
            "--cpus",
            "1",
            "--pids-limit",
            "64",
            "--init",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=64m",
            "--log-driver",
            "local",
            "--log-opt",
            "max-size=1m",
            "--log-opt",
            "max-file=1",
            "--log-opt",
            "compress=false",
            "--mount",
            f"type=bind,source={workspace},target=/workspace",
            "--workdir",
            "/workspace",
            self.image,
            *argv,
        ]
        if invocation:
            args[1:1] = ["--label", f"spec2repo.tool={UUID(invocation).hex}"]
        try:
            self.command(args)
            # Fence after creation: recovery can remove a created-but-not-started container.
            authorized()
            if stop.is_set():
                raise SandboxStopped("Execution stopped before container start")
            self.command(["start", name])
            deadline = time.monotonic() + timeout
            while True:
                if stop.is_set():
                    raise SandboxStopped("Lease lost or cancellation requested")
                if time.monotonic() >= deadline:
                    raise SandboxStopped("Container command timed out")
                info = self.inspect(lease.run_id, lease.generation, invocation=invocation)
                if info is None:
                    raise SandboxStopped("Container removed by recovery")
                if not info["State"]["Running"]:
                    output = self.command(["logs", "--tail", "100", name])[:65536]
                    if info["State"]["ExitCode"] != 0:
                        raise CommandFailed(info["State"]["ExitCode"], output)
                    authorized()
                    return output
                stop.wait(0.2)
        finally:
            if invocation:
                # Never let one completed tool delete its still-running siblings.
                if self.inspect(lease.run_id, lease.generation, invocation=invocation) is not None:
                    try:
                        self.command(["rm", "-f", name])
                    except SandboxError:
                        if self.inspect(lease.run_id, lease.generation, invocation=invocation):
                            raise
            else:
                self.remove(lease.run_id, lease.generation)
