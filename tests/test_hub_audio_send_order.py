"""Outbound TTS frames must reach the socket in submit order.

Each ``send_audio_bytes`` used to ``create_task`` a concurrent
``ws.send_bytes``. Starlette does not serialise those, so on a slow hop
(phone over Tailscale) the last PCM of clip N could land after clip N+1's
``audio_start`` -- leftover syllable at the start of the next sentence.
The hub now drains one deque with one pump; this test gives ``send_bytes``
an artificial delay so overlap would reorder without that pump.
"""
from __future__ import annotations

import asyncio
import unittest

from app.web.server import _Hub


class _SlowWs:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    async def send_bytes(self, frame: bytes) -> None:
        await asyncio.sleep(0.01)
        self.sent.append(bytes(frame))


class AudioSendOrderTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_sends_stay_in_submit_order(self) -> None:
        hub = _Hub()
        hub.attach_loop(asyncio.get_running_loop())
        ws = _SlowWs()
        hub.add(ws, "phone")  # type: ignore[arg-type]
        hub.set_client_presence("phone", True)
        hub.recompute_audio_owner()
        self.assertEqual(hub.audio_owner_id, "phone")

        frames = [bytes([i]) for i in range(1, 9)]
        for frame in frames:
            hub.send_audio_bytes(frame)
        await asyncio.sleep(0.25)
        self.assertEqual(ws.sent, frames)
