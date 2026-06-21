"""K21 fresh-eyes thread re-summary worker — pure parse + run with fakes.

``parse_thread_note`` is deterministic and covered directly. The worker
run is exercised with in-memory fakes (no real LLM / DB) to pin: the
trigger logic (too-short / interval / age / no-note), the save +
broadcast on success, and the disabled / rate-limited / not-due skips.
"""
from __future__ import annotations

import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.proactive import thread_resummary_worker as trw
from app.core.proactive.thread_resummary_worker import ThreadResummaryWorker


# ── pure helper ─────────────────────────────────────────────────────


class ParseThreadNoteTests(unittest.TestCase):
    def test_valid(self) -> None:
        raw = '{"title": "Parser bug", "note": "Deep in it. Close now."}'
        self.assertEqual(
            trw.parse_thread_note(raw), ("Parser bug", "Deep in it. Close now."),
        )

    def test_pulls_object_out_of_noise(self) -> None:
        raw = 'here:\n{"title": "T", "note": "N"}\nthanks'
        self.assertEqual(trw.parse_thread_note(raw), ("T", "N"))

    def test_malformed(self) -> None:
        self.assertEqual(trw.parse_thread_note("{not json"), ("", ""))

    def test_non_dict(self) -> None:
        self.assertEqual(trw.parse_thread_note('["a"]'), ("", ""))

    def test_missing_fields(self) -> None:
        self.assertEqual(trw.parse_thread_note('{"title": "T"}'), ("T", ""))
        self.assertEqual(trw.parse_thread_note('{"note": "N"}'), ("", "N"))

    def test_trims_long_title(self) -> None:
        raw = '{"title": "' + ("x" * 200) + '", "note": "n"}'
        title, _ = trw.parse_thread_note(raw)
        self.assertLessEqual(len(title), 60)
        self.assertTrue(title.endswith("\u2026"))


# ── fakes ───────────────────────────────────────────────────────────


def _now() -> datetime:
    return datetime(2026, 6, 21, 12, 0, 0, tzinfo=timezone.utc)


class _Note:
    def __init__(self, title: str, note: str, messages_at: int, updated_at: str) -> None:
        self.title = title
        self.note = note
        self.messages_at = messages_at
        self.updated_at = updated_at


class _Msg:
    def __init__(self, role: str, content: str) -> None:
        self.role = role
        self.content = content


class _Summary:
    def __init__(self, summary: str) -> None:
        self.summary = summary


class _FakeDB:
    def __init__(self, *, count: int, note: _Note | None = None,
                 messages: list[_Msg] | None = None) -> None:
        self._count = count
        self._note = note
        self._messages = messages or []
        self.saved: list[tuple[str, str, str, int]] = []

    def get_message_count(self, session_id: str) -> int:
        return self._count

    def get_thread_note(self, session_id: str):
        return self._note

    def get_messages(self, session_id: str, *, limit=None, offset=None):
        return self._messages

    def get_latest_summary(self, session_id: str):
        return None

    def save_thread_note(self, session_id, title, note, messages_at) -> None:
        self.saved.append((session_id, title, note, messages_at))
        self._note = _Note(title, note, messages_at, _now().isoformat())


class _FakeClient:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.surfaces: list[str] = []

    def chat(self, messages, options=None, model=None, surface="chat", **kwargs) -> str:
        self.surfaces.append(surface)
        return self._reply


