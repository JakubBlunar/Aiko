"""Binary WebSocket frame protocol for the client-owned audio path.

The browser owns mic capture and TTS playback now. Audio flows
between client and server as length-prefixed binary WebSocket
frames; each frame is a single byte of ``type`` followed by an
opaque payload whose shape depends on the type. JSON keeps doing
chat / state / metadata.

Frame catalog (single source of truth — keep
``web/src/audio/protocol.ts`` in lock-step):

  Client → Server
    0x01  mic_pcm     PCM16 LE chunk at the rate from ``mic_start``.
    0x02  mic_start   ``<uint32 sample_rate><uint8 channels><uint8 dsp_flags>``
                     Resets the source buffers and announces format.

  Server → Client
    0x10  tts_pcm     PCM16 LE TTS chunk; rate = leading ``audio_start``.
    0x11  earcon_pcm  Same shape as 0x10 but for notification sounds.
    0x12  audio_start ``<uint8 stream><uint32 sample_rate><uint8 channels>``
                     where ``stream`` is 0x10 (tts) or 0x11 (earcon);
                     tells the client to spin up a playback queue.
    0x13  audio_end   ``<uint8 stream>`` — flush the matching queue.

All integers are big-endian unsigned (network byte order). The
helpers below are intentionally tiny so both sides can match them
trivially.
"""
from __future__ import annotations

import struct

# Frame type bytes. ``MIC_*`` for inbound, ``TTS_*`` / ``EARCON_*`` /
# ``AUDIO_*`` for outbound.
FRAME_MIC_PCM: int = 0x01
FRAME_MIC_START: int = 0x02
FRAME_TTS_PCM: int = 0x10
FRAME_EARCON_PCM: int = 0x11
FRAME_AUDIO_START: int = 0x12
FRAME_AUDIO_END: int = 0x13


# DSP flag bits used by ``mic_start``. The browser sets these to
# advertise which built-in DSP nodes are active for the current
# capture — the server logs them for QoS metrics but doesn't
# otherwise react.
DSP_ECHO_CANCELLATION: int = 0b0000_0001
DSP_NOISE_SUPPRESSION: int = 0b0000_0010
DSP_AUTO_GAIN_CONTROL: int = 0b0000_0100


_MIC_START_FORMAT = ">IBB"  # u32 sample_rate, u8 channels, u8 dsp_flags
_AUDIO_START_FORMAT = ">BIB"  # u8 stream, u32 sample_rate, u8 channels


def build_audio_start(stream: int, sample_rate: int, channels: int) -> bytes:
    """Build a ``0x12 audio_start`` frame.

    ``stream`` should be ``FRAME_TTS_PCM`` or ``FRAME_EARCON_PCM`` so
    the client knows which playback queue to spin up. Channels is
    1-byte so we clamp the inputs; today everything is mono.
    """
    return bytes([FRAME_AUDIO_START]) + struct.pack(
        _AUDIO_START_FORMAT,
        int(stream) & 0xFF,
        max(0, int(sample_rate)) & 0xFFFFFFFF,
        max(1, int(channels)) & 0xFF,
    )


def build_audio_end(stream: int) -> bytes:
    """Build a ``0x13 audio_end`` frame."""
    return bytes([FRAME_AUDIO_END, int(stream) & 0xFF])


def build_tts_pcm(pcm: bytes) -> bytes:
    """Wrap ``pcm`` (Int16 LE) in a ``0x10 tts_pcm`` frame."""
    return bytes([FRAME_TTS_PCM]) + pcm


def build_earcon_pcm(pcm: bytes) -> bytes:
    """Wrap ``pcm`` (Int16 LE) in a ``0x11 earcon_pcm`` frame."""
    return bytes([FRAME_EARCON_PCM]) + pcm


def parse_mic_start(payload: bytes) -> tuple[int, int, int] | None:
    """Parse a ``0x02 mic_start`` payload (without the leading type byte).

    Returns ``(sample_rate, channels, dsp_flags)`` or ``None`` on a
    malformed frame.
    """
    if len(payload) < struct.calcsize(_MIC_START_FORMAT):
        return None
    try:
        sample_rate, channels, dsp_flags = struct.unpack_from(
            _MIC_START_FORMAT, payload, 0,
        )
    except struct.error:
        return None
    return int(sample_rate), int(channels), int(dsp_flags)


def parse_mic_pcm(payload: bytes) -> bytes:
    """Identity helper for symmetry with the start parser."""
    return bytes(payload)


def stream_name(stream: int) -> str:
    """Map a stream type byte to its short tag (``"tts"`` / ``"earcon"``)."""
    if stream == FRAME_TTS_PCM:
        return "tts"
    if stream == FRAME_EARCON_PCM:
        return "earcon"
    return "unknown"


def stream_byte(name: str) -> int:
    """Inverse of :func:`stream_name`."""
    lowered = (name or "").strip().lower()
    if lowered == "tts":
        return FRAME_TTS_PCM
    if lowered == "earcon":
        return FRAME_EARCON_PCM
    return 0
