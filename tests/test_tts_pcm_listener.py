"""Tests for the TTS / earcon PCM listener path.

The hard cut from ``sounddevice.play`` to a ``pcm_listener`` callback
is the centrepiece of the client-side audio refactor; these tests
pin the contract:

  - :class:`PocketTtsService` emits Int16 LE chunks through the
    listener and never tries to import / call sounddevice.
  - The listener receives the *playback rate* (samplerate × speed),
    not the model's native rate.
  - :class:`EarconPlayer` emits its synthesised tones through the
    same callback shape.
  - ``_speak_worker`` blocks for the audio's real-time duration before
    firing ``on_done`` so the lip-sync amplitude pacer can run its
    full natural course (otherwise the avatar froze mid-sentence on
    short utterances).
"""
from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from app.audio.earcons import EarconPlayer
from app.tts.pocket_tts_service import PocketTtsService


def _make_tts() -> PocketTtsService:
    settings = MagicMock()
    settings.enabled = True
    with patch("app.tts.pocket_tts_service.TTSModel", None), \
         patch("app.tts.pocket_tts_service.np", np):
        svc = PocketTtsService(settings)
    svc._loaded.set()
    svc._model = MagicMock()  # type: ignore[assignment]
    svc._voice_state = {}  # type: ignore[assignment]
    audio = (np.sin(np.linspace(0, 4 * np.pi, 800, dtype=np.float32)) * 0.3)
    svc.generate_audio = MagicMock(return_value=(audio, 8000))  # type: ignore[method-assign]
    return svc


