"""Approval, clarification and model-call budgets survive process restarts."""

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from spec2repo.domain.lifecycle import LifecycleError
from spec2repo.domain.spec import CoreSpec
from spec2repo.storage import memory
from spec2repo.storage.execution import ExecutionRepository
from spec2repo.storage.models import (
    AgentState,
    Attempt,
    OutboxEvent,
    PlanVersion,
    Run,
    RunEvent,
    SpecRevision,
    Verification,
)
from spec2repo.storage.runs import RunNotFound, run_payload, utc_now


class BudgetExceeded(RuntimeError):
    pass


class AgentRepository:
    MAX_CALLS = 32
    MAX_RESERVED_TOKENS = 240_000
    MAX_RESERVED_SECONDS = 1800

    def __init__(self, engine, *, lease_seconds=60):
        self.sessions = sessionmaker(engine, expire_on_commit=False)
        self.execution = (
            ExecutionRepository(engine, lease_seconds=lease_seconds)
            if engine.dialect.name == "mysql"
            else None
        )

    @staticmethod
    def state(session, run):
        state = session.get(AgentState, run.id)
        if state is None:
            state = AgentState(
                run_id=run.id,
                phase="PLAN",
                revision=0,
                questions=[],
                calls=0,
                reserved_tokens=0,
                reserved_seconds=0,
            )
            session.add(state)
        if session.get(SpecRevision, (run.id, run.spec["version"])) is None:
            session.add(
                SpecRevision(
                    run_id=run.id,
                    version=run.spec["version"],
                    spec_hash=run.spec_hash,
                    body=run.spec,
                )
            )
        return state

    @staticmethod
    def event(session, run, kind, payload):
        session.add(RunEvent(run_id=run.id, event_type=kind, payload=payload, created_at=utc_now()))

    @staticmethod
    def owned(session, owner, run_id):
        run = session.scalar(
            select(Run).where(Run.id == run_id, Run.owner_id == owner).with_for_update()
        )
        if run is None:
            raise RunNotFound("Run not found")
        return run

    @staticmethod
    def enqueue(session, run):
        run.status = "QUEUED"
        run.version += 1
        run.updated_at = utc_now()
        session.add(
            OutboxEvent(
                id=str(uuid4()),
                run_id=run.id,
                event_type="run.queued",
                payload={"run_id": run.id, "version": run.version, "generation": run.generation},
                created_at=run.updated_at,
            )
        )

    def context(self, lease):
        with self.sessions.begin() as session:
            run, _ = self.execution.guard(session, lease)
            state = self.state(session, run)
            plan = session.get(PlanVersion, (run.id, state.revision)) if state.revision else None
            reports = session.scalars(select(Verification).where(
                Verification.run_id == run.id, Verification.spec_hash == run.spec_hash
            ).order_by(Verification.sequence.desc()).limit(2)).all()
            latest = reports[0] if reports else None
            repeated = []
            if len(reports) == 2 and all(r.report.get("outcome") == "FAILED" for r in reports):
                failed = [{c["acceptance_id"] for c in r.report.get("checks", [])
                           if c.get("passed") is False} for r in reports]
                repeated = sorted(failed[0] & failed[1])
            return {
                "spec": run.spec,
                "phase": state.phase,
                "task_memory": memory.current(session, run, state).body,
                "plan": plan.body if plan else None,
                "snapshot_hash": state.snapshot_hash,
                "verification_feedback": latest.report.get("feedback", []) if latest else [],
                "repeated_acceptance_failures": repeated,
                "calls": state.calls,
                "remaining_tokens": self.MAX_RESERVED_TOKENS - state.reserved_tokens,
                "remaining_calls": self.MAX_CALLS - state.calls,
                "remaining_seconds": self.MAX_RESERVED_SECONDS - state.reserved_seconds,
            }

    def review(self, owner, run_id):
        with self.sessions.begin() as session:
            run = self.owned(session, owner, run_id)
            state = session.get(AgentState, run_id)
            plans = session.scalars(
                select(PlanVersion)
                .where(PlanVersion.run_id == run_id)
                .order_by(PlanVersion.revision)
            ).all()
            return {
                "run": run_payload(run),
                "phase": state.phase if state else "PLAN",
                "questions": state.questions
                if state
                else [
                    q
                    for q in run.spec.get("open_questions", [])
                    if q.get("blocking") and not q.get("answer")
                ],
                "plans": [
                    {
                        "revision": p.revision,
                        "spec_hash": p.spec_hash,
                        "plan_hash": p.plan_hash,
                        "plan": p.body,
                        "decision": p.decision,
                        "reason": p.reason,
                    }
                    for p in plans
                ],
                "budget": {
                    "calls": state.calls if state else 0,
                    "reserved_tokens": state.reserved_tokens if state else 0,
                    "reserved_seconds": state.reserved_seconds if state else 0,
                    "max_calls": self.MAX_CALLS,
                    "max_tokens": self.MAX_RESERVED_TOKENS,
                    "max_seconds": self.MAX_RESERVED_SECONDS,
                },
                "snapshot_hash": state.snapshot_hash if state else None,
            }

    def reserve(self, lease, tokens, seconds):
        """Charge worst-case allowance before I/O. A lost response never refunds it."""
        with self.sessions.begin() as session:
            run, _ = self.execution.guard(session, lease)
            state = self.state(session, run)
            if (
                tokens <= 0
                or seconds <= 0
                or state.calls >= self.MAX_CALLS
                or state.reserved_tokens + tokens > self.MAX_RESERVED_TOKENS
                or state.reserved_seconds + seconds > self.MAX_RESERVED_SECONDS
            ):
                raise BudgetExceeded("Run budget exhausted")
            state.calls += 1
            state.reserved_tokens += tokens
            state.reserved_seconds += seconds
            self.event(
                session,
                run,
                "agent.call_reserved",
                {
                    "call": state.calls,
                    "generation": lease.generation,
                    "reserved_tokens": tokens,
                    "reserved_seconds": seconds,
                },
            )
            return state.calls

    def reserve_tool_seconds(self, lease, seconds):
        """Reserve additional sequential tool waves without charging a model call."""
        with self.sessions.begin() as session:
            run, _ = self.execution.guard(session, lease)
            state = self.state(session, run)
            if seconds <= 0 or state.reserved_seconds + seconds > self.MAX_RESERVED_SECONDS:
                raise BudgetExceeded("Tool batch time budget exhausted")
            state.reserved_seconds += seconds
            self.event(session, run, "agent.tool_time_reserved", {
                "generation": lease.generation, "reserved_seconds": seconds,
            })

    def record(self, lease, kind, payload):
        with self.sessions.begin() as session:
            run, _ = self.execution.guard(session, lease)
            self.event(session, run, kind, {"generation": lease.generation, **payload})

    def capture_output(self, lease, name, path, result, secret=None):
        with self.sessions.begin() as session:
            run, _ = self.execution.guard(session, lease)
            row = memory.current(session, run, self.state(session, run))
            return memory.capture(session, run, row, name, path, result, secret)

    def update_memory(self, lease, notes, secret=None):
        with self.sessions.begin() as session:
            run, _ = self.execution.guard(session, lease)
            row = memory.current(session, run, self.state(session, run))
            for identifier in notes["evidence_ids"]:
                memory.evidence(session, run, row, identifier)
            sanitized = {
                key: [memory.redact(value, secret) for value in values]
                for key, values in notes.items()
            }
            row.body = {**row.body, "model_notes": sanitized}
            self.event(session, run, "agent.memory_updated", {"generation": lease.generation})
        return {"ok": True, "message": "Model notes saved; these are not verification results."}

    def read_output(self, lease, identifier, offset, limit):
        with self.sessions.begin() as session:
            run, _ = self.execution.guard(session, lease)
            row = memory.current(session, run, self.state(session, run))
            body = memory.evidence(session, run, row, identifier).body
            output = body["output"]
            end = min(offset + limit, len(output))
            return {"ok": True, "evidence_id": identifier, "output": output[offset:end],
                    "next_offset": end if end < len(output) else None,
                    "original_ok": body["ok"], "exit_code": body.get("exit_code"),
                    "capture_limited": body["capture_limited"]}

    @staticmethod
    def wait(session, run, lease, status, now):
        run.status = status
        run.version += 1
        run.updated_at = now
        run.lease_owner = run.lease_until = None
        attempt = session.get(Attempt, (run.id, lease.generation))
        attempt.status, attempt.ended_at = status, now

    def propose(self, lease, plan, *, reason="Initial implementation plan", internal=False):
        with self.sessions.begin() as session:
            run, now = self.execution.guard(session, lease)
            state = self.state(session, run)
            plan.check_spec(CoreSpec.model_validate(run.spec))
            if internal and state.phase not in {"IMPLEMENT", "REPAIR"}:
                raise LifecycleError("Internal revisions require approved implementation")
            if not internal and state.phase != "PLAN":
                raise LifecycleError("Planning is not active")
            state.revision += 1
            session.add(
                PlanVersion(
                    run_id=run.id,
                    revision=state.revision,
                    spec_hash=run.spec_hash,
                    plan_hash=plan.fingerprint(run.spec_hash),
                    body=plan.model_dump(mode="json"),
                    decision="INTERNAL" if internal else "PENDING",
                    reason=reason,
                    created_at=now,
                )
            )
            self.event(
                session,
                run,
                "agent.plan_revised" if internal else "agent.plan_proposed",
                {
                    "revision": state.revision,
                    "plan_hash": plan.fingerprint(run.spec_hash),
                    "reason": reason,
                },
            )
            if not internal:
                self.wait(session, run, lease, "WAITING_APPROVAL", now)

    def decide(self, owner, run_id, decision):
        with self.sessions.begin() as session:
            run = self.owned(session, owner, run_id)
            state = session.get(AgentState, run_id)
            plan = session.get(PlanVersion, (run_id, decision.revision))
            if (
                state is None
                or plan is None
                or state.revision != decision.revision
                or plan.spec_hash != decision.spec_hash
                or run.spec_hash != decision.spec_hash
                or plan.plan_hash != decision.plan_hash
            ):
                raise LifecycleError("Review version is stale")
            desired = "APPROVED" if decision.approve else "REJECTED"
            if plan.decision == desired:
                return run_payload(run)
            if plan.decision != "PENDING" or run.status != "WAITING_APPROVAL":
                raise LifecycleError("Approval already decided or run is not waiting")
            plan.decision = desired
            if decision.approve:
                state.phase = "IMPLEMENT"
                self.enqueue(session, run)
            else:
                run.status = "CANCELLED"
                run.version += 1
                run.updated_at = utc_now()
            self.event(
                session,
                run,
                "agent.approval_decided",
                {"revision": plan.revision, "decision": desired, "owner": owner},
            )
            return run_payload(run)

    def ask(self, lease, questions):
        with self.sessions.begin() as session:
            run, now = self.execution.guard(session, lease)
            state = self.state(session, run)
            state.questions = questions
            self.wait(session, run, lease, "WAITING_CLARIFICATION", now)
            self.event(session, run, "agent.clarification_requested", {"questions": questions})

    def clarify(self, owner, run_id, decision):
        with self.sessions.begin() as session:
            run = self.owned(session, owner, run_id)
            if (
                run.status != "WAITING_CLARIFICATION"
                or run.version != decision.expected_version
                or decision.spec.id != run.spec["id"]
                or decision.spec.version != run.spec["version"] + 1
                or not decision.spec.ready_for_planning
            ):
                raise LifecycleError("Clarification requires the next ready specification version")
            state = self.state(session, run)
            session.flush()  # Preserve original specification before replacing it.
            run.spec = decision.spec.model_dump(mode="json")
            run.spec_hash = decision.spec.fingerprint()
            session.add(
                SpecRevision(
                    run_id=run.id,
                    version=decision.spec.version,
                    spec_hash=run.spec_hash,
                    body=run.spec,
                )
            )
            state.phase, state.questions, state.snapshot_hash = "PLAN", [], None
            self.enqueue(session, run)
            self.event(
                session,
                run,
                "agent.clarification_resolved",
                {"spec_hash": run.spec_hash, "reason": decision.reason},
            )
            return run_payload(run)

    def finish(self, lease, snapshot_hash):
        with self.sessions.begin() as session:
            run, now = self.execution.guard(session, lease)
            state = self.state(session, run)
            if state.phase not in {"IMPLEMENT", "REPAIR"}:
                raise LifecycleError("No approved implementation")
            memory.current(session, run, state).snapshot_hash = snapshot_hash
            state.phase, state.snapshot_hash = "VERIFY", snapshot_hash
            self.wait(session, run, lease, "WAITING_VERIFICATION", now)
            self.event(
                session,
                run,
                "agent.implementation_submitted",
                {"snapshot_hash": snapshot_hash, "revision": state.revision},
            )
