"""Phase 6: latency + speaking-window saturation regressions.

These tests are deliberately lightweight: they pin down the *rough order
of magnitude* of hot-path operations so a future regression that doubles
or 10x's the cost trips a test, without coupling to absolute wall-clock
budgets that depend on hardware. A small slack factor keeps the tests
green on slow CI runners while still catching real blowups.
"""
from __future__ import annotations

import random
import threading
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from app.core.affect.affect_state import AffectStore
from app.core.goals.agenda import AgendaStore, extract_inline_tags
from app.core.voice.cadence import (
    CadenceContext,
    ProsodyDispatcher,
    analyze_sentence,
)
from app.core.infra.chat_database import ChatDatabase
from app.core.affect.circadian import compute as circadian_compute
from app.core.conversation.conversation_arc import ArcEstimator, ArcStore
from app.core.proactive.prepared_nudge import PreparedNudgeStore
from app.core.memory.promise_extractor import extract_regex
from app.core.relationship.relationship import RelationshipStore, RelationshipTracker
from app.core.voice.speaking_window_scheduler import (
    ScheduledJob,
    SpeakingWindowScheduler,
)
from app.core.infra.user_profile import UserProfileStore
from app.core.affect.user_state import UserStateEstimator, UserStateStore


def _make_db() -> tuple[TemporaryDirectory, ChatDatabase]:
    tmp = TemporaryDirectory()
    db = ChatDatabase(Path(tmp.name) / "chat.db")
    return tmp, db


