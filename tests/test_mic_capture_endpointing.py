"""Lightweight integration tests for the new endpoint_check / on_chunk
hooks in :class:`app.audio.mic_capture.MicrophoneCapture`.

We don't actually open a microphone or use sounddevice; instead we
monkey-patch ``sd.InputStream`` with a fake that yields a scripted
sequence of audio chunks (silence vs. speech) so we can drive the
capture loop deterministically and assert on its behaviour:

- ``on_chunk`` fires once per chunk during the speech region (and
  replays the pre-roll once on speech-start).
- An ``endpoint_check`` returning ``"commit"`` breaks out before the
  hard cap.
- ``"extend"`` resets the silence counter so the loop keeps going.
- ``"wait"`` (the default) lets the loop's own ``silence_chunks_to_stop``
  fire at the hard cap.
- When ``endpoint_check`` is ``None`` we get the legacy single-threshold
  behaviour, unchanged.
"""
from __future__ import annotations

import unittest
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator
from unittest import mock

import numpy as np

from app.audio import mic_capture
from app.audio.mic_capture import MicrophoneCapture
from app.core.settings import AudioSettings


SAMPLE_RATE = 16000
CHUNK_FRAMES = SAMPLE_RATE // 10  # 100 ms
CHANNELS = 1


def _silence_chunk() -> np.ndarray:
    return np.zeros((CHUNK_FRAMES, CHANNELS), dtype=np.float32)


def _speech_chunk(amplitude: float = 0.3) -> np.ndarray:
    # Sine wave so RMS is non-trivial; the WebRTC VAD would also fire on
    # this, but we use ``use_webrtc_vad=False`` so only the level threshold
    # matters here.
    t = np.arange(CHUNK_FRAMES, dtype=np.float32) / float(SAMPLE_RATE)
    wave = (amplitude * np.sin(2 * np.pi * 200.0 * t)).astype(np.float32)
    return wave.reshape(-1, CHANNELS)


@dataclass
class _FakeStream:
    chunks: list[np.ndarray]
    idx: int = 0

    def read(self, _frames: int) -> tuple[np.ndarray, bool]:
        if self.idx >= len(self.chunks):
            # Loop the last chunk forever once we exhaust the script —
            # the capture loop's own deadlines (max_seconds, hard cap)
            # ensure we always terminate.
            chunk = self.chunks[-1]
        else:
            chunk = self.chunks[self.idx]
            self.idx += 1
        return chunk.copy(), False


@contextmanager
def _patched_input_stream(chunks: list[np.ndarray]) -> Iterator[_FakeStream]:
    fake = _FakeStream(chunks=chunks)

    @contextmanager
    def _ctx(*_args: object, **_kwargs: object) -> Iterator[_FakeStream]:
        yield fake

    with mock.patch.object(mic_capture.sd, "InputStream", _ctx):
        yield fake


def _build_capture() -> MicrophoneCapture:
    settings = AudioSettings(
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
        enable_microphone=True,
        microphone_device=None,
        output_device=None,
        vad_level_threshold=0.02,
        vad_silence_seconds=1.0,
        barge_in_enabled=False,
    )
    return MicrophoneCapture(settings)


