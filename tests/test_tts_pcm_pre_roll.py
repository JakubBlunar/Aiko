"""``tts.pcm_pre_roll_ms`` clamps and reaches the emit loop."""
from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from app.core.infra.settings import _parse_pcm_pre_roll_ms
from app.tts.pcm_playback import PcmPlaybackMixin
from app.tts.pocket_tts_service import PocketTtsService


class ParseTests(unittest.TestCase):
    def test_default_is_six_hundred(self) -> None:
        self.assertEqual(_parse_pcm_pre_roll_ms(600), 600)

    def test_floor_is_the_old_localhost_cushion(self) -> None:
        self.assertEqual(_parse_pcm_pre_roll_ms(0), 250)
        self.assertEqual(_parse_pcm_pre_roll_ms(100), 250)

    def test_ceiling_stops_a_runaway_barge_in_queue(self) -> None:
        self.assertEqual(_parse_pcm_pre_roll_ms(5000), 1500)

    def test_garbage_falls_back(self) -> None:
        self.assertEqual(_parse_pcm_pre_roll_ms("nope"), 600)


class ConfigureTests(unittest.TestCase):
    def test_service_adopts_settings(self) -> None:
        settings = MagicMock()
        settings.enabled = True
        settings.pcm_pre_roll_ms = 400
        with patch("app.tts.pocket_tts_service.TTSModel", None), \
             patch("app.tts.pocket_tts_service.np", np):
            svc = PocketTtsService(settings)
        # 400 ms / 50 ms slices = 8 chunks.
        self.assertEqual(svc._PRE_ROLL_CHUNKS, 8)

    def test_mock_settings_do_not_crash_the_default(self) -> None:
        settings = MagicMock()
        settings.enabled = True
        # MagicMock.pcm_pre_roll_ms is another Mock; float() fails.
        with patch("app.tts.pocket_tts_service.TTSModel", None), \
             patch("app.tts.pocket_tts_service.np", np):
            svc = PocketTtsService(settings)
        self.assertEqual(svc._PRE_ROLL_CHUNKS, 12)

    def test_mixin_helper_clamps_like_the_parser(self) -> None:
        host = PcmPlaybackMixin()
        host._configure_pre_roll(MagicMock(pcm_pre_roll_ms=50))
        self.assertEqual(host._PRE_ROLL_CHUNKS, 5)
        host._configure_pre_roll(MagicMock(pcm_pre_roll_ms=2000))
        self.assertEqual(host._PRE_ROLL_CHUNKS, 30)
