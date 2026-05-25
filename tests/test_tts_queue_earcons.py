"""Tests for the Phase 1c earcon-splicing in :class:`TtsQueue`.

We don't load a real TTS engine; instead we hand-build a tiny stub
that records what it receives and call ``_on_done`` synchronously to
keep the test deterministic. The earcon player is also a stub that
records ``play_blocking`` calls.
"""
from __future__ import annotations

import threading
import time
import unittest

from app.core.tts_queue import TtsQueue


class _StubTtsEngine:
    """Minimal substitute for PocketTtsService used by the queue."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def speak_async(
        self,
        text,
        reaction=None,
        on_done=None,
        on_amplitude=None,
        speed=None,
    ):
        self.calls.append({
            "text": text, "reaction": reaction, "speed": speed,
        })
        # Fire the done callback synchronously so the queue advances
        # without us having to coordinate threads.
        if on_done is not None:
            on_done()

    def stop(self) -> None:
        pass

    def reaction_to_speed(self, reaction):
        return 1.0


class _StubEarconPlayer:
    """Records ``play_blocking`` calls; behaves as if always enabled."""

    def __init__(self) -> None:
        self.played: list[str] = []
        self.enabled = True

    def play_blocking(self, kind: str) -> None:
        self.played.append(kind)


class _OrderRecorder:
    """Helper that records the global order of TTS / earcon events
    across both stubs by stamping each event with a monotonically
    increasing counter. Lets tests assert "earcon happened between
    these two text chunks"."""

    def __init__(self) -> None:
        self._counter = 0
        self.events: list[tuple[int, str, str]] = []
        self._lock = threading.Lock()

    def stamp(self, kind: str, content: str) -> None:
        with self._lock:
            self._counter += 1
            self.events.append((self._counter, kind, content))


class _OrderedTtsEngine(_StubTtsEngine):
    def __init__(self, recorder: _OrderRecorder) -> None:
        super().__init__()
        self._recorder = recorder

    def speak_async(
        self,
        text,
        reaction=None,
        on_done=None,
        on_amplitude=None,
        speed=None,
    ):
        self._recorder.stamp("text", text)
        super().speak_async(
            text, reaction=reaction, on_done=on_done,
            on_amplitude=on_amplitude, speed=speed,
        )


class _OrderedEarconPlayer(_StubEarconPlayer):
    def __init__(self, recorder: _OrderRecorder) -> None:
        super().__init__()
        self._recorder = recorder

    def play_blocking(self, kind: str) -> None:
        self._recorder.stamp("earcon", kind)
        super().play_blocking(kind)


class EnqueueEarconTests(unittest.TestCase):
    def test_earcon_enqueued_and_played(self) -> None:
        engine = _StubTtsEngine()
        player = _StubEarconPlayer()
        q = TtsQueue(engine, earcon_player=player)
        q.enqueue_earcon("laugh")
        # Earcon plays on a background thread; wait briefly.
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not player.played:
            time.sleep(0.01)
        self.assertEqual(player.played, ["laugh"])

    def test_earcon_drops_silently_when_no_player(self) -> None:
        engine = _StubTtsEngine()
        q = TtsQueue(engine, earcon_player=None)
        q.enqueue_earcon("laugh")
        # Should not raise, should not call the engine.
        self.assertEqual(engine.calls, [])

    def test_earcon_drops_silently_when_disabled_player(self) -> None:
        engine = _StubTtsEngine()
        player = _StubEarconPlayer()
        player.enabled = False
        q = TtsQueue(engine, earcon_player=player)
        q.enqueue_earcon("laugh")
        time.sleep(0.05)
        self.assertEqual(player.played, [])

    def test_text_then_earcon_then_text_plays_in_order(self) -> None:
        recorder = _OrderRecorder()
        engine = _OrderedTtsEngine(recorder)
        player = _OrderedEarconPlayer(recorder)
        q = TtsQueue(engine, earcon_player=player)

        q.enqueue("Yeah", reaction="warm")
        q.enqueue_earcon("laugh")
        q.enqueue("right", reaction="warm")

        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and len(recorder.events) < 3:
            time.sleep(0.01)
        kinds = [e[1] for e in recorder.events]
        self.assertEqual(kinds, ["text", "earcon", "text"])
        # Confirm the text content too.
        contents = [e[2] for e in recorder.events]
        self.assertEqual(contents[0], "Yeah")
        self.assertEqual(contents[1], "laugh")
        self.assertEqual(contents[2], "right")


if __name__ == "__main__":
    unittest.main()
