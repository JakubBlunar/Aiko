"""Layer 2 tests: ``TtsQueue.enqueue_silence`` produces real timed gaps.

Covers:
  * Silence items advance the queue ordering correctly when interleaved
    with text and earcons.
  * Engines exposing ``speak_silence_async`` are preferred over the
    wall-clock fallback.
  * Bare engines (no ``speak_silence_async``) still get the queue to
    advance via the daemon-thread sleep fallback.
  * Out-of-band durations (negative, zero, NaN) are dropped silently.
  * The 1500 ms cap is honoured.
"""
from __future__ import annotations

import threading
import time
import unittest
from typing import Any

from app.core.voice.tts_queue import TtsQueue


class _FakeEngineFull:
    """Engine that exposes both ``speak_async`` and ``speak_silence_async``."""

    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str | None, float | None, float]] = []
        self.silence_calls: list[int] = []

    def speak_async(self, text, *, reaction=None, on_done=None,
                    on_amplitude=None, speed=None, gain_db=0.0) -> None:
        self.text_calls.append((text, reaction, speed, gain_db))
        if on_done is not None:
            on_done()

    def speak_silence_async(self, ms: int, on_done=None) -> None:
        self.silence_calls.append(int(ms))
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        pass


class _FakeEngineMinimal:
    """Engine without ``speak_silence_async`` -- exercises the fallback."""

    def __init__(self) -> None:
        self.text_calls: list[tuple[str, str | None, float | None]] = []

    def speak_async(self, text, *, reaction=None, on_done=None,
                    on_amplitude=None, speed=None) -> None:
        self.text_calls.append((text, reaction, speed))
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        pass


class EnqueueSilenceTests(unittest.TestCase):
    def test_engine_with_silence_helper_used_directly(self) -> None:
        engine = _FakeEngineFull()
        queue = TtsQueue(engine, enabled=True)
        queue.enqueue_silence(250)
        self.assertEqual(engine.silence_calls, [250])

    def test_silence_caps_at_max(self) -> None:
        engine = _FakeEngineFull()
        queue = TtsQueue(engine, enabled=True)
        queue.enqueue_silence(9999)
        self.assertEqual(engine.silence_calls, [TtsQueue._SILENCE_MAX_MS])

    def test_zero_and_negative_dropped(self) -> None:
        engine = _FakeEngineFull()
        queue = TtsQueue(engine, enabled=True)
        queue.enqueue_silence(0)
        queue.enqueue_silence(-100)
        self.assertEqual(engine.silence_calls, [])

    def test_invalid_dropped(self) -> None:
        engine = _FakeEngineFull()
        queue = TtsQueue(engine, enabled=True)
        queue.enqueue_silence("nope")  # type: ignore[arg-type]
        self.assertEqual(engine.silence_calls, [])

    def test_disabled_queue_drops_silence(self) -> None:
        engine = _FakeEngineFull()
        queue = TtsQueue(engine, enabled=False)
        queue.enqueue_silence(200)
        self.assertEqual(engine.silence_calls, [])

    def test_interleaves_with_text(self) -> None:
        """Pause-before / text / pause-after ordering must round-trip."""
        engine = _FakeEngineFull()
        queue = TtsQueue(engine, enabled=True)
        # Interleave: silence -> text -> silence -> text. Each ``on_done``
        # in the fake engine fires immediately, so the queue drains
        # synchronously through the dispatch chain.
        queue.enqueue_silence(120)
        queue.enqueue("hello there", reaction="warm")
        queue.enqueue_silence(80)
        queue.enqueue("how are you", reaction="warm")
        # Both texts arrived in order.
        self.assertEqual(
            [c[0] for c in engine.text_calls],
            ["hello there", "how are you"],
        )
        # Both silences arrived in order.
        self.assertEqual(engine.silence_calls, [120, 80])


class FallbackSilenceTests(unittest.TestCase):
    """When the engine has no ``speak_silence_async`` the queue uses
    a daemon thread + ``time.sleep`` so timing still lines up."""

    def test_fallback_advances_queue(self) -> None:
        engine = _FakeEngineMinimal()
        queue = TtsQueue(engine, enabled=True)
        # 50 ms silence -> wall-clock fallback fires _on_chunk_done.
        queue.enqueue_silence(50)
        queue.enqueue("after silence", reaction="neutral")
        # Wait briefly for the fallback thread to fire on_chunk_done.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline:
            if engine.text_calls:
                break
            time.sleep(0.01)
        self.assertEqual(
            [c[0] for c in engine.text_calls], ["after silence"],
        )


class CapConstantTests(unittest.TestCase):
    def test_cap_is_1500ms(self) -> None:
        # Documented in the plan; pin so a future change to the
        # cap is intentional and visible in code review.
        self.assertEqual(TtsQueue._SILENCE_MAX_MS, 1500)


class PocketSilenceWorkerTimingTests(unittest.TestCase):
    """Regression test for the silence-worker double-delay bug.

    Original implementation called ``_emit_pcm`` (which itself paces
    real-time after a 5-chunk pre-roll) AND THEN ``time.sleep`` for
    the full duration on top, doubling long pauses. This regression
    test pins the deadline-based wait so a future revert is loud.
    """

    def test_silence_worker_completes_in_single_duration(self) -> None:
        from unittest.mock import patch
        import time as _time

        from app.core.infra.settings import TtsSettings
        from app.tts import pocket_tts_service as pts_module
        from app.tts.pocket_tts_service import PocketTtsService

        settings = TtsSettings(
            provider="pocket-tts", voice="alba", enabled=True,
        )
        with patch.object(pts_module, "TTSModel", None):
            service = PocketTtsService(settings)

        # The service has no model loaded, so _emit_pcm is a noop
        # (no listener installed). The test only exercises the wait
        # logic in _silence_worker -- specifically that it doesn't
        # double-delay when ``_emit_pcm`` and the deadline-wait both
        # try to consume the same budget.
        target_ms = 200
        start = _time.monotonic()
        service._silence_worker(target_ms, on_done=None)
        elapsed_ms = (_time.monotonic() - start) * 1000.0
        # Allow some slop for scheduler jitter, but the actual elapsed
        # must NOT be roughly doubled (the original bug). We assert a
        # generous upper bound that still catches a 2x regression.
        self.assertLess(
            elapsed_ms,
            target_ms * 1.5 + 50,
            msg=(
                f"silence took {elapsed_ms:.0f}ms for a {target_ms}ms "
                "request -- double-delay regression"
            ),
        )
        # And it's not below the floor (we don't want it to fire
        # ``on_done`` early either).
        self.assertGreaterEqual(elapsed_ms, target_ms * 0.85)


if __name__ == "__main__":
    unittest.main()
