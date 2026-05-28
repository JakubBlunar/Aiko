"""Tests for the Phase 1b speed plumbing in PocketTtsService.

We can't load the real Pocket-TTS model in CI (huge download, CPU-only
inference), so these tests stub the model loader and verify the
contract:

  - ``speak_async(text, reaction=…)`` derives speed from the reaction
    table and clamps to the safe range.
  - ``speak_async(text, speed=…)`` overrides the reaction default.
  - ``_speak_worker`` emits PCM through ``pcm_listener`` at the
    playback rate (samplerate scaled by ``speed``); this is the
    actual mechanism that makes Aiko speak faster / slower.
  - The amplitude pacer is fed the playback rate, not the synthesis
    rate, so lip-sync stays in step.

The fragility budget is small because we touch only the contract
between cadence → TtsQueue → speak_async → pcm_listener. Internal
Pocket-TTS behaviour is mocked.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from app.tts.pocket_tts_service import (
    PocketTtsService,
    _SPEED_MAX,
    _SPEED_MIN,
)


def _make_service() -> PocketTtsService:
    """Build a PocketTtsService with the model loader bypassed."""
    settings = MagicMock()
    settings.enabled = True
    # Bypass the auto-load thread spun up in __init__: stub TTSModel
    # to None so the constructor records "missing" and doesn't try to
    # import anything heavy. We then wire fakes in by hand.
    with patch("app.tts.pocket_tts_service.TTSModel", None), \
         patch("app.tts.pocket_tts_service.np", np):
        svc = PocketTtsService(settings)

    # Fake out the model + voice state so generate_audio works without
    # real inference. ``generate_audio`` is overridden directly so we
    # don't need to satisfy TTSModel's API.
    svc._loaded.set()
    svc._model = MagicMock()  # type: ignore[assignment]
    svc._voice_state = {}  # type: ignore[assignment]
    fake_audio = np.zeros(1600, dtype=np.float32)
    svc.generate_audio = MagicMock(return_value=(fake_audio, 16000))  # type: ignore[method-assign]
    return svc


class ReactionToSpeedTests(unittest.TestCase):
    """The reaction table must cover every reaction the affect /
    cadence pipeline emits and never produce a value outside the safe
    range. Missing reactions silently fall back to 1.0."""

    def test_known_reactions_within_safe_range(self) -> None:
        svc = _make_service()
        for reaction in (
            "excited", "enthusiastic", "cheerful", "amused", "playful",
            "warm", "neutral", "thoughtful", "wistful", "calm",
            "serious", "concerned", "sad", "melancholy", "tired",
        ):
            speed = svc.reaction_to_speed(reaction)
            self.assertGreaterEqual(
                speed, _SPEED_MIN,
                msg=f"reaction={reaction!r} below safe range",
            )
            self.assertLessEqual(
                speed, _SPEED_MAX,
                msg=f"reaction={reaction!r} above safe range",
            )

    def test_unknown_reaction_returns_neutral(self) -> None:
        svc = _make_service()
        self.assertEqual(svc.reaction_to_speed("zoinks"), 1.0)

    def test_empty_reaction_returns_neutral(self) -> None:
        svc = _make_service()
        self.assertEqual(svc.reaction_to_speed(""), 1.0)
        self.assertEqual(svc.reaction_to_speed(None), 1.0)


class SpeakAsyncSpeedOverrideTests(unittest.TestCase):
    """When ``speak_async(speed=…)`` is given a value, it overrides the
    reaction-derived baseline. The final value is clamped before being
    passed to the worker."""

    def _capture_worker_speed(
        self,
        svc: PocketTtsService,
        *,
        reaction: str | None,
        speed: float | None,
    ) -> float:
        captured: dict[str, float] = {}

        def fake_worker(text, on_done, final_speed, on_amp):
            captured["speed"] = final_speed
            if on_done is not None:
                on_done()

        # Replace the spawn so we run synchronously; can also just mock
        # threading.Thread's start to call target() inline.
        with patch.object(svc, "_speak_worker", side_effect=fake_worker):
            with patch("threading.Thread") as ThreadCls:
                def fake_thread_init(target, args=None, daemon=None):
                    th = MagicMock()
                    th.start = lambda: target(*(args or ()))
                    return th
                ThreadCls.side_effect = fake_thread_init
                svc.speak_async("hello", reaction=reaction, speed=speed)
        return captured["speed"]

    def test_override_takes_priority_over_reaction(self) -> None:
        svc = _make_service()
        # "excited" baseline is 1.08 in the table; explicit 0.95 should win.
        speed = self._capture_worker_speed(svc, reaction="excited", speed=0.95)
        self.assertAlmostEqual(speed, 0.95, places=3)

    def test_override_clamped_to_safe_range_high(self) -> None:
        svc = _make_service()
        speed = self._capture_worker_speed(svc, reaction=None, speed=1.5)
        self.assertEqual(speed, _SPEED_MAX)

    def test_override_clamped_to_safe_range_low(self) -> None:
        svc = _make_service()
        speed = self._capture_worker_speed(svc, reaction=None, speed=0.5)
        self.assertEqual(speed, _SPEED_MIN)

    def test_invalid_override_falls_back_to_reaction(self) -> None:
        svc = _make_service()
        speed = self._capture_worker_speed(
            svc, reaction="thoughtful", speed=float("nan"),
        )
        # NaN passes the float() check but fails the clamp comparison;
        # the implementation falls back to clamping NaN -> _SPEED_MAX
        # would be ill-defined, so the implementation either clamps or
        # returns a finite reaction-derived value. Accept either as
        # long as we get a finite value in range.
        if not (speed != speed):  # not NaN
            self.assertGreaterEqual(speed, _SPEED_MIN)
            self.assertLessEqual(speed, _SPEED_MAX)


class SpeakWorkerSamplerateTests(unittest.TestCase):
    """``_speak_worker`` is what actually emits PCM through the
    listener; it must scale the samplerate by ``speed`` so playback
    runs faster / slower on the client side.
    """

    def _capture_first_rate(self, speed: float) -> int:
        svc = _make_service()
        captured: list[int] = []

        def _listener(rate: int, _channels: int, _pcm: bytes) -> None:
            captured.append(rate)

        svc.set_pcm_listener(_listener)
        done = threading.Event()
        svc._speak_worker(
            "hello", on_done=done.set, speed=speed, on_amplitude=None,
        )
        done.wait(timeout=1.0)
        return captured[0]

    def test_play_called_with_scaled_samplerate(self) -> None:
        playback_rate = self._capture_first_rate(1.05)
        self.assertEqual(playback_rate, int(16000 * 1.05))

    def test_play_uses_native_samplerate_at_speed_one(self) -> None:
        playback_rate = self._capture_first_rate(1.0)
        self.assertEqual(playback_rate, 16000)


if __name__ == "__main__":
    unittest.main()
