import pytest

from spec2repo.domain.lifecycle import (
    LifecycleError,
    RunSnapshot,
    StaleVersionError,
    transition,
)
from spec2repo.domain.lifecycle import (
    RunStatus as S,
)


def test_approval_wait_releases_run_and_can_be_requeued():
    run = transition(RunSnapshot(), S.RUNNING, expected_version=0)
    run = transition(run, S.WAITING_APPROVAL, expected_version=1)
    run = transition(run, S.QUEUED, expected_version=2)
    assert run == RunSnapshot(status=S.QUEUED, version=3)


def test_completion_requires_verification():
    run = RunSnapshot(status=S.RUNNING, version=4)
    with pytest.raises(LifecycleError, match="independent verification"):
        transition(run, S.SUCCEEDED, expected_version=4)
    finished = transition(run, S.SUCCEEDED, expected_version=4, verification_passed=True)
    assert finished.status == S.SUCCEEDED


def test_cancel_wins_then_completion_cannot_commit():
    # Sequential state observations only; SQL race tests belong to the next stage.
    running = RunSnapshot(status=S.RUNNING, version=2)
    cancelling = transition(running, S.CANCELLING, expected_version=2)
    with pytest.raises(StaleVersionError):
        transition(cancelling, S.SUCCEEDED, expected_version=2, verification_passed=True)
    with pytest.raises(LifecycleError, match="Cannot transition"):
        transition(cancelling, S.SUCCEEDED, expected_version=3, verification_passed=True)
    assert transition(cancelling, S.CANCELLED, expected_version=3).status == S.CANCELLED


def test_completion_wins_then_cancel_is_rejected():
    run = transition(
        RunSnapshot(status=S.RUNNING),
        S.SUCCEEDED,
        expected_version=0,
        verification_passed=True,
    )
    with pytest.raises(LifecycleError):
        transition(run, S.CANCELLING, expected_version=1)


@pytest.mark.parametrize("status", [S.QUEUED, S.WAITING_APPROVAL, S.WAITING_CLARIFICATION])
def test_waiting_runs_can_be_cancelled_without_running(status):
    cancelled = transition(RunSnapshot(status=status), S.CANCELLED, expected_version=0)
    assert cancelled.status == S.CANCELLED


@pytest.mark.parametrize("status", [S.SUCCEEDED, S.CANCELLED])
def test_completed_runs_cannot_restart(status):
    with pytest.raises(LifecycleError):
        transition(RunSnapshot(status=status), S.QUEUED, expected_version=0)


def test_failure_can_be_explicitly_requeued():
    assert transition(RunSnapshot(status=S.FAILED), S.QUEUED, expected_version=0).status == S.QUEUED
