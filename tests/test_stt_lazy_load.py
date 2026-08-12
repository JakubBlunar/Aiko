"""P27 -- the STT model must load on use, not at construction.

Whisper large-v1 plus RealtimeSTT's transcription child process is the
largest resident cost in the app, and a text-only session used to pay all
of it at boot. The subtle part isn't the deferral itself, it's
``is_available``: five gate sites in ``voice_capture_mixin`` consult it
*before* any audio exists, so keeping its old "a recorder object exists"
meaning would have refused every voice turn forever. These tests pin the
new meaning ("could load") alongside the deferral.

``AudioToTextRecorder`` is patched throughout -- the real dependency is a
``[voice]`` extra, and the point is to count constructions, not to run
Whisper.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest import mock

from app.core.infra.settings import SttSettings
import app.stt.realtime_stt_service as stt_mod
from app.stt.realtime_stt_service import RealtimeSttService


def _audio() -> SimpleNamespace:
    return SimpleNamespace(sample_rate=16000, channels=1)


def _stt_settings(**over) -> SttSettings:
    base = {"model": "base", "language": "en", "device": "cpu"}
    base.update(over)
    return SttSettings(**base)


class _FakeRecorder:
    """Counts constructions and records what was fed."""

    instances: list["_FakeRecorder"] = []

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.fed: list[bytes] = []
        self.entered = 0
        self.exited = 0
        self.shut_down = 0
        _FakeRecorder.instances.append(self)

    def feed_audio(self, data) -> None:
        self.fed.append(data)

    def text(self) -> str:
        return "hello there"

    def __enter__(self):
        self.entered += 1
        return self

    def __exit__(self, *_exc) -> None:
        self.exited += 1

    def shutdown(self) -> None:
        self.shut_down += 1


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        _FakeRecorder.instances = []
        patcher = mock.patch.object(
            stt_mod, "AudioToTextRecorder", _FakeRecorder,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self, **over) -> RealtimeSttService:
        return RealtimeSttService(_stt_settings(**over), _audio())


class LazyConstructionTests(_Base):
    def test_construction_loads_nothing(self) -> None:
        svc = self.service()
        self.assertEqual(_FakeRecorder.instances, [])
        self.assertFalse(svc.is_loaded)

    def test_but_it_still_reports_available(self) -> None:
        # The gate sites ask before any audio exists.
        self.assertTrue(self.service().is_available)

    def test_first_feed_triggers_the_load(self) -> None:
        svc = self.service()
        svc.feed_audio(b"\x00\x00" * 160)
        self.assertEqual(len(_FakeRecorder.instances), 1)
        self.assertTrue(svc.is_loaded)

    def test_the_load_happens_exactly_once(self) -> None:
        svc = self.service()
        for _ in range(5):
            svc.feed_audio(b"\x00\x00" * 160)
        self.assertEqual(len(_FakeRecorder.instances), 1)

    def test_start_context_triggers_the_load(self) -> None:
        svc = self.service()
        svc.start_context()
        self.assertEqual(len(_FakeRecorder.instances), 1)
        self.assertEqual(_FakeRecorder.instances[0].entered, 1)

    def test_prewarm_triggers_the_load(self) -> None:
        svc = self.service()
        self.assertTrue(svc.prewarm())
        self.assertEqual(len(_FakeRecorder.instances), 1)
        self.assertTrue(svc.prewarm(), "idempotent")
        self.assertEqual(len(_FakeRecorder.instances), 1)

    def test_reading_text_does_not_trigger_a_load(self) -> None:
        # Nothing was fed, so there is nothing to transcribe; loading
        # Whisper to return "" would stall the caller for seconds.
        svc = self.service()
        self.assertEqual(svc.text(), "")
        self.assertEqual(_FakeRecorder.instances, [])

    def test_text_works_once_loaded(self) -> None:
        svc = self.service()
        svc.feed_audio(b"\x00\x00" * 160)
        self.assertEqual(svc.text(), "hello there")

    def test_settings_reach_the_recorder(self) -> None:
        svc = self.service(model="small", compute_type="int8")
        svc.prewarm()
        kwargs = _FakeRecorder.instances[0].kwargs
        self.assertEqual(kwargs["model"], "small")
        self.assertEqual(kwargs["compute_type"], "int8")
        self.assertEqual(kwargs["device"], "cpu")
        self.assertFalse(kwargs["use_microphone"])


class DisabledTests(_Base):
    def test_disabled_never_loads(self) -> None:
        svc = self.service(enabled=False)
        svc.feed_audio(b"\x00\x00" * 160)
        svc.start_context()
        self.assertFalse(svc.prewarm())
        self.assertEqual(_FakeRecorder.instances, [])

    def test_disabled_is_not_available(self) -> None:
        self.assertFalse(self.service(enabled=False).is_available)

    def test_enabled_defaults_to_true(self) -> None:
        self.assertTrue(_stt_settings().enabled)

    def test_a_settings_object_without_enabled_is_treated_as_on(self) -> None:
        # Don't silently kill voice for an unexpected config shape.
        svc = RealtimeSttService(
            SimpleNamespace(
                model="base", language="en", device="cpu", compute_type="default",
            ),
            _audio(),
        )
        self.assertTrue(svc.is_available)


class FailedLoadTests(_Base):
    def test_a_failing_load_latches_and_is_not_retried(self) -> None:
        def _boom(**_kwargs):
            raise RuntimeError("no CUDA")

        with mock.patch.object(stt_mod, "AudioToTextRecorder", _boom):
            svc = self.service()
            for _ in range(3):
                svc.feed_audio(b"\x00\x00" * 160)
            self.assertFalse(svc.is_loaded)
            # A multi-second import must not be retried per audio chunk.
            self.assertFalse(svc.is_available)
            self.assertIn("no CUDA", svc._last_error or "")

    def test_missing_engine_is_unavailable(self) -> None:
        with mock.patch.object(stt_mod, "AudioToTextRecorder", None):
            svc = self.service()
            self.assertFalse(svc.is_available)
            self.assertFalse(svc.prewarm())


class DeferredImportTests(unittest.TestCase):
    """P27 deferred the recorder; this pins the deferral of the *import*.

    ``import RealtimeSTT`` pulls in torch and CTranslate2, which map close
    to a gigabyte of native libraries and, on Windows, two separate copies
    of the Intel OpenMP runtime -- a configuration that produces random
    access violations. A text-only session must not load any of it, and
    the availability gates run before any audio exists, so they have to
    answer without importing.
    """

    def test_availability_does_not_need_the_import(self) -> None:
        # Sentinel state = "never imported". Availability must still
        # resolve, via find_spec rather than execution.
        with mock.patch.object(
            stt_mod, "AudioToTextRecorder", stt_mod._NOT_IMPORTED,
        ):
            answered = stt_mod._engine_installed()
            # Whatever the answer, the attribute must remain untouched:
            # answering the question must not have triggered the import.
            self.assertIs(stt_mod.AudioToTextRecorder, stt_mod._NOT_IMPORTED)
        self.assertIsInstance(answered, bool)

    def test_a_patched_class_is_reported_installed(self) -> None:
        with mock.patch.object(stt_mod, "AudioToTextRecorder", _FakeRecorder):
            self.assertTrue(stt_mod._engine_installed())
            self.assertIs(stt_mod._recorder_class(), _FakeRecorder)

    def test_an_explicit_none_still_means_unavailable(self) -> None:
        # ``None`` is distinct from the sentinel: it means we looked and
        # the engine is not usable.
        with mock.patch.object(stt_mod, "AudioToTextRecorder", None):
            self.assertFalse(stt_mod._engine_installed())

    def test_constructing_the_service_imports_nothing(self) -> None:
        with mock.patch.object(
            stt_mod, "AudioToTextRecorder", stt_mod._NOT_IMPORTED,
        ):
            RealtimeSttService(_stt_settings(), _audio())
            self.assertIs(stt_mod.AudioToTextRecorder, stt_mod._NOT_IMPORTED)

    def test_a_failed_import_surfaces_as_an_error_not_a_crash(self) -> None:
        with mock.patch.object(stt_mod, "AudioToTextRecorder", None):
            svc = RealtimeSttService(_stt_settings(), _audio())
            with self.assertRaises(RuntimeError):
                svc._create_recorder()


class ShutdownTests(_Base):
    def test_shutdown_stops_a_loaded_recorder(self) -> None:
        svc = self.service()
        svc.prewarm()
        svc.shutdown()
        self.assertEqual(_FakeRecorder.instances[0].shut_down, 1)
        self.assertFalse(svc.is_loaded)

    def test_shutdown_before_any_load_is_a_no_op(self) -> None:
        svc = self.service()
        svc.shutdown()
        self.assertEqual(_FakeRecorder.instances, [])

    def test_a_feed_after_shutdown_does_not_reload(self) -> None:
        # Without the latch, teardown races a lazy reload of the very
        # model it is tearing down.
        svc = self.service()
        svc.prewarm()
        svc.shutdown()
        svc.feed_audio(b"\x00\x00" * 160)
        self.assertEqual(len(_FakeRecorder.instances), 1)
        self.assertFalse(svc.is_available)


class TranscribeTests(_Base):
    def test_transcribe_of_a_missing_file_loads_nothing(self) -> None:
        svc = self.service()
        self.assertEqual(svc.transcribe("does-not-exist.wav"), "")
        self.assertEqual(_FakeRecorder.instances, [])


class SettingsRoundTripTests(unittest.TestCase):
    def setUp(self) -> None:
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory

        import app.core.infra.settings as settings_mod

        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        # Pin user overrides at an empty path so a developer's local
        # config/user.json can't leak into these assertions.
        patcher = mock.patch.object(
            settings_mod,
            "USER_CONFIG_PATH",
            Path(self._tmp.name) / "user.json",
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        self._base = json.loads(default_path.read_text(encoding="utf-8"))
        self._json = json
        self._Path = Path

    def _load(self, stt_extra: dict):
        from app.core.infra.settings import load_settings

        cfg = dict(self._base)
        cfg["stt"] = {**cfg.get("stt", {}), **stt_extra}
        path = self._Path(self._tmp.name) / "config.json"
        path.write_text(self._json.dumps(cfg), encoding="utf-8")
        return load_settings(config_path=path)

    def test_enabled_parses_from_the_stt_block(self) -> None:
        self.assertFalse(self._load({"enabled": False}).stt.enabled)

    def test_absent_key_defaults_to_enabled(self) -> None:
        self.assertTrue(self._load({}).stt.enabled)

    def test_dataclass_default_matches_the_loader(self) -> None:
        self.assertTrue(SttSettings(model="base", language="en").enabled)


if __name__ == "__main__":
    unittest.main()
