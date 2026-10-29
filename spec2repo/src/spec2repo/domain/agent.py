"""Reviewable plans and owner decisions; these never authorize changing CoreSpec."""

import hashlib
import json
from typing import Annotated

from pydantic import Field, StringConstraints

from spec2repo.domain.spec import Contract, CoreSpec, Identifier, Text

BoundedText = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4000)
]


class PlanTask(Contract):
    id: Identifier
    description: BoundedText
    requirement_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    acceptance_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=100)
    files: tuple[Text, ...] = Field(min_length=1, max_length=30)


class ImplementationPlan(Contract):
    summary: BoundedText
    tasks: tuple[PlanTask, ...] = Field(min_length=1, max_length=30)

    def check_spec(self, spec: CoreSpec):
        requirements = {r.id for r in spec.requirements}
        acceptance = {a.id for a in spec.acceptance_criteria}
        if len({t.id for t in self.tasks}) != len(self.tasks):
            raise ValueError("Duplicate task IDs")
        covered, checked = set(), set()
        for task in self.tasks:
            if not set(task.requirement_ids) <= requirements:
                raise ValueError("Unknown requirement")
            if not set(task.acceptance_ids) <= acceptance:
                raise ValueError("Unknown acceptance criterion")
            covered.update(task.requirement_ids)
            checked.update(task.acceptance_ids)
        if covered != requirements or checked != acceptance:
            raise ValueError("Plan must cover every requirement and acceptance criterion")
        return self

    def fingerprint(self, spec_hash):
        body = {"spec_hash": spec_hash, "plan": self.model_dump(mode="json")}
        return hashlib.sha256(
            json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
        ).hexdigest()


class ApprovalDecision(Contract):
    spec_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    plan_hash: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
    revision: int = Field(strict=True, ge=1)
    approve: bool


class ClarificationDecision(Contract):
    expected_version: int = Field(strict=True, ge=0)
    spec: CoreSpec
    reason: BoundedText
