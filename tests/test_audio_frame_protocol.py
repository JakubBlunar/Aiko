"""Round-trip tests for the binary WS frame protocol.

These tests exercise the parse / build helpers in
:mod:`app.web.audio_frames`. They're deliberately tiny — the protocol
is meant to stay decode-trivial on both sides — but they catch any
accidental drift between the Python builders and the TypeScript
counterparts in ``web/src/audio/protocol.ts``.
"""
from __future__ import annotations

import struct
import unittest

from app.web import audio_frames as frames


class FrameTypeBytesTests(unittest.TestCase):
    """Pin the integer values so a future renumbering is loud."""

    def test_frame_type_bytes_are_stable(self) -> None:
        self.assertEqual(frames.FRAME_MIC_PCM, 0x01)
        self.assertEqual(frames.FRAME_MIC_START, 0x02)
        self.assertEqual(frames.FRAME_TTS_PCM, 0x10)
        self.assertEqual(frames.FRAME_EARCON_PCM, 0x11)
        self.assertEqual(frames.FRAME_AUDIO_START, 0x12)
        self.assertEqual(frames.FRAME_AUDIO_END, 0x13)


class AudioStartTests(unittest.TestCase):
    def test_build_audio_start_has_stream_rate_channels(self) -> None:
        frame = frames.build_audio_start(frames.FRAME_TTS_PCM, 22050, 1)
        # 1 byte type + 1 byte stream + 4 bytes sample_rate + 1 byte channels
        self.assertEqual(len(frame), 7)
        self.assertEqual(frame[0], frames.FRAME_AUDIO_START)
        stream, sample_rate, channels = struct.unpack(">BIB", frame[1:])
        self.assertEqual(stream, frames.FRAME_TTS_PCM)
        self.assertEqual(sample_rate, 22050)
        self.assertEqual(channels, 1)

    def test_build_audio_start_clamps_channels_to_at_least_one(self) -> None:
        frame = frames.build_audio_start(frames.FRAME_EARCON_PCM, 22050, 0)
        _stream, _rate, channels = struct.unpack(">BIB", frame[1:])
        self.assertEqual(channels, 1)


class AudioEndTests(unittest.TestCase):
    def test_audio_end_is_two_bytes(self) -> None:
        frame = frames.build_audio_end(frames.FRAME_TTS_PCM)
        self.assertEqual(frame, bytes([frames.FRAME_AUDIO_END, frames.FRAME_TTS_PCM]))


class MicStartTests(unittest.TestCase):
    def test_round_trip_mic_start_payload(self) -> None:
        # Browser would send this as the leading frame of a capture.
        payload = struct.pack(">IBB", 48000, 1, 0b111)
        parsed = frames.parse_mic_start(payload)
        self.assertIsNotNone(parsed)
        sample_rate, channels, dsp_flags = parsed  # type: ignore[misc]
        self.assertEqual(sample_rate, 48000)
        self.assertEqual(channels, 1)
        self.assertEqual(dsp_flags, frames.DSP_ECHO_CANCELLATION
                         | frames.DSP_NOISE_SUPPRESSION
                         | frames.DSP_AUTO_GAIN_CONTROL)

    def test_truncated_payload_returns_none(self) -> None:
        self.assertIsNone(frames.parse_mic_start(b"\x00\x00\x00"))


class PcmWrappersTests(unittest.TestCase):
    def test_tts_pcm_prefix(self) -> None:
        body = b"\x00\x01\x02\x03"
        frame = frames.build_tts_pcm(body)
        self.assertEqual(frame[0], frames.FRAME_TTS_PCM)
        self.assertEqual(frame[1:], body)

    def test_earcon_pcm_prefix(self) -> None:
        body = b"\x10\x20"
        frame = frames.build_earcon_pcm(body)
        self.assertEqual(frame[0], frames.FRAME_EARCON_PCM)
        self.assertEqual(frame[1:], body)


class StreamNameTests(unittest.TestCase):
    def test_round_trip_known_streams(self) -> None:
        self.assertEqual(frames.stream_name(frames.FRAME_TTS_PCM), "tts")
        self.assertEqual(frames.stream_name(frames.FRAME_EARCON_PCM), "earcon")
        self.assertEqual(frames.stream_byte("tts"), frames.FRAME_TTS_PCM)
        self.assertEqual(frames.stream_byte("earcon"), frames.FRAME_EARCON_PCM)

    def test_unknown_stream_maps_to_zero_byte(self) -> None:
        self.assertEqual(frames.stream_byte("video"), 0)
        self.assertEqual(frames.stream_name(0x42), "unknown")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
