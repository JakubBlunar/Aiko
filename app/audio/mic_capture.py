"""Backwards-compat shim for the pre-refactor microphone module.

The real implementation now lives in :mod:`app.audio.client_mic_source`
— audio comes from a connected WebSocket client rather than the host
``sounddevice`` API. Keep the old names exported so external imports
(stale tests, downstream tooling) keep working without churn.
"""
from __future__ import annotations

from app.audio.client_mic_source import ClientMicSource


# Old import path: ``from app.audio.mic_capture import MicrophoneCapture``.
MicrophoneCapture = ClientMicSource


def list_output_devices() -> list[tuple[int, str]]:
    """No-op kept for callers that still import the helper.

    Output devices are enumerated client-side now — the browser owns
    the audio stack, so there's nothing meaningful to report from the
    server. Returns an empty list.
    """
    return []


__all__ = ["MicrophoneCapture", "ClientMicSource", "list_output_devices"]
