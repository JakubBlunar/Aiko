"""Unit tests for the H9 away-diary worker (:class:`DiaryWorker`).

Covers the gate matrix (away / cooldown / LLM / context),
the LLM compose round-trip, persistence into a ``diary`` memory, the
kv watermark accounting, the ``force_next`` MCP bypass, and the
``build_recent_context`` transcript helper.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timezone
from typing import Any

from app.core.proactive.diary_worker import DiaryWorker, build_recent_context, context_key


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
    def __init__(self, entry: str = "The quiet felt different without them.") -> None:
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
        self.assertEqual(out, "")

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


class DemandTests(unittest.TestCase):
    """The P44 probe. One entry is owed or it isn't."""

    def test_entry_due_reports_pressure(self) -> None:
        worker = _make_worker()
        now = datetime.now(timezone.utc)
        self.assertTrue(worker.is_ready(now=now, last_run_at=None))
        signal = worker.demand(now=now, last_run_at=None)
        self.assertGreater(signal.pressure, 0.0)
        self.assertTrue(signal.needs_llm)

    def test_client_connected_vetoes(self) -> None:
        worker = _make_worker(away=False)
        now = datetime.now(timezone.utc)
        self.assertFalse(worker.is_ready(now=now, last_run_at=None))
        signal = worker.demand(now=now, last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "client_connected")

    def test_cooldown_vetoes(self) -> None:
        kv = {
            "diary_worker.last_fired_at": datetime.now(
                timezone.utc,
            ).isoformat(timespec="seconds"),
        }
        worker = _make_worker(kv=kv, cooldown_seconds=10800.0)
        now = datetime.now(timezone.utc)
        self.assertFalse(worker.is_ready(now=now, last_run_at=None))
        self.assertEqual(
            worker.demand(now=now, last_run_at=None).reason, "cooldown",
        )

    def test_thin_context_vetoes(self) -> None:
        worker = _make_worker(context="hi", min_context_chars=80)
        now = datetime.now(timezone.utc)
        self.assertFalse(worker.is_ready(now=now, last_run_at=None))
        self.assertEqual(
            worker.demand(now=now, last_run_at=None).reason, "no_context",
        )

    def test_probe_does_not_consume_the_force_flag(self) -> None:
        """Only ``run`` may spend a one-shot.

        A probe that consumed ``_forced`` would silently swallow the
        MCP force, and the run it was armed for would take the normal
        cooled-down path.
        """
        kv = {
            "diary_worker.last_fired_at": datetime.now(
                timezone.utc,
            ).isoformat(timespec="seconds"),
        }
        worker = _make_worker(kv=kv, cooldown_seconds=10800.0)
        worker.force_next()
        now = datetime.now(timezone.utc)

        signal = worker.demand(now=now, last_run_at=None)
        self.assertEqual(signal.reason, "forced")
        self.assertTrue(worker.is_ready(now=now, last_run_at=None))
        self.assertTrue(worker._forced)

        worker.run()
        self.assertFalse(worker._forced)

    def test_probe_writes_nothing(self) -> None:
        kv: dict[str, str] = {}
        store = _FakeStore()
        worker = _make_worker(kv=kv, store=store)
        now = datetime.now(timezone.utc)
        worker.demand(now=now, last_run_at=None)
        worker.is_ready(now=now, last_run_at=None)
        self.assertEqual(kv, {})
        self.assertEqual(len(store.added), 0)


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
            "recent_context_chars",
        ):
            self.assertIn(key, state)
        self.assertTrue(state["away"])
        self.assertTrue(state["has_llm"])


class JournalQualityTests(unittest.TestCase):
    def test_no_new_conversation_does_not_produce_another_entry_after_cooldown(self):
        worker = _make_worker()
        revision = ["session:42"]
        worker._context_key_provider = lambda: revision[0]
        assert worker.run()["fired"] == 1
        assert worker.run()["reason"] == "no_new_conversation"
        assert len(worker._ollama.calls) == 1
        revision[0] = "session:44"
        assert worker.run()["fired"] == 1
        assert len(worker._memory_store.added) == 2
        journal = worker._memory_store.added[-1].kw["metadata"]["journal"]
        assert journal["authorship"] == "background"

    def test_empty_reflection_spends_cooldown_and_waits_for_new_material(self):
        kv = {}
        worker = _make_worker(llm=_FakeLLM(entry=""), kv=kv)
        worker._context_key_provider = lambda: "session:42"
        assert worker.run()["reason"] == "empty"
        assert "diary_worker.last_fired_at" in kv
        assert worker.run()["reason"] == "no_new_conversation"
        assert len(worker._ollama.calls) == 1

    def test_context_identity_is_based_on_message_content_and_time_not_rendered_age(self):
        from types import SimpleNamespace

        row = SimpleNamespace(id=42, session_id="s1", role="user", content="A quiet thought.",
                              created_at="2026-11-05T10:00:00Z")
        first = context_key([row])
        assert context_key([row]) == first
        row.content = "A corrected thought."
        assert context_key([row]) != first

    def test_previous_entries_are_dated_subjective_context_not_new_evidence(self):
        from types import SimpleNamespace

        worker = _make_worker()
        worker._memory_store.list_recent = lambda **_: [SimpleNamespace(
            id=999, kind="diary", metadata={},
            content="I felt uncertain about offering another suggestion.",
            created_at="2026-11-01T10:00:00Z", temporal_type="past_event",
        )]
        worker.run()
        prompt = worker._ollama.calls[0]["messages"][1]["content"]
        assert "earlier subjective diary entries, not new evidence" in prompt
        assert "I felt uncertain" in prompt
        assert "Do not paraphrase an earlier entry" in prompt


if __name__ == "__main__":
    unittest.main()
