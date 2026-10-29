from spec2repo.harness.loop import AgentLoop
from spec2repo.harness.verification import Verifier
from spec2repo.runtime.executor import ExecutionWorker
from spec2repo.sandbox.docker import SandboxError
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.verification import VerificationRepository


class AgentWorker(ExecutionWorker):
    def __init__(self, engine, streams, consumer, workspaces, sandbox, model,
                 *, profile="guarded", **kwargs):
        super().__init__(engine, streams, consumer, workspaces, sandbox, **kwargs)
        self.agent = AgentLoop(
            AgentRepository(engine, lease_seconds=kwargs.get("lease_seconds", 60)),
            model,
            workspaces,
            sandbox,
            profile=profile,
        )
        self.verification = VerificationRepository(
            engine, lease_seconds=kwargs.get("lease_seconds", 60)
        )
        self.verifier = Verifier(workspaces, sandbox)

    def execute(self, lease, stop):
        context = self.agent.repository.context(lease)
        if context["phase"] == "VERIFY":
            try:
                report = self.verifier.run(
                    lease, context, lambda: self.agent.check(lease, stop), stop
                )
            except SandboxError:
                self.agent.check(lease, stop)
                report = {
                    "spec_hash": lease.spec_hash, "snapshot_hash": context["snapshot_hash"],
                    "suite_hash": self.verifier.suite_hash, "outcome": "ERROR",
                    "reason": "Verification infrastructure failure", "feedback": [], "checks": [],
                }
            return self.verification.commit(lease, report)
        return self.agent.run(lease, stop)
