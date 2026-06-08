"""Tests for :mod:`app.llm.llm_gate` — priority gate + GatedChatClient."""
from __future__ import annotations

import threading
import time
import unittest

from app.llm.chat_client import ChatClient
from app.llm.llm_gate import (
    CONVERSATION_WORKER,
    MAINTENANCE_WORKER,
    TASK,
    GatedChatClient,
    LlmPriorityGate,
    tier_from_name,
    tier_label,
)


class _FakeClient:
    """Minimal ChatClient-shaped stub recording call order."""

    def __init__(self) -> None:
        self.base_url = "http://x"
        self.last_usage = object()
        self.closed = False
        self.runtime_updates: list[str] = []

    def chat(self, *a, **k):
        return "chat-ok"

    def chat_with_tools(self, *a, **k):
        return "tools-ok"

    def chat_json(self, *a, **k):
        return ("json-ok", None)

    def chat_stream(self, *a, **k):
        yield "a"
        yield "b"

    def list_models(self):
        return ["m"]

    def get_context_length(self, model):
        return 4096

    def update_runtime(self, *, model=None):
        self.runtime_updates.append(model or "")

    def close(self):
        self.closed = True


class GateOrderingTests(unittest.TestCase):
    def test_concurrency_bound_is_one(self) -> None:
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(CONVERSATION_WORKER)
        acquired_second = threading.Event()

        def second() -> None:
            gate.acquire(CONVERSATION_WORKER)
            acquired_second.set()

        t = threading.Thread(target=second)
        t.start()
        # Second acquire must block while the first holds the only slot.
        self.assertFalse(acquired_second.wait(timeout=0.2))
        gate.release(CONVERSATION_WORKER)
        self.assertTrue(acquired_second.wait(timeout=1.0))
        gate.release(CONVERSATION_WORKER)
        t.join(timeout=1.0)

    def test_higher_priority_waiter_jumps_queue(self) -> None:
        # A TASK waiter that enqueued FIRST must still yield to a
        # CONVERSATION waiter that enqueued later, once the slot frees.
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(CONVERSATION_WORKER)  # holder occupies the slot
        order: list[str] = []
        order_lock = threading.Lock()
        task_enqueued = threading.Event()

        def task_waiter() -> None:
            task_enqueued.set()
            gate.acquire(TASK)
            with order_lock:
                order.append("task")
            gate.release(TASK)

        def conv_waiter() -> None:
            gate.acquire(CONVERSATION_WORKER)
            with order_lock:
                order.append("conversation")
            gate.release(CONVERSATION_WORKER)

        t1 = threading.Thread(target=task_waiter)
        t1.start()
        task_enqueued.wait(timeout=1.0)
        time.sleep(0.05)  # ensure TASK is parked in the heap first
        t2 = threading.Thread(target=conv_waiter)
        t2.start()
        time.sleep(0.05)  # ensure CONVERSATION is parked too
        # Free the slot; the gate should grant CONVERSATION before TASK.
        gate.release(CONVERSATION_WORKER)
        t1.join(timeout=2.0)
        t2.join(timeout=2.0)
        self.assertEqual(order, ["conversation", "task"])

    def test_fifo_within_tier(self) -> None:
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(MAINTENANCE_WORKER)
        order: list[int] = []
        order_lock = threading.Lock()
        started: list[threading.Event] = []

        def waiter(idx: int, ev: threading.Event) -> None:
            ev.set()
            gate.acquire(MAINTENANCE_WORKER)
            with order_lock:
                order.append(idx)
            gate.release(MAINTENANCE_WORKER)

        threads = []
        for i in range(3):
            ev = threading.Event()
            started.append(ev)
            t = threading.Thread(target=waiter, args=(i, ev))
            threads.append(t)
            t.start()
            ev.wait(timeout=1.0)
            time.sleep(0.05)  # serialise enqueue order
        gate.release(MAINTENANCE_WORKER)
        for t in threads:
            t.join(timeout=2.0)
        self.assertEqual(order, [0, 1, 2])

    def test_stats_track_grants(self) -> None:
        gate = LlmPriorityGate(max_concurrency=1)
        gate.acquire(CONVERSATION_WORKER)
        gate.release(CONVERSATION_WORKER)
        stats = gate.stats()
        self.assertEqual(stats["max_concurrency"], 1)
        self.assertIn("conversation", stats["tiers"])
        self.assertEqual(stats["tiers"]["conversation"]["grants"], 1)


class GatedClientTests(unittest.TestCase):
    def test_passthrough_when_disabled(self) -> None:
        inner = _FakeClient()
        proxy = GatedChatClient(inner, None, TASK)
        self.assertEqual(proxy.chat(), "chat-ok")
        self.assertEqual(proxy.chat_with_tools(), "tools-ok")
        self.assertEqual(proxy.chat_json(), ("json-ok", None))
        self.assertEqual(list(proxy.chat_stream()), ["a", "b"])

    def test_forwards_non_gated_attrs(self) -> None:
        inner = _FakeClient()
        gate = LlmPriorityGate(max_concurrency=1)
        proxy = GatedChatClient(inner, gate, MAINTENANCE_WORKER)
        self.assertEqual(proxy.base_url, "http://x")
        self.assertIs(proxy.last_usage, inner.last_usage)
        self.assertEqual(proxy.get_context_length("m"), 4096)
        self.assertEqual(proxy.list_models(), ["m"])
        proxy.update_runtime(model="new")
        self.assertEqual(inner.runtime_updates, ["new"])
        proxy.close()
        self.assertTrue(inner.closed)

    def test_gated_call_acquires_and_releases(self) -> None:
        inner = _FakeClient()
        gate = LlmPriorityGate(max_concurrency=1)
        proxy = GatedChatClient(inner, gate, CONVERSATION_WORKER)
        self.assertEqual(proxy.chat(), "chat-ok")
        # Slot fully released afterwards.
        self.assertEqual(gate.stats()["inflight"], 0)

    def test_chat_stream_releases_on_close(self) -> None:
        inner = _FakeClient()
        gate = LlmPriorityGate(max_concurrency=1)
        proxy = GatedChatClient(inner, gate, TASK)
        gen = proxy.chat_stream()
        self.assertEqual(next(gen), "a")
        # Abandon the stream before exhaustion.
        gen.close()
        self.assertEqual(gate.stats()["inflight"], 0)

    def test_protocol_conformance(self) -> None:
        proxy = GatedChatClient(_FakeClient(), None, TASK)
        self.assertIsInstance(proxy, ChatClient)


class TierHelperTests(unittest.TestCase):
    def test_tier_from_name(self) -> None:
        self.assertEqual(tier_from_name("conversation"), CONVERSATION_WORKER)
        self.assertEqual(tier_from_name("MAINTENANCE"), MAINTENANCE_WORKER)
        self.assertEqual(tier_from_name("task"), TASK)
        self.assertEqual(tier_from_name("unknown"), MAINTENANCE_WORKER)

    def test_tier_label(self) -> None:
        self.assertEqual(tier_label(CONVERSATION_WORKER), "conversation")
        self.assertEqual(tier_label(MAINTENANCE_WORKER), "maintenance")
        self.assertEqual(tier_label(TASK), "task")


if __name__ == "__main__":
    unittest.main()
