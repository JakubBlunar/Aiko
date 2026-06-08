"""Priority gate in front of the shared worker LLM.

The background side of Aiko all serialises on a single local Ollama
model (one GPU): ~15 background workers plus the new nested-workflow
tasks. Without ordering, a long workflow run can sit in front of a
memory-extraction pass that feeds the next reply. This module adds a
fair priority semaphore so conversation-critical workers beat
maintenance workers beat background tasks on that single model.

Two pieces:

* :class:`LlmPriorityGate` — a fair priority semaphore reusing the
  ``BrainEventQueue`` heapq-of-waiters pattern. ``acquire(priority)``
  blocks until a slot is free AND the caller is the highest-priority
  waiter; ``release(priority)`` wakes the next. **Non-preemptive**: an
  in-flight call runs to completion — priority only decides who goes
  *next*. FIFO tie-break within a tier via a monotonic sequence.
* :class:`GatedChatClient` — a transparent :class:`ChatClient` proxy
  that acquires the gate **per generating call** (so the workflow
  daemon releases the gate while waiting on its children — no
  priority-inversion deadlock) and delegates everything else straight
  through.

Three tiers (lower int wins, matching ``BrainEventQueue``):

* ``CONVERSATION_WORKER`` (10) — per-turn / speaking-window workers
  that feed the next reply (memory extraction, dialogue-act, …).
* ``MAINTENANCE_WORKER`` (50) — idle-scheduler workers (decay,
  promotion, conflict, schedule-learner, day-color, dream, …).
* ``TASK`` (100) — nested-workflow planner + skills.

When the gate is disabled (``agent.worker_llm_gate_enabled=False``) the
proxy is constructed with ``gate=None`` and becomes a pure pass-through
— zero behaviour change.

Logging contract:

* DEBUG ``llm-gate acquire: tier=<n> name=<s> waited_ms=<ms> inflight=<n> queued=<n>``
* DEBUG ``llm-gate release: tier=<n> name=<s> held_ms=<ms> inflight=<n>``
"""
from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from collections.abc import Generator
from typing import Any


log = logging.getLogger("app.llm_gate")


# ── priority tiers ────────────────────────────────────────────────────
CONVERSATION_WORKER = 10
MAINTENANCE_WORKER = 50
TASK = 100

# Stable name -> tier mapping for config overrides + stats readability.
TIER_NAMES: dict[str, int] = {
    "conversation": CONVERSATION_WORKER,
    "conversation_worker": CONVERSATION_WORKER,
    "maintenance": MAINTENANCE_WORKER,
    "maintenance_worker": MAINTENANCE_WORKER,
    "task": TASK,
}


def tier_from_name(name: str, default: int = MAINTENANCE_WORKER) -> int:
    """Resolve a config tier name (case-insensitive) to its int."""
    return TIER_NAMES.get((name or "").strip().lower(), default)


def tier_label(priority: int) -> str:
    """Reverse-map an int tier to a readable label (best-effort)."""
    if priority <= CONVERSATION_WORKER:
        return "conversation"
    if priority <= MAINTENANCE_WORKER:
        return "maintenance"
    return "task"


class LlmPriorityGate:
    """Fair priority semaphore.

    ``acquire(priority)`` enqueues the caller in a heap keyed
    ``(priority, sequence)`` and blocks until (a) an execution slot is
    free and (b) the caller is the head of the heap (highest priority,
    FIFO within a tier). ``release()`` frees a slot and wakes the next
    head.
    """

    def __init__(self, *, max_concurrency: int = 1, name: str = "worker") -> None:
        self._max = max(1, int(max_concurrency))
        self._name = name
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._heap: list[tuple[int, int]] = []  # (priority, sequence)
        self._seq = itertools.count()
        self._inflight = 0
        # Per-tier cumulative telemetry.
        self._grants: dict[int, int] = {}
        self._wait_ms_total: dict[int, float] = {}
        self._wait_ms_max: dict[int, float] = {}

    # ── acquire / release ────────────────────────────────────────────

    def acquire(self, priority: int) -> float:
        """Block until granted. Returns the wait time in seconds."""
        prio = int(priority)
        wait_start = time.monotonic()
        with self._cond:
            seq = next(self._seq)
            me = (prio, seq)
            heapq.heappush(self._heap, me)
            # Wake any waiters so a newly-arrived higher-priority entry
            # re-orders the head correctly.
            self._cond.notify_all()
            while True:
                if self._inflight < self._max and self._heap and self._heap[0] == me:
                    heapq.heappop(self._heap)
                    self._inflight += 1
                    break
                self._cond.wait()
            waited = time.monotonic() - wait_start
            waited_ms = waited * 1000.0
            self._grants[prio] = self._grants.get(prio, 0) + 1
            self._wait_ms_total[prio] = self._wait_ms_total.get(prio, 0.0) + waited_ms
            self._wait_ms_max[prio] = max(self._wait_ms_max.get(prio, 0.0), waited_ms)
            inflight = self._inflight
            queued = len(self._heap)
        log.debug(
            "llm-gate acquire: tier=%d name=%s waited_ms=%.1f inflight=%d queued=%d",
            prio,
            self._name,
            waited_ms,
            inflight,
            queued,
        )
        return waited

    def release(self, priority: int) -> None:
        with self._cond:
            if self._inflight > 0:
                self._inflight -= 1
            inflight = self._inflight
            self._cond.notify_all()
        log.debug(
            "llm-gate release: tier=%d name=%s inflight=%d",
            int(priority),
            self._name,
            inflight,
        )

    # ── observability ────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        with self._lock:
            queued_by_tier: dict[int, int] = {}
            for prio, _seq in self._heap:
                queued_by_tier[prio] = queued_by_tier.get(prio, 0) + 1
            per_tier: dict[str, dict[str, float]] = {}
            for prio, count in self._grants.items():
                total = self._wait_ms_total.get(prio, 0.0)
                per_tier[tier_label(prio)] = {
                    "priority": prio,
                    "grants": count,
                    "avg_wait_ms": round(total / count, 2) if count else 0.0,
                    "max_wait_ms": round(self._wait_ms_max.get(prio, 0.0), 2),
                }
            return {
                "name": self._name,
                "max_concurrency": self._max,
                "inflight": self._inflight,
                "queued": len(self._heap),
                "queued_by_tier": {
                    tier_label(p): c for p, c in queued_by_tier.items()
                },
                "tiers": per_tier,
            }


