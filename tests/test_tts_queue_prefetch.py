"""The prefetch that hides synthesis behind playback.

Both engines produce a whole clip before emitting a byte, so the queue
pre-generates the next sentence while the current chunk plays. Three
separate defects meant that never actually happened in a real turn, and
each is pinned here because each was individually silent -- the feature
looked present at every level and delivered nothing:

  * The prefetch peeked at exactly one chunk and gave up unless it was
    text. The cadence layer brackets sentences with ``pause_before`` /
    ``pause_after`` silences, so in practice the next chunk was a pause
    and the prefetch never ran.
  * Chatterbox had no clip cache, so its prefetch synthesised and dropped
    the result, and playback then synthesised the same sentence again
    behind the sidecar's single-request pipe -- strictly worse than not
    prefetching.
  * The cache key included speed, which the prefetch guessed from the
    cadence hint while playback pinned it to 1.0 under the runtime speed
    gate. The two disagreed on any sentence carrying prosody, so the
    entry was never read.

The invariant that covers all three: **one synthesis per sentence, and
the sentence being spoken is never delayed by the next one.**
"""
from __future__ import annotations

import threading
import time
import unittest

from app.core.voice.tts_queue import TtsQueue
from app.tts.clip_cache import ClipCache, SynthesisGate


class _RecordingEngine:
    """Engine shaped like the real ones: synthesise fully, then play.

    Synthesis is serialised on one lock, as it is for both real engines
    (pocket-tts holds the model lock; Chatterbox's sidecar takes one
    request at a time), which is what makes prefetch ordering matter.
    """

    def __init__(
        self, synth_seconds: float = 0.05, play_seconds: float = 0.25,
    ) -> None:
        self.synth_seconds = synth_seconds
        self.play_seconds = play_seconds
        self.synthesised: list[str] = []
        self.played: list[str] = []
        self.order: list[str] = []
        self._pipe = threading.Lock()
        self._cache = ClipCache()
        self._gate = SynthesisGate()
        self._lock = threading.Lock()

    def _mark(self, event: str) -> None:
        with self._lock:
            self.order.append(event)

    def _synthesise(self, text: str) -> str:
        with self._pipe:
            with self._lock:
                self.synthesised.append(text)
                self.order.append(f"synth-start:{text}")
            time.sleep(self.synth_seconds)
        self._mark(f"synth-end:{text}")
        return text

    def _warm(self, text: str):
        return self._cache.warm(
            ClipCache.key(text), lambda: self._synthesise(text),
        )

    def generate_audio(self, text: str, speed: float = 1.0, *, temp=None):
        """The prefetch entry point, mirroring both engines."""
        if not self._gate.wait_for_idle(timeout=5.0):
            return None
        return self._warm(text)

    def speak_async(self, text, reaction=None, on_done=None,
                    on_amplitude=None, *, speed=None, gain_db=0.0):
        self._gate.claim()

        def worker() -> None:
            try:
                self._warm(text)
            finally:
                self._gate.release()
            self._cache.discard(ClipCache.key(text))
            with self._lock:
                self.played.append(text)
                self.order.append(f"play-start:{text}")
            # Real playback occupies wall-clock time; that window is
            # exactly what the prefetch exists to fill.
            time.sleep(self.play_seconds)
            self._mark(f"play-end:{text}")
            if on_done:
                on_done()

        threading.Thread(target=worker, daemon=True).start()

    def speak_silence_async(self, ms: int, on_done=None) -> None:
        def worker() -> None:
            time.sleep(ms / 1000.0)
            if on_done:
                on_done()

        threading.Thread(target=worker, daemon=True).start()

    def reaction_to_speed(self, reaction) -> float:
        # A non-unity hint is the common case, and it used to poison the
        # cache key. Keep it non-unity so a regression there shows up.
        return 1.07 if reaction else 1.0

    def stop(self) -> None:
        pass


