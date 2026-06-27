"""Unit tests for the H9 away-diary worker (:class:`DiaryWorker`).

Covers the gate matrix (away / cooldown / daily-cap / LLM / context),
the LLM compose round-trip, persistence into a ``diary`` memory, the
kv watermark accounting, the ``force_next`` MCP bypass, and the
``build_recent_context`` transcript helper.
"""
from __future__ import annotations

import json
import unittest
from typing import Any

from app.core.proactive.diary_worker import DiaryWorker, build_recent_context


class _FakeMemory:
    def __init__(self, mem_id: int, content: str, kind: str, **kw: Any) -> None:
        self.id = mem_id
        self.content = content
        self.kind = kind
        self.kw = kw


class _FakeStore:
    def __init__(self) -> None:
        self.added: list[_FakeMemory] = []
        self._next_id = 1

    def add(self, *, content: str, kind: str, embedding: Any, **kw: Any):
        mem = _FakeMemory(self._next_id, content, kind, **kw)
        self._next_id += 1
        self.added.append(mem)
        return mem


class _FakeLLM:
    def __init__(self, entry: str = "Today felt quiet without them.") -> None:
        self.entry = entry
        self.calls: list[dict[str, Any]] = []

    def chat_json(self, messages, *, model, options, format_json, surface):
        self.calls.append(
            {"messages": messages, "model": model, "surface": surface}
        )
        return json.dumps({"entry": self.entry}), None


class _Row:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


def _make_worker(
    *,
    store: _FakeStore | None = None,
    llm: _FakeLLM | None = None,
    away: bool = True,
    context: str = "Aiko: hi\nJacob: hey, rough day at work",
    kv: dict[str, str] | None = None,
    enabled: bool = True,
    cooldown_seconds: float = 0.0,
    daily_cap: int = 3,
    min_context_chars: int = 8,
    notified: list[Any] | None = None,
) -> DiaryWorker:
    kv = {} if kv is None else kv
    store = store if store is not None else _FakeStore()

    def kv_get(key: str) -> str | None:
        return kv.get(key)

    def kv_set(key: str, value: str) -> None:
        kv[key] = value

    return DiaryWorker(
        memory_store=store,
        embed=lambda text: [0.0, 1.0],
        recent_context_provider=lambda: context,
        is_away_provider=lambda: away,
        user_display_name_provider=lambda: "Jacob",
        kv_get=kv_get,
        kv_set=kv_set,
        enabled_provider=lambda: enabled,
        ollama=llm if llm is not None else _FakeLLM(),
        model="worker-model",
        on_memory_added=(notified.append if notified is not None else None),
        interval_seconds=1800.0,
        cooldown_seconds=cooldown_seconds,
        daily_cap=daily_cap,
        min_context_chars=min_context_chars,
    )


class BuildRecentContextTests(unittest.TestCase):
    def test_renders_speaker_labels(self) -> None:
        rows = [_Row("user", "hello"), _Row("assistant", "hi there")]
        out = build_recent_context(rows, "Jacob")
        self.assertEqual(out, "Jacob: hello\nAiko: hi there")

    def test_skips_blank_and_trims_to_max(self) -> None:
        rows = [_Row("user", ""), _Row("assistant", "x" * 100)]
        out = build_recent_context(rows, "Jacob", max_chars=20)
        self.assertEqual(len(out), 20)

    def test_empty_rows(self) -> None:
        self.assertEqual(build_recent_context([], "Jacob"), "")


class GateTests(unittest.TestCase):
    def test_writes_when_away(self) -> None:
        store = _FakeStore()
        notified: list[Any] = []
        worker = _make_worker(store=store, notified=notified)
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        self.assertEqual(len(store.added), 1)
        mem = store.added[0]
        self.assertEqual(mem.kind, "diary")
        self.assertTrue(mem.kw.get("skip_dedupe"))
        self.assertEqual(mem.kw.get("tier"), "long_term")
        self.assertEqual(len(notified), 1)

    def test_defers_when_client_connected(self) -> None:
        store = _FakeStore()
        worker = _make_worker(store=store, away=False)
        result = worker.run()
        self.assertEqual(result["fired"], 0)
        self.assertEqual(result["reason"], "client_connected")
        self.assertEqual(store.added, [])

    def test_disabled(self) -> None:
        worker = _make_worker(enabled=False)
        result = worker.run()
        self.assertEqual(result["reason"], "disabled")

    def test_no_llm_skips(self) -> None:
        store = _FakeStore()
        worker = DiaryWorker(
            memory_store=store,
            embed=lambda t: [0.0],
            recent_context_provider=lambda: "Jacob: long enough context here",
            is_away_provider=lambda: True,
            user_display_name_provider=lambda: "Jacob",
            kv_get=lambda k: None,
            kv_set=lambda k, v: None,
            ollama=None,
            model=None,
            cooldown_seconds=0.0,
            min_context_chars=8,
        )
        result = worker.run()
        self.assertEqual(result["reason"], "no_llm")

    def test_no_context_skips(self) -> None:
        worker = _make_worker(context="hi", min_context_chars=80)
        result = worker.run()
        self.assertEqual(result["reason"], "no_context")

    def test_empty_entry_skips(self) -> None:
        worker = _make_worker(llm=_FakeLLM(entry=""))
        result = worker.run()
        self.assertEqual(result["reason"], "empty")

    def test_cooldown_blocks_second_run(self) -> None:
        kv: dict[str, str] = {}
        store = _FakeStore()
        worker = _make_worker(
            store=store, kv=kv, cooldown_seconds=3600.0
        )
        first = worker.run()
        self.assertEqual(first["fired"], 1)
        second = worker.run()
        self.assertEqual(second["fired"], 0)
        self.assertEqual(second["reason"], "cooldown")
        self.assertEqual(len(store.added), 1)

    def test_daily_cap(self) -> None:
        kv: dict[str, str] = {}
        store = _FakeStore()
        worker = _make_worker(
            store=store, kv=kv, cooldown_seconds=0.0, daily_cap=1
        )
        self.assertEqual(worker.run()["fired"], 1)
        blocked = worker.run()
        self.assertEqual(blocked["reason"], "daily_cap")

    def test_force_next_bypasses_client_and_cooldown(self) -> None:
        store = _FakeStore()
        worker = _make_worker(
            store=store, away=False, cooldown_seconds=99999.0
        )
        worker.force_next()
        result = worker.run()
        self.assertEqual(result["fired"], 1)
        # force flag is consumed after one run.
        self.assertFalse(worker._forced)


class StateTests(unittest.TestCase):
    def test_state_snapshot_shape(self) -> None:
        worker = _make_worker()
        state = worker.state()
        for key in (
            "enabled",
            "away",
            "has_llm",
            "interval_seconds",
            "cooldown_seconds",
            "daily_cap",
            "recent_context_chars",
        ):
            self.assertIn(key, state)
        self.assertTrue(state["away"])
        self.assertTrue(state["has_llm"])


if __name__ == "__main__":
    unittest.main()
