"""Tests for :class:`app.audio.client_mic_source.ClientMicSource`.

We focus on the new wiring — feeding PCM via ``feed_pcm`` and pulling
it back through the legacy capture surface (``read_chunk``,
``capture_seconds``) — plus the migration-critical bits like the
48k → 16k resample path and the ``mic_start`` reset.
"""
from __future__ import annotations

import threading
import time
import unittest

import numpy as np

from app.audio.client_mic_source import ClientMicSource, _QueuedInputStream
from app.core.infra.settings import AudioSettings


def _settings(sample_rate: int = 16000, channels: int = 1) -> AudioSettings:
    return AudioSettings(
        sample_rate=sample_rate,
        channels=channels,
        enable_microphone=True,
        vad_level_threshold=0.02,
        vad_silence_seconds=1.0,
        barge_in_enabled=False,
    )


def _sine_pcm(sample_rate: int, seconds: float, freq: float = 440.0) -> bytes:
    n = int(sample_rate * seconds)
    t = np.arange(n, dtype=np.float32) / float(sample_rate)
    wave = (0.3 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)
    return (np.clip(wave, -1.0, 1.0) * 32767).astype(np.int16).tobytes()


class FeedAndReadTests(unittest.TestCase):
    def test_feeds_round_trip_through_queued_stream(self) -> None:
        source = ClientMicSource(_settings(sample_rate=16000))
        source.feed_start(16000, 1, 0)
        # Push 300 ms of speech at the native rate so the resampler is
        # a no-op and we can assert directly on RMS.
        source.feed_pcm(16000, 1, _sine_pcm(16000, 0.3))
        with _QueuedInputStream(source, channels=1) as stream:
            chunk, overflow = stream.read(16000 // 10)  # 100 ms
        self.assertFalse(overflow)
        self.assertEqual(chunk.shape, (1600, 1))
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        self.assertGreater(rms, 0.05)

    def test_resamples_48k_input_to_target_rate(self) -> None:
        source = ClientMicSource(_settings(sample_rate=16000))
        source.feed_start(48000, 1, 0)
        # 200 ms at 48 kHz should produce ~3200 samples at 16 kHz.
        source.feed_pcm(48000, 1, _sine_pcm(48000, 0.2))
        # Pull from the queue — internal blocks are 100 ms, so two reads
        # at 100 ms each cover the resampled span.
        collected: list[np.ndarray] = []
        with _QueuedInputStream(source, channels=1) as stream:
            for _ in range(2):
                chunk, _ov = stream.read(1600)
                collected.append(chunk[:, 0])
        joined = np.concatenate(collected, axis=0)
        # We resampled 9600 source samples → ~3200 target samples;
        # require at least 80% of that ended up in the queue.
        nonzero = int(np.count_nonzero(np.abs(joined) > 1e-4))
        self.assertGreater(nonzero, 2500)

    def test_feed_start_drops_pending_audio(self) -> None:
        source = ClientMicSource(_settings(sample_rate=16000))
        source.feed_start(16000, 1, 0)
        # Push a lot of audio, then call feed_start again — the queue
        # should be drained so the next read sees only the new stream.
        source.feed_pcm(16000, 1, _sine_pcm(16000, 0.5))
        source.feed_start(16000, 1, 0)
        # Push a deterministic silence chunk.
        source.feed_pcm(16000, 1, np.zeros(1600, dtype=np.int16).tobytes())
        with _QueuedInputStream(source, channels=1) as stream:
            chunk, _ = stream.read(1600)
        rms = float(np.sqrt(np.mean(chunk * chunk)))
        self.assertLess(rms, 1e-3)

    def test_feed_end_makes_read_return_padding(self) -> None:
        source = ClientMicSource(_settings(sample_rate=16000))
        source.feed_start(16000, 1, 0)
        # No PCM at all, then end — the read should return silence,
        # not block forever.
        source.feed_end()
        with _QueuedInputStream(source, channels=1) as stream:
            chunk, _ = stream.read(1600)
        # Padding is zeros; signal flat.
        self.assertEqual(chunk.shape, (1600, 1))
        self.assertTrue(np.all(chunk == 0.0))

    def test_browser_dsp_flags_round_trip(self) -> None:
        source = ClientMicSource(_settings())
        source.feed_start(48000, 1, 0b101)
        self.assertEqual(source.browser_dsp_flags, 0b101)


class LegacySurfaceTests(unittest.TestCase):
    """The public methods :class:`SessionController` relies on must
    still work — only the audio source changed, not the contract."""

    def test_list_input_devices_returns_empty(self) -> None:
        source = ClientMicSource(_settings())
        self.assertEqual(source.list_input_devices(), [])

    def test_set_device_is_a_noop(self) -> None:
        source = ClientMicSource(_settings())
        # No exception, no side effects.
        source.set_device(7)

    def test_read_chunk_returns_pcm16_bytes_at_settings_rate(self) -> None:
        source = ClientMicSource(_settings(sample_rate=16000))
        source.feed_start(16000, 1, 0)

        def _feed():
            time.sleep(0.05)
            source.feed_pcm(16000, 1, _sine_pcm(16000, 0.2))

        threading.Thread(target=_feed, daemon=True).start()
        chunk = source.read_chunk(chunk_ms=100)
        self.assertIsNotNone(chunk)
        # 100 ms at 16 kHz Int16 mono = 3200 bytes.
        self.assertEqual(len(chunk), 3200)

    def test_capture_seconds_returns_two_d_array(self) -> None:
        source = ClientMicSource(_settings(sample_rate=16000))
        source.feed_start(16000, 1, 0)

        def _feed():
            for _ in range(3):
                source.feed_pcm(16000, 1, _sine_pcm(16000, 0.1))
                time.sleep(0.01)

        threading.Thread(target=_feed, daemon=True).start()
        samples = source.capture_seconds(seconds=0.2)
        # Shape: (frames, channels) — mono so channels=1.
        self.assertEqual(samples.ndim, 2)
        self.assertEqual(samples.shape[1], 1)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
