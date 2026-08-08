"""Regression tests for the post-EOS tail on synthesised speech.

Pocket-TTS keeps decoding after the model signals end-of-sequence: a guess
of 1 frame over four words (3 at or under), plus an unconditional ``+2``.
At the Mimi frame rate of 12.5 Hz that is 240 ms of audio per clip, 400 ms
for short ones, generated with nothing left in the text to say.

That was audible. Measured with the RNG pinned -- so the two takes are
sample-identical right up to the tail and the difference *is* the post-EOS
segment -- it runs at 14-42% of the body's RMS. Per frame, frame 1 is the
genuine phoneme release and frame 2 often matches it in level before frame
3 decays away, which is why the default keeps exactly one frame. The
symptom was a stray syllable at the end of every spoken chunk, which is
also why chasing it in the *text* pipeline found nothing: the text handed
to the synthesiser was always clean.
"""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from app.core.infra.settings import _parse_frames_after_eos
from app.tts.pocket_tts_service import PocketTtsService


def _make_service(frames: object) -> PocketTtsService:
    settings = MagicMock()
    settings.enabled = True
    settings.pocket_tts_frames_after_eos = frames
    with patch("app.tts.pocket_tts_service.TTSModel", None), \
         patch("app.tts.pocket_tts_service.np", np):
        svc = PocketTtsService(settings)
    svc._loaded.set()
    svc._voice_state = {}  # type: ignore[assignment]
    return svc


class ParseTests(unittest.TestCase):
    def test_default_is_one_frame(self) -> None:
        self.assertEqual(_parse_frames_after_eos(1), 1)

    def test_none_hands_control_back_to_the_library(self) -> None:
        self.assertIsNone(_parse_frames_after_eos(None))
        self.assertIsNone(_parse_frames_after_eos("default"))
        self.assertIsNone(_parse_frames_after_eos(""))

    def test_clamped_to_a_meaningful_range(self) -> None:
        # Past 8 frames the tail outlasts the 150 ms of silence appended
        # after it, so the knob has stopped meaning anything.
        self.assertEqual(_parse_frames_after_eos(-3), 0)
        self.assertEqual(_parse_frames_after_eos(99), 8)

    def test_garbage_falls_back_to_the_default(self) -> None:
        self.assertEqual(_parse_frames_after_eos("banana"), 1)


class GenerateTests(unittest.TestCase):
    def test_frames_are_passed_through(self) -> None:
        svc = _make_service(1)
        model = MagicMock()
        svc._generate(model, {}, "hello there friend")
        self.assertEqual(
            model.generate_audio.call_args.kwargs.get("frames_after_eos"), 1
        )

    def test_none_omits_the_argument_entirely(self) -> None:
        # Not "passes None" -- the library treats None as "use my guess",
        # but omitting it keeps us off a kwarg older builds may not have.
        svc = _make_service(None)
        model = MagicMock()
        svc._generate(model, {}, "hello there friend")
        self.assertNotIn("frames_after_eos", model.generate_audio.call_args.kwargs)

    def test_older_library_falls_back_instead_of_losing_the_utterance(self) -> None:
        svc = _make_service(1)
        model = MagicMock()
        sentinel = object()

        def _reject(*args, **kwargs):
            if "frames_after_eos" in kwargs:
                raise TypeError("unexpected keyword argument")
            return sentinel

        model.generate_audio.side_effect = _reject
        self.assertIs(svc._generate(model, {}, "hello there friend"), sentinel)

    def test_unsupported_build_is_only_probed_once(self) -> None:
        svc = _make_service(1)
        model = MagicMock()

        def _reject(*args, **kwargs):
            if "frames_after_eos" in kwargs:
                raise TypeError("unexpected keyword argument")
            return object()

        model.generate_audio.side_effect = _reject
        for _ in range(3):
            svc._generate(model, {}, "hello there friend")
        with_kwarg = [
            c for c in model.generate_audio.call_args_list
            if "frames_after_eos" in c.kwargs
        ]
        self.assertEqual(len(with_kwarg), 1)


if __name__ == "__main__":
    unittest.main()