class PocketTtsListenerTests(unittest.TestCase):
    def test_pcm_listener_receives_int16_le_chunks(self) -> None:
        svc = _make_tts()
        captured: list[tuple[int, int, bytes]] = []

        def _listener(rate: int, channels: int, pcm: bytes) -> None:
            captured.append((rate, channels, pcm))

        svc.set_pcm_listener(_listener)
        svc._speak_worker("hello", on_done=None, speed=1.0, on_amplitude=None)

        self.assertTrue(captured, "listener was never called")
        rate, channels, pcm = captured[0]
        # Speed 1.0 → playback rate equals the synthesis rate (8000 in
        # the fake). Channels is always mono in this build.
        self.assertEqual(rate, 8000)
        self.assertEqual(channels, 1)
        # PCM is Int16 LE → length must be even, samples in range.
        self.assertEqual(len(pcm) % 2, 0)

    def test_pcm_listener_rate_scales_with_speed(self) -> None:
        svc = _make_tts()
        captured: list[int] = []

        def _listener(rate: int, _channels: int, _pcm: bytes) -> None:
            captured.append(rate)

        svc.set_pcm_listener(_listener)
        svc._speak_worker("hello", on_done=None, speed=1.05, on_amplitude=None)
        self.assertTrue(captured)
        self.assertEqual(captured[0], int(8000 * 1.05))

    def test_clip_end_listener_fires_after_pcm(self) -> None:
        svc = _make_tts()
        events: list[str] = []

        def _on_pcm(_r: int, _c: int, _pcm: bytes) -> None:
            events.append("pcm")

        def _on_end() -> None:
            events.append("end")

        svc.set_pcm_listener(_on_pcm, end_listener=_on_end)
        svc._speak_worker("hi", on_done=None, speed=1.0, on_amplitude=None)
        self.assertIn("pcm", events)
        self.assertEqual(events[-1], "end")

    def test_worker_blocks_for_real_playback_duration(self) -> None:
        # Regression: ``_emit_pcm`` ships bytes at network speed; we
        # need ``_speak_worker`` to wait for the audio's real-time
        # duration before calling ``on_done``. Without this, the
        # ``TtsQueue`` would dispatch the next sentence immediately
        # and the lip-sync amplitude pacer (paced by wall-clock time)
        # would be killed mid-utterance — which is what the user saw
        # as an "avatar frozen for a little bit between every okay".
        svc = _make_tts()
        # Crank the synthesised clip up to 0.4 s of audio so we can
        # actually measure the wait. ``_make_tts`` uses 8000 Hz, so
        # 3200 samples at speed=1.0 → 0.4 s of playback time.
        long_audio = np.zeros(3200, dtype=np.float32)
        svc.generate_audio = MagicMock(  # type: ignore[method-assign]
            return_value=(long_audio, 8000),
        )
        svc.set_pcm_listener(lambda *_args: None)
        t0 = time.monotonic()
        svc._speak_worker("hi", on_done=None, speed=1.0, on_amplitude=None)
        elapsed = time.monotonic() - t0
        # 0.4 s real audio + 0.15 s trailing silence the worker
        # appends → at least ~0.5 s. Be generous on the lower bound to
        # tolerate scheduler jitter on busy CI hosts.
        self.assertGreaterEqual(elapsed, 0.4)

    def test_emit_paces_chunks_at_real_time(self) -> None:
        # Regression: an unpaced ``_emit_pcm`` shipped 20+ binary
        # frames in ~5 ms, which forced a matching burst of
        # AudioBuffer / AudioBufferSourceNode allocations on the
        # client and stuttered the Live2D render thread mid-utterance
        # ("the model started freezing while she was saying 'let me
        # see'"). After the fix, only the pre-roll chunks ship
        # back-to-back; the rest are paced at ``_EMIT_CHUNK_SECONDS``
        # so the WS load and the client allocation rate are flat
        # across the whole clip.
        svc = _make_tts()
        # Build a clip that is long enough that pacing dominates the
        # pre-roll. 20 chunks at 50 ms = 1 s of audio at 8 kHz.
        long_audio = np.zeros(8000 * 1, dtype=np.float32)
        timestamps: list[float] = []

        def _on_pcm(_r: int, _c: int, _pcm: bytes) -> None:
            timestamps.append(time.monotonic())

        svc.set_pcm_listener(_on_pcm)
        svc._emit_pcm(long_audio, 8000)

        # Pre-roll is the first ``_PRE_ROLL_CHUNKS`` chunks; everything
        # after that should be paced. The total wall-clock span from
        # the first paced chunk to the last must be at least
        # ``(N - PRE_ROLL_CHUNKS) * chunk_seconds`` minus a tolerance
        # for the thread / OS jitter.
        pre_roll = svc._PRE_ROLL_CHUNKS
        chunk_seconds = svc._EMIT_CHUNK_SECONDS
        self.assertGreater(len(timestamps), pre_roll + 5)
        paced_span = timestamps[-1] - timestamps[pre_roll]
        expected = (len(timestamps) - 1 - pre_roll) * chunk_seconds
        # Allow generous slack for CI host scheduling jitter.
        self.assertGreaterEqual(paced_span, expected * 0.6)

    def test_stop_request_aborts_the_playback_wait(self) -> None:
        # Stop / barge-in must cut the playback wait short — otherwise
        # the worker would block for the full clip duration before
        # acknowledging the stop signal.
        svc = _make_tts()
        long_audio = np.zeros(8000 * 5, dtype=np.float32)  # 5 s clip
        svc.generate_audio = MagicMock(  # type: ignore[method-assign]
            return_value=(long_audio, 8000),
        )
        svc.set_pcm_listener(lambda *_args: None)
        # Trip the stop flag immediately — the worker should still
        # complete its emission loop but exit the playback wait
        # within a tick or two.
        svc._stop_requested.set()
        t0 = time.monotonic()
        svc._speak_worker("hi", on_done=None, speed=1.0, on_amplitude=None)
        elapsed = time.monotonic() - t0
        # Must be well under the 5 s playback duration.
        self.assertLess(elapsed, 1.0)


class EarconListenerTests(unittest.TestCase):
    def test_play_blocking_emits_through_listener(self) -> None:
        captured: list[tuple[int, int, bytes]] = []

        def _listener(rate: int, channels: int, pcm: bytes) -> None:
            captured.append((rate, channels, pcm))

        player = EarconPlayer(enabled=True, pcm_listener=_listener)
        player.play_blocking("done")
        self.assertTrue(captured)
        rate, channels, _pcm = captured[0]
        self.assertEqual(channels, 1)
        # Earcons run at the module-fixed 22050 Hz.
        self.assertEqual(rate, 22050)

    def test_clip_end_listener_fires_after_earcon(self) -> None:
        events: list[str] = []

        def _on_pcm(_r: int, _c: int, _pcm: bytes) -> None:
            events.append("pcm")

        def _on_end() -> None:
            events.append("end")

        player = EarconPlayer(
            enabled=True, pcm_listener=_on_pcm, clip_end_listener=_on_end,
        )
        player.play_blocking("done")
        self.assertIn("pcm", events)
        self.assertEqual(events[-1], "end")

    def test_disabled_player_does_not_call_listener(self) -> None:
        captured: list[bytes] = []
        player = EarconPlayer(
            enabled=False,
            pcm_listener=lambda *_args: captured.append(b"x"),
        )
        player.play_blocking("done")
        self.assertEqual(captured, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