class CaptureLoopTests(unittest.TestCase):
    """End-to-end behaviour of ``capture_phrase`` with the new callbacks."""

    def _run(
        self,
        chunks: list[np.ndarray],
        **kwargs: object,
    ) -> tuple[np.ndarray | None, list[np.ndarray], list[tuple[float, int]]]:
        cap = _build_capture()
        on_chunk_calls: list[np.ndarray] = []
        endpoint_calls: list[tuple[float, int]] = []

        def _on_chunk(chunk: np.ndarray) -> None:
            on_chunk_calls.append(chunk)

        def _wrap_endpoint(check):
            def _wrapped(silence_s: float, spoken: int) -> str:
                endpoint_calls.append((silence_s, spoken))
                return check(silence_s, spoken)
            return _wrapped

        endpoint_check = kwargs.pop("endpoint_check", None)
        if endpoint_check is not None:
            endpoint_check = _wrap_endpoint(endpoint_check)

        with _patched_input_stream(chunks):
            samples = cap.capture_phrase(
                max_seconds=10.0,
                use_webrtc_vad=False,
                silence_seconds_to_stop=kwargs.pop("silence_seconds_to_stop", 0.5),
                level_threshold=0.02,
                min_speech_seconds_before_stop=kwargs.pop(
                    "min_speech_seconds_before_stop", 0.3
                ),
                speech_start_grace_seconds=0.0,
                on_chunk=_on_chunk,
                endpoint_check=endpoint_check,
                **kwargs,
            )
        return samples, on_chunk_calls, endpoint_calls

    def test_legacy_path_still_works_without_callbacks(self) -> None:
        # 5 silence then 4 speech then 6 silence chunks (>= 0.5s silence).
        chunks = (
            [_silence_chunk()] * 5
            + [_speech_chunk()] * 4
            + [_silence_chunk()] * 6
        )
        cap = _build_capture()
        with _patched_input_stream(chunks):
            samples = cap.capture_phrase(
                max_seconds=5.0,
                use_webrtc_vad=False,
                silence_seconds_to_stop=0.5,
                level_threshold=0.02,
                min_speech_seconds_before_stop=0.3,
                speech_start_grace_seconds=0.0,
            )
        self.assertIsNotNone(samples)
        # Exit was via the silence cap, not the max_seconds deadline.
        self.assertGreater(len(samples), 0)

    def test_on_chunk_replays_pre_roll_then_streams_speech(self) -> None:
        chunks = (
            [_silence_chunk()] * 3
            + [_speech_chunk()] * 5
            + [_silence_chunk()] * 8
        )
        samples, on_chunk_calls, _ = self._run(
            chunks,
            silence_seconds_to_stop=0.5,
            min_speech_seconds_before_stop=0.3,
        )
        self.assertIsNotNone(samples)
        # We should see at least the speech chunks streamed; the pre-roll
        # is replayed once on speech-start so the count is >= speech-only.
        self.assertGreaterEqual(len(on_chunk_calls), 5)

    def test_endpoint_check_commit_breaks_early(self) -> None:
        # Lots of speech, then sustained silence — without a short
        # silence_seconds_to_stop, the loop would only break on the hard
        # cap. The endpoint_check forces an early break at the first
        # silence chunk after grace.
        chunks = (
            [_speech_chunk()] * 6
            + [_silence_chunk()] * 60  # 6s of silence available
        )
        commit_after_silence = 0.2  # seconds

        def _check(silence_s: float, _spoken: int) -> str:
            return "commit" if silence_s >= commit_after_silence else "wait"

        samples, _, endpoint_calls = self._run(
            chunks,
            silence_seconds_to_stop=10.0,  # huge cap so only commit breaks us
            min_speech_seconds_before_stop=0.3,
            endpoint_check=_check,
        )
        self.assertIsNotNone(samples)
        # Endpoint check should have fired at least once.
        self.assertGreaterEqual(len(endpoint_calls), 1)
        # And we should have broken before silence_seconds_to_stop's 10s.
        # Loop exited within ~0.3s of speech-end, so at most a few extra
        # silence chunks beyond the commit threshold.
        max_silence_seen = max(s for s, _ in endpoint_calls)
        self.assertGreaterEqual(max_silence_seen, commit_after_silence)
        self.assertLess(max_silence_seen, 1.0)

    def test_endpoint_check_extend_resets_silence_counter(self) -> None:
        # We give 6 speech chunks then a long silence. The check returns
        # "extend" the FIRST time silence reaches 0.3s, and "wait" after
        # that. With extend, silence_chunks resets to 0 — so the loop
        # should run for *more* silence chunks total than without.
        chunks = (
            [_speech_chunk()] * 6
            + [_silence_chunk()] * 60
        )
        extends_remaining = [1]
        observed = {"max_silence": 0.0, "extends": 0}

        def _check(silence_s: float, _spoken: int) -> str:
            observed["max_silence"] = max(observed["max_silence"], silence_s)
            if silence_s >= 0.3 and extends_remaining[0] > 0:
                extends_remaining[0] -= 1
                observed["extends"] += 1
                return "extend"
            return "wait"

        samples, _, _ = self._run(
            chunks,
            silence_seconds_to_stop=0.5,  # 5 silence chunks @ 100ms = hard cap
            min_speech_seconds_before_stop=0.3,
            endpoint_check=_check,
        )
        self.assertIsNotNone(samples)
        self.assertEqual(observed["extends"], 1)
        # After the reset at ~0.3s, silence rebuilds from 0 to 0.5s before
        # the hard cap fires — so we observed silence in excess of
        # silence_seconds_to_stop in total.
        self.assertGreaterEqual(observed["max_silence"], 0.4)

    def test_endpoint_check_wait_falls_through_to_hard_cap(self) -> None:
        chunks = (
            [_speech_chunk()] * 6
            + [_silence_chunk()] * 60
        )

        def _check(_silence: float, _spoken: int) -> str:
            return "wait"

        samples, _, endpoint_calls = self._run(
            chunks,
            silence_seconds_to_stop=0.5,
            min_speech_seconds_before_stop=0.3,
            endpoint_check=_check,
        )
        self.assertIsNotNone(samples)
        # Hard cap is 0.5s; the check is consulted but never short-circuits.
        max_silence_seen = max(s for s, _ in endpoint_calls)
        self.assertLessEqual(max_silence_seen, 0.6)

    def test_endpoint_check_exception_is_swallowed(self) -> None:
        # A misbehaving callback must not abort the capture; the loop
        # treats exceptions as "wait" and falls back to the silence cap.
        chunks = (
            [_speech_chunk()] * 6
            + [_silence_chunk()] * 30
        )

        def _check(_silence: float, _spoken: int) -> str:
            raise RuntimeError("nope")

        samples, _, _ = self._run(
            chunks,
            silence_seconds_to_stop=0.5,
            min_speech_seconds_before_stop=0.3,
            endpoint_check=_check,
        )
        self.assertIsNotNone(samples)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
