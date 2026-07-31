"""P28 -- TTS must not load when ``tts.enabled`` is false.

The bug this covers is easy to mistake for fixed: playback *was* gated
(``TtsQueue(enabled=...)``, ``get_status``, ``warmup_sync`` all check the
flag), so from the outside a TTS-off install looked correct — while the
constructor had already started a load thread for the voice model and the
import had already pulled in the PyTorch runtime.

The load itself can't be asserted on directly without the optional
``pocket_tts`` dependency, so these tests assert the thing that *causes*
it: which engine class gets built, and that the heavy module is never
imported on the disabled path.
"""
from __future__ import annotations

import sys
import unittest
from types import SimpleNamespace
from unittest import mock

from app.core.session.lifecycle_mixin import LifecycleMixin
from app.tts.null_tts_service import NullTtsService


def _settings(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        tts=SimpleNamespace(
            enabled=enabled,
            provider="pocket-tts",
            voice="alba",
            pocket_tts_voice="alba",
            pocket_tts_temp=0.7,
        ),
    )


class BuildGateTests(unittest.TestCase):
    def test_disabled_builds_the_null_engine(self) -> None:
        engine = LifecycleMixin._build_tts_service(_settings(False))
        self.assertIsInstance(engine, NullTtsService)
        self.assertTrue(engine.is_null_engine)

    def test_disabled_never_imports_the_heavy_module(self) -> None:
        # The import is the expensive half (PyTorch), so it has to be
        # inside the enabled branch, not at module scope.
        with mock.patch.dict(sys.modules):
            sys.modules.pop("app.tts.pocket_tts_service", None)
            LifecycleMixin._build_tts_service(_settings(False))
            self.assertNotIn("app.tts.pocket_tts_service", sys.modules)

    def test_enabled_builds_the_real_engine(self) -> None:
        built: list[object] = []

        class _Fake:
            def __init__(self, settings):
                built.append(settings)

        with mock.patch(
            "app.tts.pocket_tts_service.PocketTtsService", _Fake, create=True,
        ):
            engine = LifecycleMixin._build_tts_service(_settings(True))
        self.assertIsInstance(engine, _Fake)
        self.assertEqual(len(built), 1)

    def test_missing_enabled_attribute_defaults_to_on(self) -> None:
        # Don't silently disable TTS for a config shape we didn't expect.
        settings = SimpleNamespace(tts=SimpleNamespace())
        with mock.patch(
            "app.tts.pocket_tts_service.PocketTtsService",
            lambda _s: "real", create=True,
        ):
            self.assertEqual(
                LifecycleMixin._build_tts_service(settings), "real",
            )


class NullEngineContractTests(unittest.TestCase):
    """Every call site must survive the null engine untouched."""

    def setUp(self) -> None:
        self.engine = NullTtsService(_settings(False).tts)

    def test_status_says_disabled_not_error(self) -> None:
        state, _ = self.engine.get_status()
        self.assertEqual(state, "disabled")

    def test_warmup_reports_success(self) -> None:
        # ``prewarm_runtime`` treats False as a boot problem worth logging.
        self.assertTrue(self.engine.warmup_sync())

    def test_speak_is_a_silent_no_op(self) -> None:
        self.assertFalse(self.engine.speak_async("hello", reaction="warm"))

    def test_stop_fires_the_end_listener(self) -> None:
        fired: list[bool] = []
        self.engine.set_pcm_listener(None, end_listener=lambda: fired.append(True))
        self.engine.stop()
        self.assertEqual(fired, [True])

    def test_a_raising_end_listener_does_not_escape(self) -> None:
        def _boom():
            raise RuntimeError("client gone")

        self.engine.set_pcm_listener(None, end_listener=_boom)
        self.engine.stop()  # must not raise

    def test_covers_the_engine_surface_the_session_reaches_for(self) -> None:
        for name in (
            "set_pcm_listener", "get_status", "model_status", "warmup_sync",
            "warmup_async", "speak_async", "stop", "list_voices", "set_voice",
            "get_model", "reaction_to_speed", "set_length_scale",
            "set_runtime_temp_enabled", "set_runtime_speed_enabled",
            "release_model", "export_voice",
        ):
            self.assertTrue(callable(getattr(self.engine, name, None)), name)


class _Host:
    """Minimal stand-in exercising the real mixin methods."""

    def __init__(self, engine, *, enabled: bool) -> None:
        from app.core.session.voice_mixin import VoiceMixin

        self.__class__ = type("_HostWithMixin", (_Host, VoiceMixin), {})
        self._settings = _settings(enabled)
        self._tts_engine = engine
        self._tts = SimpleNamespace(
            set_enabled=lambda v: self.calls.append(("queue_enabled", v)),
            stop=lambda: None,
        )
        self.calls: list[tuple] = []
        self.rebuilt = 0

    def _rebuild_tts_engine(self) -> None:
        self.rebuilt += 1


class RuntimeToggleTests(unittest.TestCase):
    def test_disabling_releases_the_weights(self) -> None:
        released: list[bool] = []
        engine = SimpleNamespace(release_model=lambda: released.append(True))
        host = _Host(engine, enabled=True)
        self.assertFalse(host.set_tts_enabled(False))
        self.assertEqual(released, [True])
        self.assertFalse(host._settings.tts.enabled)
        self.assertIn(("queue_enabled", False), host.calls)

    def test_enabling_from_a_null_engine_rebuilds(self) -> None:
        host = _Host(NullTtsService(_settings(False).tts), enabled=False)
        self.assertTrue(host.set_tts_enabled(True))
        self.assertEqual(host.rebuilt, 1)

    def test_enabling_a_released_real_engine_reloads_in_place(self) -> None:
        loaded: list[bool] = []
        engine = SimpleNamespace(load_model_now=lambda: loaded.append(True))
        host = _Host(engine, enabled=False)
        self.assertTrue(host.set_tts_enabled(True))
        self.assertEqual(loaded, [True])
        self.assertEqual(host.rebuilt, 0, "no need to rebuild a real engine")

    def test_an_engine_without_release_support_is_tolerated(self) -> None:
        host = _Host(SimpleNamespace(), enabled=True)
        self.assertFalse(host.set_tts_enabled(False))

    def test_a_raising_release_does_not_break_the_toggle(self) -> None:
        def _boom():
            raise RuntimeError("release failed")

        host = _Host(SimpleNamespace(release_model=_boom), enabled=True)
        self.assertFalse(host.set_tts_enabled(False))
        self.assertFalse(host._settings.tts.enabled)


class PrewarmGateTests(unittest.TestCase):
    def test_prewarm_skips_the_engine_when_disabled(self) -> None:
        # ``warmup_sync`` on the real engine blocks up to 60s waiting for
        # a load that never started, so this is a boot-hang guard too.
        touched: list[str] = []
        engine = SimpleNamespace(
            warmup_sync=lambda: touched.append("sync"),
            warmup_async=lambda: touched.append("async"),
        )
        host = _Host(engine, enabled=False)
        host.prewarm_tts()
        self.assertEqual(touched, [])

    def test_prewarm_runs_when_enabled(self) -> None:
        touched: list[str] = []
        engine = SimpleNamespace(warmup_sync=lambda: touched.append("sync"))
        host = _Host(engine, enabled=True)
        host.prewarm_tts()
        self.assertEqual(touched, ["sync"])


if __name__ == "__main__":
    unittest.main()
