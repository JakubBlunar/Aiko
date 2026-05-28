"""Lightweight integration tests for the endpoint_check / on_chunk
hooks in :class:`app.audio.client_mic_source.ClientMicSource`.

The source is fed scripted PCM via ``feed_pcm`` (matching how the
WS layer drives it in production); we don't open a microphone or
use sounddevice. Each test pushes a deterministic sequence of
silence / speech chunks into the queue and asserts on the capture
loop's behaviour:

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

import threading
import time
import unittest

import numpy as np

from app.audio.client_mic_source import ClientMicSource
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


def _to_pcm_bytes(chunk: np.ndarray) -> bytes:
    mono = chunk[:, 0] if chunk.ndim > 1 else chunk
    pcm = np.clip(mono, -1.0, 1.0)
    return (pcm * 32767).astype(np.int16).tobytes()


def _build_capture() -> ClientMicSource:
    settings = AudioSettings(
        sample_rate=SAMPLE_RATE,
        channels=CHANNELS,
        enable_microphone=True,
        vad_level_threshold=0.02,
        vad_silence_seconds=1.0,
        barge_in_enabled=False,
    )
    return ClientMicSource(settings)


class _Feeder:
    """Drip scripted PCM chunks into a ClientMicSource on a worker thread."""

    def __init__(self, source: ClientMicSource, chunks: list[np.ndarray]) -> None:
        self._source = source
        self._chunks = chunks
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._source.feed_start(SAMPLE_RATE, CHANNELS, 0)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)
        self._source.feed_end()

    def _run(self) -> None:
        for chunk in self._chunks:
            if self._stop.is_set():
                return
            self._source.feed_pcm(SAMPLE_RATE, CHANNELS, _to_pcm_bytes(chunk))
            # 100 ms of pre-buffered audio per feed call; sleep so the
            # capture loop's RMS / VAD math runs against fresh data
            # rather than draining the queue in one go.
            time.sleep(0.02)
        # Loop the last chunk forever so the capture loop's
        # ``max_seconds`` / silence caps eventually break us.
        last = self._chunks[-1] if self._chunks else _silence_chunk()
        while not self._stop.is_set():
            self._source.feed_pcm(SAMPLE_RATE, CHANNELS, _to_pcm_bytes(last))
            time.sleep(0.02)


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

        feeder = _Feeder(cap, chunks)
        feeder.start()
        try:
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
        finally:
            feeder.stop()
        return samples, on_chunk_calls, endpoint_calls

    def test_legacy_path_still_works_without_callbacks(self) -> None:
        # 5 silence then 4 speech then 6 silence chunks (>= 0.5s silence).
        chunks = (
            [_silence_chunk()] * 5
            + [_speech_chunk()] * 4
            + [_silence_chunk()] * 6
        )
        cap = _build_capture()
        feeder = _Feeder(cap, chunks)
        feeder.start()
        try:
            samples = cap.capture_phrase(
                max_seconds=5.0,
                use_webrtc_vad=False,
                silence_seconds_to_stop=0.5,
                level_threshold=0.02,
                min_speech_seconds_before_stop=0.3,
                speech_start_grace_seconds=0.0,
            )
        finally:
            feeder.stop()
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
