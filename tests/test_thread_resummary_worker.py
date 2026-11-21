"""K21 fresh-eyes thread re-summary worker — pure parse + run with fakes.

``parse_thread_note`` is deterministic and covered directly. The worker
run is exercised with in-memory fakes (no real LLM / DB) to pin: the
trigger logic (too-short / interval / age / no-note), the save +
broadcast on success, and the disabled / rate-limited / not-due skips.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock
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
                 messages: list[_Msg] | None = None, summary: str = "") -> None:
        self._count = count
        self._note = note
        self._messages = messages or []
        self._summary = _Summary(summary) if summary else None
        self.saved: list[tuple[str, str, str, int]] = []

    def get_message_count(self, session_id: str) -> int:
        return self._count

    def get_thread_note(self, session_id: str):
        return self._note

    def get_messages(self, session_id: str, *, limit=None, offset=None):
        return self._messages

    def get_latest_summary(self, session_id: str):
        return self._summary

    def get_history_edit_revision(self, session_id: str) -> int:
        return 0

    def save_thread_note(
        self, session_id, title, note, messages_at, *, expected_history_revision=None,
    ) -> bool:
        self.saved.append((session_id, title, note, messages_at))
        self._note = _Note(title, note, messages_at, _now().isoformat())
        return True


class _FakeClient:
    def __init__(self, reply: str) -> None:
        self._reply = reply
        self.surfaces: list[str] = []
        self.requests: list[tuple] = []

    def chat(self, messages, options=None, model=None, surface="chat", **kwargs) -> str:
        self.surfaces.append(surface)
        self.requests.append((messages, options, model))
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
                 limiter=None, notify=None, model_context=None) -> ThreadResummaryWorker:
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
        model_context=model_context,
    )


_GOOD_REPLY = '{"title": "Parser bug", "note": "Deep in a parser bug. Frustrated but close."}'


def test_thread_input_keeps_the_final_negation_of_a_long_message():
    text = "A proposed plan. " * 30 + "But this was imagined and I did not do it."
    db = _FakeDB(count=20, messages=[_Msg("user", text)])
    client = _FakeClient(_GOOD_REPLY)
    assert _make_worker(db, client).run()["wrote"]
    assert text in client.requests[0][0][-1]["content"]


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


# ── demand ──────────────────────────────────────────────────────────


class DemandTests(unittest.TestCase):
    """Pressure tracks how far the note has drifted from the thread."""

    def test_no_redraft_owed_is_zero(self) -> None:
        note = _Note("t", "n", 60, _now().isoformat())
        db = _FakeDB(count=70, note=note)
        w = _make_worker(db, _FakeClient(_GOOD_REPLY))
        signal = w.demand(now=_now(), last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)

    def test_pressure_grows_with_untracked_messages(self) -> None:
        near = _make_worker(
            _FakeDB(count=70, note=_Note("t", "n", 20, _now().isoformat())),
            _FakeClient(_GOOD_REPLY),
        ).demand(now=_now(), last_run_at=None)
        far = _make_worker(
            _FakeDB(count=200, note=_Note("t", "n", 20, _now().isoformat())),
            _FakeClient(_GOOD_REPLY),
        ).demand(now=_now(), last_run_at=None)
        self.assertGreater(far.pressure, near.pressure)
        self.assertTrue(far.needs_llm)

    def test_spent_budget_reports_nothing(self) -> None:
        db = _FakeDB(count=20, note=None)
        w = _make_worker(db, _FakeClient(_GOOD_REPLY),
                         limiter=_FakeRateLimiter(hour_used=6))
        self.assertEqual(w.demand(now=_now(), last_run_at=None).pressure, 0.0)

    def test_probe_neither_spends_a_token_nor_writes(self) -> None:
        db = _FakeDB(count=20, note=None)
        limiter = _FakeRateLimiter()
        client = _FakeClient(_GOOD_REPLY)
        w = _make_worker(db, client, limiter=limiter)
        w.demand(now=_now(), last_run_at=None)
        w.is_ready(now=_now(), last_run_at=None)
        self.assertEqual(limiter.allow_calls, 0)
        self.assertEqual(client.surfaces, [])
        self.assertEqual(db.saved, [])


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

    def test_packs_whole_latest_messages_against_the_worker_route(self) -> None:
        from app.llm.token_utils import estimate_tokens

        latest = "A longer account. " * 20 + "\nBut that was only a hypothetical."
        db = _FakeDB(
            count=40, summary="OLD SUMMARY " * 65,
            note=_Note("older", "OLD NOTE " * 54, 20, "2026-01-01T00:00:00+00:00"),
            messages=[
                _Msg("assistant", "OMIT THIS OLD MESSAGE " * 80),
                _Msg("assistant", "Second newest message."),
                _Msg("user", latest),
            ],
        )
        client = _FakeClient(_GOOD_REPLY)
        window = 900
        worker = _make_worker(db, client, model_context=lambda: ("active-worker", window))
        self.assertTrue(worker.run()["wrote"])
        messages, options, model = client.requests[0]
        self.assertEqual(model, "active-worker")
        prompt = messages[-1]["content"]
        self.assertIn(latest, prompt)
        self.assertLess(prompt.index("Second newest message."), prompt.index(latest))
        self.assertNotIn("OMIT THIS OLD MESSAGE", prompt)
        self.assertLessEqual(
            sum(estimate_tokens(m["content"]) + 4 for m in messages) + options["num_predict"] + 64,
            window,
        )

    def test_oversized_newest_message_does_not_charge_or_write(self) -> None:
        db = _FakeDB(count=20, messages=[_Msg("user", "A long message. " * 300)])
        client = _FakeClient(_GOOD_REPLY)
        limiter = _FakeRateLimiter()
        w = _make_worker(db, client, limiter=limiter, model_context=lambda: ("small", 768))
        self.assertEqual(w.run()["reason"], "prompt_budget")
        self.assertEqual(limiter.allow_calls, 0)
        self.assertEqual(client.requests, [])
        self.assertEqual(db.saved, [])

    def test_route_change_defers_the_prepared_prompt(self) -> None:
        db = _FakeDB(count=20, messages=self._msgs())
        client = _FakeClient(_GOOD_REPLY)
        routes = iter([("first", 4096), ("second", 2048)])
        w = _make_worker(db, client, model_context=lambda: next(routes))
        self.assertEqual(w.run()["reason"], "route_changed")
        self.assertEqual(client.requests, [])
        self.assertEqual(db.saved, [])

    def test_worker_gate_checks_route_after_waiting(self) -> None:
        from app.llm.llm_gate import GatedChatClient, MAINTENANCE_WORKER

        db = _FakeDB(count=20, messages=self._msgs())
        old_client = _FakeClient(_GOOD_REPLY)
        new_client = _FakeClient(_GOOD_REPLY)
        gate = Mock()
        client = GatedChatClient(old_client, gate, MAINTENANCE_WORKER)

        def retarget(*args, **kwargs):
            client.retarget(new_client, gate)
            return 0.0

        gate.acquire.side_effect = retarget
        worker = _make_worker(db, client, model_context=lambda: ("worker", 4096))
        self.assertEqual(worker.run()["reason"], "route_changed")
        self.assertEqual(old_client.requests + new_client.requests, [])
        gate.release.assert_called_once_with(MAINTENANCE_WORKER)

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
