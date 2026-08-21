"""A cut clip must tell the client to drop what it is still holding.

The bug this covers is inaudible in any single-layer test, which is why
it survived so long. Each half is individually correct:

* the engine ships ``_PRE_ROLL_CHUNKS`` ahead of real time, so the
  scheduler on the client never underruns;
* the client refuses to flush on ``audio_end``, so consecutive sentences
  chain instead of clipping each other short.

Together they mean a cut clip leaves the client holding ~250 ms of speech
that the server has stopped sending and nothing will ever retract. It
plays -- a fragment after the sentence, on any engine, because the
pre-roll lives in the shared mixin.

So the assertions here are about the *pairing*: a natural end must not
cancel (or every sentence loses its tail) and a cut must (or the pre-roll
is heard).
"""
from __future__ import annotations

import threading
import unittest

import numpy as np

from app.tts.pcm_playback import PcmPlaybackMixin
from app.web import audio_frames as frames


class Engine(PcmPlaybackMixin):
    """The smallest host satisfying the mixin's contract."""

    def __init__(self) -> None:
        self.chunks: list[bytes] = []
        self.events: list[str] = []
        self._stop_requested = threading.Event()
        self._pitch_preserving_speed = False
        self._pcm_listener = self._collect
        self._clip_end_listener = lambda: self.events.append("end")
        self._clip_cancel_listener = lambda: self.events.append("cancel")

    def _collect(self, rate: int, channels: int, pcm: bytes) -> None:
        self.chunks.append(pcm)


def tone(seconds: float, rate: int = 24000) -> np.ndarray:
    t = np.arange(int(rate * seconds), dtype=np.float32) / rate
    return (0.2 * np.sin(2 * np.pi * 220.0 * t)).astype(np.float32)


class CutCancelsTests(unittest.TestCase):
    def test_a_finished_clip_does_not_cancel(self) -> None:
        # The whole reason cancel is a separate frame. If a natural end
        # cancelled, every sentence would lose its tail to the next one.
        engine = Engine()
        engine._emit_pcm(tone(0.2), 24000)
        self.assertEqual(engine.events, ["end"])

    def test_a_clip_cut_before_it_starts_cancels(self) -> None:
        engine = Engine()
        engine._stop_requested.set()
        engine._emit_pcm(tone(2.0), 24000)
        self.assertEqual(engine.chunks, [])
        # Cancel first: the client should drop the queue before anything
        # else reacts to the clip being over.
        self.assertEqual(engine.events, ["cancel", "end"])

    def test_a_clip_cut_midway_cancels_what_was_pre_rolled(self) -> None:
        engine = Engine()

        # A long clip, stopped once the pre-roll is out. That pre-roll is
        # exactly the audio the client would otherwise still play.
        def cut() -> None:
            while len(engine.chunks) < engine._PRE_ROLL_CHUNKS:
                pass
            engine._stop_requested.set()

        watcher = threading.Thread(target=cut, daemon=True)
        watcher.start()
        engine._emit_pcm(tone(4.0), 24000)
        watcher.join(timeout=5.0)

        self.assertIn("cancel", engine.events)
        self.assertEqual(engine.events[0], "cancel")
        # Well short of the whole clip: the point is that a cut leaves
        # audio shipped but unheard.
        shipped = sum(len(c) for c in engine.chunks) / 2 / 24000
        self.assertLess(shipped, 4.0)

    def test_an_engine_without_a_cancel_listener_still_works(self) -> None:
        # Engines predating the frame must keep playing, not raise.
        engine = Engine()
        engine._clip_cancel_listener = None
        engine._stop_requested.set()
        engine._emit_pcm(tone(1.0), 24000)
        self.assertEqual(engine.events, ["end"])

    def test_a_raising_cancel_listener_does_not_break_playback(self) -> None:
        engine = Engine()

        def boom() -> None:
            raise RuntimeError("listener went away mid-barge-in")

        engine._clip_cancel_listener = boom
        engine._stop_requested.set()
        engine._emit_pcm(tone(1.0), 24000)
        self.assertEqual(engine.events, ["end"])


class FrameTests(unittest.TestCase):
    def test_cancel_is_its_own_frame_byte(self) -> None:
        # Over-the-wire contract, mirrored in web/src/audio/protocol.ts.
        self.assertEqual(frames.FRAME_AUDIO_CANCEL, 0x14)
        self.assertNotEqual(frames.FRAME_AUDIO_CANCEL, frames.FRAME_AUDIO_END)

    def test_cancel_carries_the_stream_it_applies_to(self) -> None:
        built = frames.build_audio_cancel(frames.FRAME_TTS_PCM)
        self.assertEqual(built[0], frames.FRAME_AUDIO_CANCEL)
        self.assertEqual(frames.stream_name(built[1]), "tts")


if __name__ == "__main__":
    unittest.main()
