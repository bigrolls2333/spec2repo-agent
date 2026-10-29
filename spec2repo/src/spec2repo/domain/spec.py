"""Versioned specification contracts; no generated-repository templates."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

Text = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
Identifier = Annotated[Text, StringConstraints(pattern=r"^[A-Za-z][A-Za-z0-9_.-]*$")]
HttpStatus = Annotated[int, Field(strict=True, ge=100, le=599)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Requirement(Contract):
    id: Identifier
    description: Text


class AcceptanceCriterion(Contract):
    id: Identifier
    requirement_ids: tuple[Identifier, ...] = Field(min_length=1)
    given: Text
    when: Text
    then: Text


class Operation(Contract):
    id: Identifier
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    path: Annotated[Text, StringConstraints(pattern=r"^/[^\s?#]*$")]
    requirement_ids: tuple[Identifier, ...] = Field(min_length=1)
    description: Text
    response_statuses: tuple[HttpStatus, ...] = Field(min_length=1)


class OpenQuestion(Contract):
    id: Identifier
    question: Text
    blocking: bool = True
    answer: Text | None = None


class CoreSpec(Contract):
    """A first-profile contract, not a claim of arbitrary-domain support.

    Textual acceptance criteria need an independently implemented verifier.
    Structural validation alone does not prove that the specification is complete.
    """

    schema_version: Literal[1] = 1
    id: Identifier
    version: Annotated[int, Field(strict=True, ge=1)]
    profile: Literal["python_api"] = "python_api"
    title: Text
    requirements: tuple[Requirement, ...] = Field(min_length=1)
    acceptance_criteria: tuple[AcceptanceCriterion, ...] = Field(min_length=1)
    operations: tuple[Operation, ...] = Field(min_length=1)
    constraints: tuple[Text, ...] = ()
    open_questions: tuple[OpenQuestion, ...] = ()

    @model_validator(mode="after")
    def check_traceability(self) -> Self:
        groups = {
            "requirements": self.requirements,
            "acceptance_criteria": self.acceptance_criteria,
            "operations": self.operations,
            "open_questions": self.open_questions,
        }
        for label, items in groups.items():
            ids = [item.id for item in items]
            if len(ids) != len(set(ids)):
                raise ValueError(f"Duplicate IDs in {label}")

        known = {requirement.id for requirement in self.requirements}
        covered: set[str] = set()
        for item in (*self.acceptance_criteria, *self.operations):
            refs = item.requirement_ids
            if len(refs) != len(set(refs)):
                raise ValueError(f"Duplicate requirement references in {item.id}")
            unknown = set(refs) - known
            if unknown:
                raise ValueError(f"{item.id} references unknown requirements: {sorted(unknown)}")
        for criterion in self.acceptance_criteria:
            covered.update(criterion.requirement_ids)
        if missing := known - covered:
            raise ValueError(f"Requirements without acceptance criteria: {sorted(missing)}")

        routes = [(operation.method, operation.path) for operation in self.operations]
        if len(routes) != len(set(routes)):
            raise ValueError("Duplicate method/path pairs in operations")
        return self

    @property
    def blocking_question_ids(self) -> tuple[str, ...]:
        return tuple(
            question.id
            for question in self.open_questions
            if question.blocking and question.answer is None
        )

    @property
    def ready_for_planning(self) -> bool:
        return not self.blocking_question_ids

    def fingerprint(self) -> str:
        """Hash normalized contract content; meaningful revisions change the hash."""
        encoded = json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()
