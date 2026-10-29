"""Fenced acceptance commits and at most two repair handoffs."""

from sqlalchemy import select

from spec2repo.domain.lifecycle import LifecycleError
from spec2repo.storage.agent import AgentRepository
from spec2repo.storage.models import AgentState, Run, Verification


class VerificationRepository(AgentRepository):
    MAX_REPAIRS = 2

    def schedule(self, *, limit=100, run_id=None):
        with self.sessions.begin() as session:
            statement = select(Run).where(Run.status == "WAITING_VERIFICATION")
            if run_id is not None:
                statement = statement.where(Run.id == run_id)
            rows = session.scalars(
                statement.limit(limit).with_for_update(skip_locked=True)
            ).all()
            for run in rows:
                state = session.get(AgentState, run.id)
                if state is None or state.phase != "VERIFY" or not state.snapshot_hash:
                    raise LifecycleError("Missing submitted implementation")
                self.enqueue(session, run)
            return len(rows)

    def commit(self, lease, report):
        with self.sessions.begin() as session:
            run, now = self.execution.guard(session, lease)
            state = self.state(session, run)
            previous = session.scalars(
                select(Verification).where(Verification.run_id == run.id)
                .order_by(Verification.sequence)
            ).all()
            relevant = [row for row in previous if row.spec_hash == run.spec_hash]
            if (
                state.phase != "VERIFY" or report["spec_hash"] != run.spec_hash
                or report["snapshot_hash"] != state.snapshot_hash
                or report["outcome"] not in {"PASSED", "FAILED", "ERROR"}
                or (relevant and relevant[-1].suite_hash != report["suite_hash"])
            ):
                raise LifecycleError("Verification identity or suite changed")
            if report["outcome"] == "PASSED":
                checks = report.get("checks", [])
                required = {a["id"] for a in run.spec["acceptance_criteria"]}
                if (not checks or any(c.get("passed") is not True for c in checks)
                        or {c.get("acceptance_id") for c in checks} != required):
                    raise LifecycleError("Success requires complete acceptance evidence")
            sequence = previous[-1].sequence + 1 if previous else 1
            repairs = sum(row.report.get("repair_scheduled", False) for row in previous)
            repair = report["outcome"] == "FAILED" and repairs < self.MAX_REPAIRS
            report = {**report, "repair_scheduled": repair}
            session.add(Verification(
                run_id=run.id, sequence=sequence, generation=lease.generation,
                spec_hash=run.spec_hash, snapshot_hash=state.snapshot_hash,
                suite_hash=report["suite_hash"], report=report, created_at=now,
            ))
            # Repair allowance belongs to the run, not to a process or specification revision.
            target = "SUCCEEDED" if report["outcome"] == "PASSED" else "FAILED"
            self.wait(session, run, lease, target if not repair else "REPAIR_SCHEDULED", now)
            if repair:
                state.phase = "REPAIR"
                self.enqueue(session, run)
            else:
                state.phase = "FINAL"
            self.event(session, run, "verification.completed", {
                "sequence": sequence, "outcome": report["outcome"],
                "snapshot_hash": state.snapshot_hash, "suite_hash": report["suite_hash"],
                "repair_scheduled": repair,
            })
            return run.status

    def artifact(self, owner, run_id):
        with self.sessions.begin() as session:
            run = self.owned(session, owner, run_id)
            last = session.scalar(select(Verification).where(Verification.run_id == run_id)
                                  .order_by(Verification.sequence.desc()))
            if run.status != "SUCCEEDED" or last is None or last.report["outcome"] != "PASSED":
                raise LifecycleError("No verified deliverable")
            return {"snapshot_hash": last.snapshot_hash, "spec_hash": last.spec_hash,
                    "suite_hash": last.suite_hash, "verification_sequence": last.sequence}

    def reports(self, owner, run_id):
        with self.sessions.begin() as session:
            self.owned(session, owner, run_id)
            rows = session.scalars(
                select(Verification).where(Verification.run_id == run_id)
                .order_by(Verification.sequence)
            ).all()
            return [{"sequence": r.sequence, "generation": r.generation, **r.report} for r in rows]
