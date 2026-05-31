"""Unit tests for the ``user_display_name`` plumbing.

Phase 1 of the macOS publishable build introduces a configurable user
display name that replaces the historical hardcoded ``"Jacob"``
references. These tests pin the contract of:

  * ``AssistantSettings.user_display_name`` parsing + trimming.
  * ``resolve_user_display_name`` falling back to ``"friend"``.
  * ``is_onboarding_needed`` returning ``True`` only when the value is
    blank / whitespace.
  * ``session_text_utils.resolve_user_name`` swallowing exceptions and
    blank values.
  * ``session_text_utils.speaker_label`` mapping roles.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from app.core.infra import settings as settings_mod
from app.core.infra.settings import (
    AppSettings,
    AssistantSettings,
    USER_DISPLAY_NAME_FALLBACK,
    is_onboarding_needed,
    load_settings,
    resolve_user_display_name,
)
from app.core.session.session_text_utils import resolve_user_name, speaker_label


def _make_app_settings(name: str) -> AppSettings:
    """Build the minimum ``AppSettings`` slice the resolvers actually
    read. The real loader is exercised in ``LoaderTests`` below; this
    helper keeps the resolver / onboarding tests focused on behaviour."""
    settings = AppSettings.__new__(AppSettings)  # type: ignore[arg-type]
    settings.assistant = AssistantSettings(
        name="Aiko",
        remember_history=True,
        user_display_name=name,
    )
    return settings


class ResolverTests(unittest.TestCase):
    def test_blank_name_falls_back_to_friend(self) -> None:
        s = _make_app_settings("")
        self.assertEqual(resolve_user_display_name(s), USER_DISPLAY_NAME_FALLBACK)
        self.assertEqual(resolve_user_display_name(s), "friend")

    def test_whitespace_name_falls_back(self) -> None:
        s = _make_app_settings("   ")
        self.assertEqual(resolve_user_display_name(s), "friend")

    def test_set_name_round_trips(self) -> None:
        s = _make_app_settings("Aiko's friend")
        self.assertEqual(resolve_user_display_name(s), "Aiko's friend")


class OnboardingFlagTests(unittest.TestCase):
    def test_blank_name_needs_onboarding(self) -> None:
        self.assertTrue(is_onboarding_needed(_make_app_settings("")))
        self.assertTrue(is_onboarding_needed(_make_app_settings("  ")))

    def test_set_name_skips_onboarding(self) -> None:
        self.assertFalse(is_onboarding_needed(_make_app_settings("Bea")))


class ProviderHelperTests(unittest.TestCase):
    """``resolve_user_name`` is the shared helper every worker uses to
    fetch the current display name lazily, with safe fallbacks."""

    def test_none_provider_returns_fallback(self) -> None:
        self.assertEqual(resolve_user_name(None), "the user")
        self.assertEqual(resolve_user_name(None, fallback="anon"), "anon")

    def test_empty_provider_value_returns_fallback(self) -> None:
        self.assertEqual(resolve_user_name(lambda: ""), "the user")
        self.assertEqual(resolve_user_name(lambda: "   "), "the user")

    def test_raising_provider_returns_fallback(self) -> None:
        def boom() -> str:
            raise RuntimeError("nope")

        self.assertEqual(resolve_user_name(boom), "the user")

    def test_provider_returns_trimmed_value(self) -> None:
        self.assertEqual(resolve_user_name(lambda: "  Bea  "), "Bea")


class SpeakerLabelTests(unittest.TestCase):
    def test_user_role_uses_name(self) -> None:
        self.assertEqual(speaker_label("user", "Bea"), "Bea")
        self.assertEqual(speaker_label("USER", "Bea"), "Bea")

    def test_assistant_role_uses_aiko(self) -> None:
        self.assertEqual(speaker_label("assistant", "Bea"), "Aiko")
        self.assertEqual(speaker_label("system", "Bea"), "Aiko")

    def test_blank_name_falls_back(self) -> None:
        self.assertEqual(speaker_label("user", ""), "the user")
        self.assertEqual(speaker_label("user", "  "), "the user")

    def test_custom_assistant_name(self) -> None:
        self.assertEqual(
            speaker_label("assistant", "Bea", assistant_name="You"),
            "You",
        )


class LoaderTests(unittest.TestCase):
    """End-to-end loader test: ``user_display_name`` round-trips through
    the real ``load_settings`` function with both the default config and
    a user-overrides JSON file."""

    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.user_json = Path(self._tmp.name) / "user.json"
        patcher = mock.patch.object(
            settings_mod, "USER_CONFIG_PATH", self.user_json,
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        default_path = (
            Path(__file__).resolve().parents[1] / "config" / "default.json"
        )
        self._base_config = json.loads(default_path.read_text(encoding="utf-8"))

    def _write_config(self, assistant_extra: dict | None = None) -> Path:
        cfg = copy.deepcopy(self._base_config)
        if assistant_extra is not None:
            cfg["assistant"] = {**cfg.get("assistant", {}), **assistant_extra}
        path = Path(self._tmp.name) / "config.json"
        path.write_text(json.dumps(cfg), encoding="utf-8")
        return path

    def test_default_config_has_blank_display_name(self) -> None:
        path = self._write_config()
        settings = load_settings(path)
        self.assertEqual(settings.assistant.user_display_name, "")
        self.assertTrue(is_onboarding_needed(settings))

    def test_user_overrides_persist_display_name(self) -> None:
        path = self._write_config()
        self.user_json.write_text(
            json.dumps({"assistant": {"user_display_name": "Bea"}}),
            encoding="utf-8",
        )
        settings = load_settings(path)
        self.assertEqual(settings.assistant.user_display_name, "Bea")
        self.assertEqual(resolve_user_display_name(settings), "Bea")
        self.assertFalse(is_onboarding_needed(settings))

    def test_overlong_name_is_truncated(self) -> None:
        path = self._write_config()
        self.user_json.write_text(
            json.dumps({"assistant": {"user_display_name": "x" * 100}}),
            encoding="utf-8",
        )
        settings = load_settings(path)
        # Loader caps to 32 chars per Phase 1 spec.
        self.assertLessEqual(len(settings.assistant.user_display_name), 32)


if __name__ == "__main__":
    unittest.main()
