"""Per-call scheduling and generation policy, without mutating shared clients."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time
from typing import Callable
from typing import Any


class LlmCallCancelled(Exception):
    """The owner stopped or its execution generation changed before dispatch."""


class LlmCallDeferred(Exception):
    """Higher-priority work occupied the model; retry this inference later."""


@dataclass(frozen=True)
class BoundRoute:
    client: Any
    model: str | None
    window: int | None
    signature: tuple
    resolve: Callable[[], tuple]

    def acquired(self):
        if self.resolve() != self.signature:
            raise LlmCallDeferred("Inference route changed; repack before dispatch")


def bind_route(client_provider, model_provider, window_provider):
    def resolve():
        client = client_provider()
        model, window = model_provider(), window_provider()
        revision = getattr(client, "routing_revision", None)
        identity = (
            (id(client), revision)
            if type(revision) is int
            else (None, getattr(client, "base_url", None))
        )
        return client, model, window, (*identity, model, window)

    client, model, window, signature = resolve()
    return BoundRoute(client, model, window, signature, lambda: resolve()[3])


@dataclass
class CallScope:
    cancelled: Callable[[], bool] = lambda: False
    on_wait: Callable[[], None] = lambda: None
    on_acquired: Callable[[], None] = lambda: None
    queue_timeout: float | None = 10.0
    reasoning_tokens: int | None = None
    queue_seconds: float = 0.0
    elapsed_seconds: float = 0.0
    reported_tokens: int = 0
    request_deadline: float | None = None

    def remaining_timeout(self, requested: float) -> float:
        if self.request_deadline is None:
            return requested
        return max(0.0, min(requested, self.request_deadline - time.monotonic()))

    @property
    def active_seconds(self):
        return max(0.0, self.elapsed_seconds - self.queue_seconds)


_scope: ContextVar[CallScope | None] = ContextVar("llm_call_scope", default=None)


def current_call_scope():
    return _scope.get()


def record_call_usage(usage):
    scope = current_call_scope()
    if scope is not None:
        scope.reported_tokens += max(0, int(getattr(usage, "total_tokens", 0) or 0))


@contextmanager
def call_scope(scope: CallScope):
    if _scope.get() is scope:
        yield scope
        return
    token = _scope.set(scope)
    started = time.monotonic()
    try:
        if scope.cancelled():
            raise LlmCallCancelled()
        yield scope
    finally:
        scope.elapsed_seconds += time.monotonic() - started
        _scope.reset(token)