class _FakeRateLimiter:
    def __init__(self, *, allow: bool = True, hour_used: int = 0,
                 day_used: int = 0) -> None:
        self._allow = allow
        self._hour_used = hour_used
        self._day_used = day_used
        self.allow_calls = 0

    def snapshot(self, now) -> dict[str, int]:
        return {
            "hour_used": self._hour_used, "hour_cap": 6,
            "day_used": self._day_used, "day_cap": 24,
        }

    def allow(self, now) -> bool:
        self.allow_calls += 1
        return self._allow


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(
        thread_resummary_enabled=True,
        thread_resummary_min_messages=12,
        thread_resummary_message_interval=50,
        thread_resummary_max_age_hours=24.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _memory() -> SimpleNamespace:
    return SimpleNamespace(thread_resummary_interval_seconds=3600)


def _make_worker(db: _FakeDB, client: _FakeClient, *, agent=None,
                 limiter=None, notify=None) -> ThreadResummaryWorker:
    return ThreadResummaryWorker(
        chat_db=db,
        ollama=client,
        chat_model="worker-model",
        cancel_event=threading.Event(),
        agent_settings=agent or _agent(),
        memory_settings=_memory(),
        rate_limiter=limiter or _FakeRateLimiter(),
        session_key_provider=lambda: "default:abc",
        user_display_name_provider=lambda: "Jacob",
        assistant_display_name_provider=lambda: "Aiko",
        notify_thread_note=notify,
        clock=_now,
    )


_GOOD_REPLY = '{"title": "Parser bug", "note": "Deep in a parser bug. Frustrated but close."}'


# ── trigger logic (_should_redraft / is_ready) ──────────────────────


class TriggerTests(unittest.TestCase):
    def test_no_note_with_enough_messages_is_due(self) -> None:
        db = _FakeDB(count=20, note=None)
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        self.assertTrue(w._should_redraft("default:abc", 20, _now()))

    def test_too_short_not_due(self) -> None:
        db = _FakeDB(count=5, note=None)
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        self.assertFalse(w._should_redraft("default:abc", 5, _now()))

    def test_message_interval_trigger(self) -> None:
        note = _Note("t", "n", 10, _now().isoformat())
        db = _FakeDB(count=70, note=note)  # 70 - 10 >= 50
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        self.assertTrue(w._should_redraft("default:abc", 70, _now()))

    def test_below_interval_and_fresh_not_due(self) -> None:
        note = _Note("t", "n", 60, _now().isoformat())
        db = _FakeDB(count=70, note=note)  # only 10 new, note fresh
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        self.assertFalse(w._should_redraft("default:abc", 70, _now()))

    def test_age_trigger(self) -> None:
        old = (_now() - timedelta(hours=30)).isoformat()
        note = _Note("t", "n", 65, old)
        db = _FakeDB(count=70, note=note)  # few new but 30h old
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        self.assertTrue(w._should_redraft("default:abc", 70, _now()))

    def test_is_ready_false_when_disabled(self) -> None:
        db = _FakeDB(count=20, note=None)
        w = _make_worker(db, _FakeClient(_GOOD_REPLY),
                         agent=_agent(thread_resummary_enabled=False))
        self.assertFalse(w.is_ready(now=_now(), last_run_at=None))

    def test_is_ready_false_when_rate_exhausted(self) -> None:
        db = _FakeDB(count=20, note=None)
        w = _make_worker(db, _FakeClient(_GOOD_REPLY),
                         limiter=_FakeRateLimiter(hour_used=6))
        self.assertFalse(w.is_ready(now=_now(), last_run_at=None))

    def test_is_ready_true_when_due(self) -> None:
        db = _FakeDB(count=20, note=None)
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        self.assertTrue(w.is_ready(now=_now(), last_run_at=None))


# ── run ─────────────────────────────────────────────────────────────


class RunTests(unittest.TestCase):
    def _msgs(self) -> list[_Msg]:
        return [
            _Msg("user", "my parser keeps choking on nested braces"),
            _Msg("assistant", "let's look at the tokenizer"),
        ]

    def test_writes_note_and_broadcasts(self) -> None:
        db = _FakeDB(count=20, note=None, messages=self._msgs())
        client = _FakeClient(_GOOD_REPLY)
        events: list[dict[str, Any]] = []
        w = _make_worker(db, client, notify=events.append)
        result = w.run()
        self.assertTrue(result["wrote"])
        self.assertEqual(result["title"], "Parser bug")
        self.assertEqual(len(db.saved), 1)
        self.assertEqual(db.saved[0][0], "default:abc")
        self.assertEqual(db.saved[0][3], 20)  # messages_at watermark
        self.assertEqual(client.surfaces, ["thread_resummary_worker"])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["title"], "Parser bug")

    def test_skips_when_disabled(self) -> None:
        db = _FakeDB(count=20, note=None, messages=self._msgs())
        w = _make_worker(db, _FakeClient(_GOOD_REPLY),
                         agent=_agent(thread_resummary_enabled=False))
        result = w.run()
        self.assertTrue(result["skipped"])
        self.assertEqual(result["reason"], "disabled")
        self.assertEqual(db.saved, [])

    def test_skips_when_too_short(self) -> None:
        db = _FakeDB(count=5, note=None, messages=self._msgs())
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        result = w.run()
        self.assertEqual(result["reason"], "too_short")
        self.assertEqual(db.saved, [])

    def test_skips_when_not_due(self) -> None:
        note = _Note("t", "n", 60, _now().isoformat())
        db = _FakeDB(count=70, note=note, messages=self._msgs())
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        result = w.run()
        self.assertEqual(result["reason"], "not_due")
        self.assertEqual(db.saved, [])

    def test_skips_when_rate_limited(self) -> None:
        db = _FakeDB(count=20, note=None, messages=self._msgs())
        limiter = _FakeRateLimiter(allow=False)
        w = _make_worker(db, _FakeClient(_GOOD_REPLY), limiter=limiter)
        result = w.run()
        self.assertEqual(result["reason"], "rate_limited")
        self.assertEqual(db.saved, [])

    def test_empty_note_does_not_save(self) -> None:
        db = _FakeDB(count=20, note=None, messages=self._msgs())
        w = _make_worker(db, _FakeClient("garbage no json"))
        result = w.run()
        self.assertFalse(result["wrote"])
        self.assertEqual(result["reason"], "empty_note")
        self.assertEqual(db.saved, [])

    def test_title_fallback_from_note(self) -> None:
        reply = '{"note": "We are untangling a tricky recursion issue together."}'
        db = _FakeDB(count=20, note=None, messages=self._msgs())
        w = _make_worker(db, _FakeClient(reply))
        result = w.run()
        self.assertTrue(result["wrote"])
        self.assertTrue(result["title"])  # derived from first words of note


if __name__ == "__main__":
    unittest.main()
