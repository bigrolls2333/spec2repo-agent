"""Process-local values that must not be serialized into graph checkpoints."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar


ProgressCallback = Callable[[str], None]

_progress_callback: ContextVar[ProgressCallback | None] = ContextVar(
    "paper2repo_progress_callback",
    default=None,
)


@contextmanager
def bind_progress_callback(callback: ProgressCallback | None) -> Iterator[None]:
    """Bind a callback for one invocation without putting it in durable state."""
    token = _progress_callback.set(callback)
    try:
        yield
    finally:
        _progress_callback.reset(token)


def emit_progress(message: str) -> None:
    callback = _progress_callback.get()
    if callback is not None:
        callback(message)
