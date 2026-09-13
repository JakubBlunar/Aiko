"""Live mode Pass 8: H6 backchannel audio + H7 duplex plumbing."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.audio.client_mic_source import ClientMicSource
from app.core.infra.settings import AudioSettings
from app.core.session.live_session import LiveSession
from app.core.session.voice_mixin import VoiceMixin
from tests.test_client_mic_source import _sine_pcm
from tests.test_live_mode_pass1 import LiveModeMixinHost


class BackchannelPlayTests(unittest.TestCase):
    def test_plays_mm_via_earcon_player_not_tts_queue(self) -> None:
        host = SimpleNamespace(
            _earcons=MagicMock(),
            _tts=MagicMock(),
        )
        host.backchannel_audio_enabled = lambda: True
        host.is_tts_playing = lambda: False
        host.mic_last_level = lambda: 0.0
        VoiceMixin._maybe_play_backchannel_audio(host, "agreement")
        host._earcons.play.assert_called_once_with("mm")
        host._tts.enqueue_earcon.assert_not_called()

    def test_skips_when_disabled(self) -> None:
        host = SimpleNamespace(_earcons=MagicMock())
        host.backchannel_audio_enabled = lambda: False
        host.is_tts_playing = lambda: False
        host.mic_last_level = lambda: 0.0
        VoiceMixin._maybe_play_backchannel_audio(host, "agreement")
        host._earcons.play.assert_not_called()


class PlaybackDrainTests(unittest.TestCase):
    def test_notify_sets_event_and_impulse(self) -> None:
        host = LiveModeMixinHost()
        host._settings.agent.live_impulse_bus_enabled = True
        host.mark_playback_pending()
        self.assertFalse(host.is_playback_drained())
        host.notify_playback_drained()
        self.assertTrue(host.is_playback_drained())
        kinds = [item["kind"] for item in host._live_impulse_bus.snapshot()["tail"]]
        self.assertIn("aiko.playback_drained", kinds)

    def test_wait_returns_when_client_acks(self) -> None:
        session = MagicMock()
        session.is_tts_playing.return_value = True
        session.is_playback_drained.side_effect = [False, False, True]
        live = LiveSession(session, on_event=lambda *_a, **_k: None)
        with patch("app.core.session.live_session.time.sleep"):
            live._wait_for_tts_drain()
        session.mark_playback_pending.assert_called_once()
        self.assertEqual(session.is_playback_drained.call_count, 3)

    def test_wait_skips_when_tts_never_started(self) -> None:
        session = MagicMock()
        session.is_tts_playing.return_value = False
        session.is_playback_drained.return_value = True
        live = LiveSession(session, on_event=lambda *_a, **_k: None)
        with patch("app.core.session.live_session.time.sleep") as slept:
            live._wait_for_tts_drain()
        slept.assert_not_called()
        session.mark_playback_pending.assert_not_called()


class OverlapWatchTests(unittest.TestCase):
    def test_sustained_energy_aborts_turn(self) -> None:
        session = MagicMock()
        session.barge_in_enabled.return_value = True
        session.mic_last_level.return_value = 0.2
        session.vad_level_threshold = 0.02
        session._settings.endpointing.barge_in_min_speech_seconds = 0.1
        live = LiveSession(session, on_event=lambda *_a, **_k: None)
        live._tick_overlap_watch()
        session.stop_tts.assert_not_called()
        live._tick_overlap_watch()
        session.stop_tts.assert_called_once()
        session.request_turn_stop.assert_called_once()
        self.assertTrue(live._is_turn_aborted())


class IdleRingTests(unittest.TestCase):
    def test_idle_queue_caps_at_ring(self) -> None:
        source = ClientMicSource(
            AudioSettings(
                sample_rate=16000,
                channels=1,
                enable_microphone=True,
                vad_level_threshold=0.02,
                vad_silence_seconds=1.0,
                barge_in_enabled=True,
            ),
        )
        source.feed_start(16000, 1, 0)
        source.feed_pcm(16000, 1, _sine_pcm(16000, 3.0))
        self.assertLessEqual(source._chunk_queue.qsize(), 15)
        self.assertGreater(source.last_level, 0.0)


class CaptureAvailableTests(unittest.TestCase):
    def test_true_only_while_voice_session_active(self) -> None:
        host = LiveModeMixinHost()
        self.assertFalse(host.live_capture_available())
        host._live_voice_session_active = True
        self.assertTrue(host.live_capture_available())


class BargeInPersistTests(unittest.TestCase):
    def test_set_barge_in_enabled_persists(self) -> None:
        host = SimpleNamespace(
            _settings=SimpleNamespace(
                audio=SimpleNamespace(barge_in_enabled=True),
            ),
        )
        with patch(
            "app.core.session.voice_mixin.persist_user_overrides",
        ) as persist:
            VoiceMixin.set_barge_in_enabled(host, False)
        self.assertFalse(host._settings.audio.barge_in_enabled)
        persist.assert_called_once_with({"audio": {"barge_in_enabled": False}})


if __name__ == "__main__":
    unittest.main()