def _close(tmp: TemporaryDirectory, db: ChatDatabase) -> None:
    conn = getattr(db._local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        db._local.conn = None
    try:
        tmp.cleanup()
    except PermissionError:
        pass


# ── Latency regression ────────────────────────────────────────────────


class HotPathLatencyTests(unittest.TestCase):
    """Each test asserts that a 50-iteration burst stays under a budget.

    The budgets are deliberately loose (>= 5x the typical observed mean)
    so they don't flake on a slow runner; the goal is to catch a real
    regression of 10x+.
    """

    def test_user_state_estimator_under_budget(self):
        tmp, db = _make_db()
        try:
            store = UserStateStore(db)
            est = UserStateEstimator(store)
            t0 = time.perf_counter()
            for i in range(50):
                est.apply_turn(
                    "u1",
                    user_text=f"I'm so tired today {i}, working on the deploy",
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(
                elapsed_ms, 600.0,
                f"UserStateEstimator burst took {elapsed_ms:.1f}ms",
            )
        finally:
            _close(tmp, db)

    def test_arc_estimator_under_budget(self):
        tmp, db = _make_db()
        try:
            store = ArcStore(db)
            est = ArcEstimator(store)
            samples = [
                "Let's plan the migration step by step",
                "I'm exhausted, this week was rough",
                "Why is the deploy crashing again?",
                "haha that was hilarious",
                "I've been thinking about the architecture",
            ]
            t0 = time.perf_counter()
            for i in range(50):
                est.apply_turn(
                    "u1",
                    user_text=samples[i % len(samples)],
                    current_turn=i,
                )
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(
                elapsed_ms, 600.0,
                f"ArcEstimator burst took {elapsed_ms:.1f}ms",
            )
        finally:
            _close(tmp, db)

    def test_promise_extract_regex_under_budget(self):
        sample_user = "I'll get back to you tomorrow about the deploy."
        sample_assistant = "I'll send you a follow-up note tonight."
        t0 = time.perf_counter()
        for _ in range(200):
            extract_regex(user_text=sample_user, assistant_text=sample_assistant)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(
            elapsed_ms, 200.0,
            f"PromiseExtractor.extract_regex burst took {elapsed_ms:.1f}ms",
        )

    def test_agenda_inline_tags_under_budget(self):
        text = (
            "Sure thing! [[agenda:0.7:rebuild the docs site]] and "
            "[[agenda:read the new paper]] sound good." * 4
        )
        t0 = time.perf_counter()
        for _ in range(200):
            extract_inline_tags(text)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(
            elapsed_ms, 200.0,
            f"agenda.extract_inline_tags burst took {elapsed_ms:.1f}ms",
        )

    def test_cadence_analyze_sentence_under_budget(self):
        ctx = CadenceContext(
            base_reaction="neutral",
            mood_label="content",
            mood_arousal=0.4,
            rng=random.Random(0),
        )
        samples = [
            "Are you alright?",
            "Oh! That actually surprised me.",
            "I think we should probably try the other route.",
            "haha okay that's a good point",
            "Yeah, well…",
        ]
        t0 = time.perf_counter()
        for i in range(200):
            analyze_sentence(samples[i % len(samples)], ctx)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(
            elapsed_ms, 200.0,
            f"cadence.analyze_sentence burst took {elapsed_ms:.1f}ms",
        )

    def test_circadian_compute_under_budget(self):
        t0 = time.perf_counter()
        for _ in range(200):
            circadian_compute()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        self.assertLess(
            elapsed_ms, 200.0,
            f"circadian.compute burst took {elapsed_ms:.1f}ms",
        )


class InnerLifeBlockBurstTests(unittest.TestCase):
    """Render every inner-life block a 1000 times — total stays small."""

    def test_render_blocks_under_budget(self):
        tmp, db = _make_db()
        try:
            arc_store = ArcStore(db)
            arc_store.upsert("u1", arc="planning", since_turn=2, confidence=0.85)
            agenda = AgendaStore(db)
            agenda.add("u1", goal="ship the migration", importance=0.7)
            user_state_store = UserStateStore(db)
            UserStateEstimator(user_state_store).apply_turn(
                "u1", user_text="getting things done today",
            )
            user_profile = UserProfileStore(db)
            relationship = RelationshipStore(db)
            tracker = RelationshipTracker(relationship)
            tracker.register_session_start("u1")
            for _ in range(5):
                tracker.record_turn("u1")
            # Touch the affect store once so the row exists; render_blocks
            # below won't actually use it but it's part of the inner life.
            AffectStore(db).get("u1")

            t0 = time.perf_counter()
            for _ in range(1000):
                arc_store.render_block("u1", current_turn=10)
                agenda.render_block("u1")
                user_state_store.render_block("u1")
                user_profile.render_block("u1")
            elapsed_ms = (time.perf_counter() - t0) * 1000.0
            self.assertLess(
                elapsed_ms, 1500.0,
                f"inner-life block render burst took {elapsed_ms:.1f}ms",
            )
        finally:
            _close(tmp, db)


# ── Speaking-window saturation ────────────────────────────────────────


class SaturationTests(unittest.TestCase):
    def test_drains_under_pressure_priority_order(self):
        sched = SpeakingWindowScheduler(speaking_window_grace_ms=10)
        try:
            order: list[int] = []
            lock = threading.Lock()

            def make_job(priority: int):
                def _run(_stop):
                    with lock:
                        order.append(priority)
                return _run

            # Submit 30 jobs across a few priorities.
            for i in range(30):
                pri = (10, 20, 30, 50, 70, 90)[i % 6]
                sched.submit(ScheduledJob(
                    name=f"j-{i}",
                    priority=pri,
                    estimated_seconds=0.001,
                    callable=make_job(pri),
                    dedupe_key=None,
                ))
            sched.on_tts_state("start")
            # Wait until all have run or timeout.
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if sched.snapshot()["pending"] == 0:
                    # Allow the in-flight job to flush.
                    time.sleep(0.05)
                    break
                time.sleep(0.02)
            sched.on_tts_state("end")
            time.sleep(0.05)
            # All ran (no flake on a normal CI box).
            self.assertEqual(len(order), 30)
            # Lower priorities run first overall (allow runs from the same
            # priority bucket to interleave because of equal sort keys).
            sorted_order = sorted(order)
            self.assertEqual(order, sorted_order)
        finally:
            sched.stop()

    def test_dedupe_keeps_only_latest(self):
        sched = SpeakingWindowScheduler(speaking_window_grace_ms=10)
        try:
            counter: dict[str, int] = {"runs": 0, "latest": -1}

            def make(idx: int):
                def _run(_stop):
                    counter["runs"] += 1
                    counter["latest"] = idx
                return _run

            for idx in range(10):
                # Same dedupe_key — only the last one should run.
                sched.submit(ScheduledJob(
                    name="dup",
                    priority=50,
                    estimated_seconds=0.001,
                    callable=make(idx),
                    dedupe_key="dup",
                ))
            sched.on_tts_state("start")
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline and sched.snapshot()["pending"] > 0:
                time.sleep(0.02)
            time.sleep(0.05)
            sched.on_tts_state("end")
            self.assertEqual(counter["runs"], 1)
            self.assertEqual(counter["latest"], 9)
            stats = sched.snapshot()
            self.assertGreaterEqual(int(stats["skipped_stale"]), 9)
        finally:
            sched.stop()

    def test_user_speech_cancels_in_flight(self):
        sched = SpeakingWindowScheduler(speaking_window_grace_ms=10)
        try:
            cancelled = threading.Event()
            started = threading.Event()

            def long_job(stop_flag):
                started.set()
                # Spin checking the stop flag for up to 2s.
                deadline = time.monotonic() + 2.0
                while time.monotonic() < deadline:
                    if stop_flag.is_set():
                        cancelled.set()
                        return
                    time.sleep(0.02)

            sched.submit(ScheduledJob(
                name="long",
                priority=10,
                estimated_seconds=2.0,
                callable=long_job,
                dedupe_key=None,
            ))
            sched.on_tts_state("start")
            self.assertTrue(started.wait(timeout=2.0))
            sched.on_user_speech()
            self.assertTrue(cancelled.wait(timeout=2.0))
        finally:
            sched.stop()

    def test_post_window_jobs_pick_up_in_next_window(self):
        sched = SpeakingWindowScheduler(speaking_window_grace_ms=10)
        try:
            ran: list[str] = []
            lock = threading.Lock()

            def quick(name: str):
                def _run(_stop):
                    with lock:
                        ran.append(name)
                return _run

            sched.submit(ScheduledJob(
                name="first",
                priority=10,
                estimated_seconds=0.001,
                callable=quick("first"),
                dedupe_key=None,
            ))
            sched.on_tts_state("start")
            sched.on_tts_state("end")
            time.sleep(0.05)
            # New job after the window closed.
            sched.submit(ScheduledJob(
                name="second",
                priority=10,
                estimated_seconds=0.001,
                callable=quick("second"),
                dedupe_key=None,
            ))
            sched.on_tts_state("start")
            time.sleep(0.05)
            sched.on_tts_state("end")
            # Both ran, in order.
            self.assertEqual(ran[:2], ["first", "second"])
        finally:
            sched.stop()


# ── Overall sanity: prepared-nudge fast path observable ───────────────


class PreparedNudgeFastPathTests(unittest.TestCase):
    def test_consume_clears_row(self):
        tmp, db = _make_db()
        try:
            store = PreparedNudgeStore(db)
            store.upsert("u1", text="hi there", source_kind="callback", ttl_seconds=120.0)
            self.assertIsNotNone(store.consume("u1"))
            self.assertIsNone(store.get("u1"))
        finally:
            _close(tmp, db)


# ── Smoke: ProsodyDispatcher under burst input ────────────────────────


class ProsodyDispatcherBurstTests(unittest.TestCase):
    def test_burst_dispatch_stays_fast(self):
        sent: list[tuple[str, str | None]] = []

        def enqueue(text: str, reaction=None):
            sent.append((text, reaction))

        d = ProsodyDispatcher(enqueue, rng=random.Random(0))
        sentences = [
            "Hey there.",
            "Are you sure?",
            "Oh! That works.",
            "Hmm, let me think about it…",
            "I think we should try it.",
            "haha that's amazing",
        ]
        t0 = time.perf_counter()
        for i in range(500):
            d.dispatch(sentences[i % len(sentences)], "neutral")
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        # 500 dispatches; budget 500ms (more than 5x typical observed).
        self.assertLess(
            elapsed_ms, 500.0,
            f"ProsodyDispatcher burst took {elapsed_ms:.1f}ms",
        )
        # Each dispatch produced 1-2 outputs.
        self.assertGreaterEqual(len(sent), 500)
        self.assertLessEqual(len(sent), 1000)


if __name__ == "__main__":
    unittest.main()