class GatedChatClient:
    """Transparent :class:`ChatClient` proxy that gates generating calls.

    Wraps a real worker client + a shared :class:`LlmPriorityGate` + a
    fixed priority. The generating methods (``chat`` / ``chat_with_tools``
    / ``chat_json`` / ``chat_stream``) acquire the gate around the call;
    everything else (``update_runtime``, ``close``, ``get_context_length``,
    ``list_models``, ``base_url``, ``last_usage``, …) is forwarded
    straight through via ``__getattr__``.

    ``gate=None`` makes the proxy a pure pass-through (gate disabled).
    """

    def __init__(
        self,
        inner: Any,
        gate: LlmPriorityGate | None,
        priority: int,
        *,
        name: str = "",
    ) -> None:
        self._inner = inner
        self._gate = gate
        self._priority = int(priority)
        self._name = name or tier_label(priority)

    def retarget(
        self,
        inner: Any,
        gate: LlmPriorityGate | None,
        priority: int | None = None,
    ) -> None:
        """Repoint this proxy at a new inner client / gate in place.

        Used on ``reconfigure_chat_llm`` so the ~24 worker references that
        already hold this proxy object transparently follow the new
        worker-client topology without re-wiring every worker. The swap is
        a plain attribute write — any call already inside the gate keeps
        its captured locals.
        """
        self._inner = inner
        self._gate = gate
        if priority is not None:
            self._priority = int(priority)

    # ── gated generating calls ───────────────────────────────────────

    def chat(self, *args: Any, **kwargs: Any) -> Any:
        if self._gate is None:
            return self._inner.chat(*args, **kwargs)
        self._gate.acquire(self._priority)
        t0 = time.monotonic()
        try:
            return self._inner.chat(*args, **kwargs)
        finally:
            self._log_held(t0)
            self._gate.release(self._priority)

    def chat_with_tools(self, *args: Any, **kwargs: Any) -> Any:
        if self._gate is None:
            return self._inner.chat_with_tools(*args, **kwargs)
        self._gate.acquire(self._priority)
        t0 = time.monotonic()
        try:
            return self._inner.chat_with_tools(*args, **kwargs)
        finally:
            self._log_held(t0)
            self._gate.release(self._priority)

    def chat_json(self, *args: Any, **kwargs: Any) -> Any:
        if self._gate is None:
            return self._inner.chat_json(*args, **kwargs)
        self._gate.acquire(self._priority)
        t0 = time.monotonic()
        try:
            return self._inner.chat_json(*args, **kwargs)
        finally:
            self._log_held(t0)
            self._gate.release(self._priority)

    def chat_stream(self, *args: Any, **kwargs: Any) -> Generator[str, None, None]:
        if self._gate is None:
            yield from self._inner.chat_stream(*args, **kwargs)
            return
        # Acquire on entry; release when the generator is exhausted or
        # closed (GeneratorExit) so a caller that abandons the stream
        # still frees the slot.
        self._gate.acquire(self._priority)
        t0 = time.monotonic()
        try:
            yield from self._inner.chat_stream(*args, **kwargs)
        finally:
            self._log_held(t0)
            self._gate.release(self._priority)

    def _log_held(self, t0: float) -> None:
        held_ms = (time.monotonic() - t0) * 1000.0
        log.debug(
            "llm-gate held: tier=%d name=%s held_ms=%.1f",
            self._priority,
            self._name,
            held_ms,
        )

    # ── ungated passthrough (statically present so the proxy satisfies
    #    the runtime_checkable ChatClient protocol, whose 3.13 isinstance
    #    uses static attribute lookup and skips __getattr__) ───────────

    @property
    def base_url(self) -> Any:
        return self._inner.base_url

    @property
    def last_usage(self) -> Any:
        return self._inner.last_usage

    def list_models(self) -> Any:
        return self._inner.list_models()

    def get_context_length(self, model: str) -> Any:
        return self._inner.get_context_length(model)

    def update_runtime(self, *args: Any, **kwargs: Any) -> Any:
        fn = getattr(self._inner, "update_runtime", None)
        if callable(fn):
            return fn(*args, **kwargs)
        return None

    def close(self) -> Any:
        fn = getattr(self._inner, "close", None)
        if callable(fn):
            return fn()
        return None

    def __getattr__(self, item: str) -> Any:
        # Reached only for attrs not defined above (provider-specific
        # extras). Forward to the wrapped client.
        return getattr(self._inner, item)


__all__ = [
    "LlmPriorityGate",
    "GatedChatClient",
    "CONVERSATION_WORKER",
    "MAINTENANCE_WORKER",
    "TASK",
    "TIER_NAMES",
    "tier_from_name",
    "tier_label",
]