def _drain(queue: TtsQueue, timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while queue.is_active() and time.monotonic() < deadline:
        time.sleep(0.005)
    # Let the last worker's bookkeeping land.
    time.sleep(0.05)


class PrefetchAcrossPausesTests(unittest.TestCase):
    def test_a_pause_between_sentences_does_not_block_the_prefetch(self) -> None:
        # The original bug, and the one that mattered most: with a pause
        # between two sentences, the prefetch looked at the pause, saw it
        # wasn't text, and gave up -- so every sentence in a real turn was
        # synthesised cold while the listener waited.
        engine = _RecordingEngine()
        queue = TtsQueue(engine)
        queue.enqueue("First sentence.", reaction="warm", speed=1.07)
        queue.enqueue_silence(120)
        queue.enqueue("Second sentence.", reaction="warm", speed=1.07)
        _drain(queue)

        self.assertEqual(engine.played, ["First sentence.", "Second sentence."])
        # The second clip has to be *ready* before the first stops
        # playing. That is the whole difference between a natural
        # boundary and a synthesis-length hole in the middle of a reply.
        self.assertLess(
            engine.order.index("synth-end:Second sentence."),
            engine.order.index("play-end:First sentence."),
            f"prefetch did not run across the pause: {engine.order}",
        )

    def test_each_sentence_is_synthesised_exactly_once(self) -> None:
        # Chatterbox's missing cache and the speed-poisoned key both
        # showed up here: the prefetch did the work, playback could not
        # find it, and the sentence was generated a second time.
        engine = _RecordingEngine()
        queue = TtsQueue(engine)
        for text in ("One.", "Two.", "Three."):
            queue.enqueue(text, reaction="warm", speed=1.07)
            queue.enqueue_silence(60)
        _drain(queue)

        self.assertEqual(engine.played, ["One.", "Two.", "Three."])
        self.assertEqual(
            sorted(engine.synthesised),
            ["One.", "Three.", "Two."],
            f"a sentence was synthesised more than once: {engine.synthesised}",
        )

    def test_one_prefetch_thread_per_sentence(self) -> None:
        # A pause dispatches several chunks that all see the same next
        # sentence. Without de-duplication each spawns its own prefetch
        # and they contend for the one engine.
        engine = _RecordingEngine()
        queue = TtsQueue(engine)
        queue.enqueue("Alpha.", reaction="warm")
        queue.enqueue_silence(40)
        queue.enqueue_silence(40)
        queue.enqueue_silence(40)
        queue.enqueue("Beta.", reaction="warm")
        _drain(queue)

        self.assertEqual(engine.synthesised.count("Beta."), 1)

    def test_prefetch_reaches_a_sentence_behind_an_earcon(self) -> None:
        engine = _RecordingEngine()
        queue = TtsQueue(engine)
        queue.enqueue("Before.", reaction="warm")
        queue.enqueue_earcon("breath")
        queue.enqueue("After.", reaction="warm")
        _drain(queue)

        self.assertEqual(engine.played, ["Before.", "After."])
        self.assertEqual(engine.synthesised.count("After."), 1)


class SpeakingSentenceHasPriorityTests(unittest.TestCase):
    def test_a_prefetch_never_delays_the_sentence_being_spoken(self) -> None:
        # Making the prefetch fire introduced a new way to be slow: the
        # prefetch of sentence 2 could claim the engine before sentence 1
        # asked for it, turning a mid-turn gap into a silence at the very
        # top of the reply. The gate orders playback first.
        engine = _RecordingEngine(synth_seconds=0.3)
        queue = TtsQueue(engine)
        queue.enqueue("Speak me now.", reaction="warm")
        queue.enqueue_silence(50)
        queue.enqueue("Later.", reaction="warm")

        deadline = time.monotonic() + 5.0
        while not engine.order and time.monotonic() < deadline:
            time.sleep(0.005)
        _drain(queue)

        self.assertEqual(
            engine.order[0],
            "synth-start:Speak me now.",
            f"the prefetch got in front of the spoken sentence: {engine.order}",
        )

    def test_syntheses_start_in_sentence_order(self) -> None:
        # The strong form of the same invariant, and the one that caught
        # the ordering bug in the queue: with the prefetch spawned before
        # the engine had staked its claim, it slipped in front and the
        # sentence the listener was waiting on was synthesised second.
        # Since prefetching is one sentence deep and always yields to
        # playback, synthesis order must equal sentence order.
        engine = _RecordingEngine(synth_seconds=0.15, play_seconds=0.2)
        queue = TtsQueue(engine)
        for text in ("One.", "Two.", "Three.", "Four."):
            queue.enqueue(text, reaction="warm", speed=1.07)
            queue.enqueue_silence(50)
        _drain(queue)

        self.assertEqual(engine.synthesised, ["One.", "Two.", "Three.", "Four."])

    def test_stop_clears_the_dedup_marker(self) -> None:
        # Otherwise the sentence prefetched at the moment of a barge-in
        # would be skipped by the prefetch of the *next* turn, silently
        # costing that turn its head start.
        engine = _RecordingEngine()
        queue = TtsQueue(engine)
        queue.enqueue("Interrupted.", reaction="warm")
        queue.enqueue_silence(40)
        queue.enqueue("Dropped.", reaction="warm")
        queue.stop()
        _drain(queue)

        engine.synthesised.clear()
        engine.order.clear()
        queue.enqueue("Fresh.", reaction="warm")
        queue.enqueue_silence(40)
        queue.enqueue("Dropped.", reaction="warm")
        _drain(queue)

        self.assertLess(
            engine.order.index("synth-end:Dropped."),
            engine.order.index("play-end:Fresh."),
            f"the stale marker suppressed the prefetch: {engine.order}",
        )


class ClipCacheTests(unittest.TestCase):
    def test_speed_is_not_part_of_the_key(self) -> None:
        # Speed is applied by the time-stretch at emission, so the clip
        # is speed-independent. Keying on it meant the prefetch's guessed
        # speed and playback's pinned 1.0 never met.
        self.assertEqual(ClipCache.key("hello"), ClipCache.key("hello"))
        self.assertNotEqual(ClipCache.key("hello"), ClipCache.key("hello", 0.7))

    def test_a_second_caller_waits_instead_of_generating_again(self) -> None:
        # The case a plain dict gets wrong: playback arriving while the
        # prefetch is still in flight sees no entry, calls it a miss, and
        # starts a duplicate synthesis of the same text.
        cache = ClipCache()
        calls: list[str] = []
        started = threading.Event()
        release = threading.Event()

        def slow() -> str:
            calls.append("gen")
            started.set()
            release.wait(timeout=5.0)
            return "clip"

        first: list[str] = []
        thread = threading.Thread(
            target=lambda: first.append(cache.warm("k", slow)), daemon=True,
        )
        thread.start()
        self.assertTrue(started.wait(timeout=5.0))

        second: list[str] = []
        waiter = threading.Thread(
            target=lambda: second.append(cache.warm("k", slow)), daemon=True,
        )
        waiter.start()
        time.sleep(0.05)
        self.assertEqual(len(calls), 1, "the second caller started its own synthesis")

        release.set()
        thread.join(timeout=5.0)
        waiter.join(timeout=5.0)
        self.assertEqual(first, ["clip"])
        self.assertEqual(second, ["clip"])
        self.assertEqual(len(calls), 1)

    def test_a_failed_generation_is_not_cached(self) -> None:
        cache = ClipCache()
        self.assertIsNone(cache.warm("k", lambda: None))
        self.assertEqual(cache.pending(), 0)
        self.assertEqual(cache.warm("k", lambda: "clip"), "clip")

    def test_a_raising_factory_releases_the_key(self) -> None:
        # A wedged in-flight marker would make every later caller for
        # that text wait out the full timeout.
        cache = ClipCache()

        def boom() -> str:
            raise RuntimeError("engine died")

        with self.assertRaises(RuntimeError):
            cache.warm("k", boom)
        self.assertEqual(cache.warm("k", lambda: "clip"), "clip")

    def test_entries_are_bounded(self) -> None:
        cache = ClipCache(limit=2)
        for i in range(5):
            cache.warm(f"k{i}", lambda i=i: f"clip{i}")
        self.assertEqual(cache.pending(), 2)

    def test_discard_drops_the_entry(self) -> None:
        cache = ClipCache()
        cache.warm("k", lambda: "clip")
        self.assertEqual(cache.pending(), 1)
        cache.discard("k")
        self.assertEqual(cache.pending(), 0)


class SynthesisGateTests(unittest.TestCase):
    def test_idle_by_default(self) -> None:
        self.assertTrue(SynthesisGate().wait_for_idle(timeout=0.1))

    def test_a_claim_holds_a_waiter_until_released(self) -> None:
        gate = SynthesisGate()
        gate.claim()
        self.assertFalse(gate.wait_for_idle(timeout=0.05))
        gate.release()
        self.assertTrue(gate.wait_for_idle(timeout=0.5))

    def test_release_without_claim_does_not_wedge_the_gate(self) -> None:
        # A stray release must not drive the count negative, which would
        # keep the gate shut for every subsequent claim.
        gate = SynthesisGate()
        gate.release()
        gate.claim()
        gate.release()
        self.assertTrue(gate.wait_for_idle(timeout=0.5))


if __name__ == "__main__":
    unittest.main()
