"""Pure lifecycle rules. The future repository must enforce them atomically in SQL.

No in-memory object here provides distributed locking or durable persistence.
"""

from dataclasses import dataclass, replace
from enum import Enum


class RunStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    WAITING_CLARIFICATION = "WAITING_CLARIFICATION"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_VERIFICATION = "WAITING_VERIFICATION"
    RETRY_WAIT = "RETRY_WAIT"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


class LifecycleError(ValueError):
    """A requested transition violates the run lifecycle."""


class StaleVersionError(LifecycleError):
    """The caller made its decision using an obsolete state version."""


@dataclass(frozen=True)
class RunSnapshot:
    status: RunStatus = RunStatus.QUEUED
    version: int = 0


_ALLOWED = {
    RunStatus.QUEUED: {RunStatus.RUNNING, RunStatus.CANCELLED},
    RunStatus.RUNNING: {
        RunStatus.WAITING_CLARIFICATION,
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_VERIFICATION,
        RunStatus.RETRY_WAIT,
        RunStatus.SUCCEEDED,
        RunStatus.FAILED,
        RunStatus.CANCELLING,
    },
    RunStatus.WAITING_CLARIFICATION: {RunStatus.QUEUED, RunStatus.CANCELLED},
    RunStatus.WAITING_APPROVAL: {RunStatus.QUEUED, RunStatus.CANCELLED},
    RunStatus.WAITING_VERIFICATION: {RunStatus.QUEUED, RunStatus.CANCELLED},
    RunStatus.RETRY_WAIT: {RunStatus.QUEUED, RunStatus.CANCELLED},
    RunStatus.CANCELLING: {RunStatus.CANCELLED},
    RunStatus.CANCELLED: set(),
    RunStatus.SUCCEEDED: set(),
    # Explicit user retry; a future service must create a new attempt/generation.
    RunStatus.FAILED: {RunStatus.QUEUED},
}


def transition(
    current: RunSnapshot,
    target: RunStatus,
    *,
    expected_version: int,
    verification_passed: bool = False,
) -> RunSnapshot:
    """Validate a transition before a database compare-and-swap update.

    The service must obtain verification_passed from its protected verifier,
    never from an agent or a client request. This function only models that gate.
    """
    if current.version != expected_version:
        raise StaleVersionError(
            f"Expected version {expected_version}, current version is {current.version}"
        )
    if target not in _ALLOWED[current.status]:
        raise LifecycleError(f"Cannot transition from {current.status.value} to {target.value}")
    if target == RunStatus.SUCCEEDED and verification_passed is not True:
        raise LifecycleError("Successful completion requires independent verification")
    return replace(current, status=target, version=current.version + 1)
