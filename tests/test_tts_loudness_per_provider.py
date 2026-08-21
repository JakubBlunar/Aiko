"""Level matching is per-engine, and the two engines disagree on purpose.

The asymmetry is the whole point of this file. Applying level matching to
both engines is the *obvious* reading -- both drift by ~8 dB between
sentences -- and it was shipped that way and reported back as "she stopped
being lively". Measured afterwards on nine sentences of known intended
energy, 1.6 dB of pocket-tts's 5.1 dB spread tracks the intent, because
the model reads the text: a loud line is partly loud *because* it is
excited. So matching removes delivery along with drift, and whether that
trade is worth taking depends on how large the drift is relative to the
expression -- which differs by engine.

Without a test, the next person to notice both engines drift will make the
same change again for the same good reason.
"""
from __future__ import annotations

import unittest

from app.core.infra.settings import (
    TtsProviderSettings,
    TtsSettings,
    _parse_tts_providers,
)
from app.tts.pcm_playback import resolve_loudness_target

GLOBAL = -26.0


def _settings(**providers: TtsProviderSettings) -> TtsSettings:
    return TtsSettings(
        provider="pocket-tts",
        voice="",
        enabled=True,
        loudness_target_dbfs=GLOBAL,
        providers={k.replace("_", "-"): v for k, v in providers.items()},
    )


class DefaultsTests(unittest.TestCase):
    def test_pocket_tts_is_raw_even_though_the_global_is_set(self) -> None:
        """The revert. Her established voice is the baseline, not a drift."""
        got = resolve_loudness_target(
            _settings(), "pocket-tts", default=0.0,
        )
        self.assertEqual(got, 0.0)

    def test_a_cloning_engine_takes_the_global(self) -> None:
        got = resolve_loudness_target(
            _settings(), "chatterbox-nano", default=GLOBAL,
        )
        self.assertEqual(got, GLOBAL)

    def test_the_two_engines_land_differently_on_one_config(self) -> None:
        """Stated as one assertion, since the asymmetry is the contract."""
        settings = _settings()
        self.assertNotEqual(
            resolve_loudness_target(settings, "pocket-tts", default=0.0),
            resolve_loudness_target(
                settings, "chatterbox-nano", default=GLOBAL,
            ),
        )


class OverrideTests(unittest.TestCase):
    def test_pocket_tts_can_be_turned_back_on_for_an_ab(self) -> None:
        settings = _settings(
            pocket_tts=TtsProviderSettings(loudness_target_dbfs=-24.0),
        )
        self.assertEqual(
            resolve_loudness_target(settings, "pocket-tts", default=0.0),
            -24.0,
        )

    def test_a_cloning_engine_can_be_turned_off(self) -> None:
        settings = _settings(
            chatterbox_nano=TtsProviderSettings(loudness_target_dbfs=0.0),
        )
        self.assertEqual(
            resolve_loudness_target(
                settings, "chatterbox-nano", default=GLOBAL,
            ),
            0.0,
        )

    def test_an_override_for_one_engine_does_not_reach_another(self) -> None:
        settings = _settings(
            pocket_tts=TtsProviderSettings(loudness_target_dbfs=-24.0),
        )
        self.assertEqual(
            resolve_loudness_target(
                settings, "chatterbox-nano", default=GLOBAL,
            ),
            GLOBAL,
        )


class ParsingTests(unittest.TestCase):
    """``absent`` and ``0.0`` must stay distinguishable."""

    def test_an_absent_key_reads_as_unset_not_as_off(self) -> None:
        parsed = _parse_tts_providers({"pocket-tts": {"voice": "x"}})
        self.assertIsNone(parsed["pocket-tts"].loudness_target_dbfs)

    def test_an_explicit_zero_reads_as_off(self) -> None:
        parsed = _parse_tts_providers(
            {"pocket-tts": {"loudness_target_dbfs": 0.0}}
        )
        self.assertEqual(parsed["pocket-tts"].loudness_target_dbfs, 0.0)

    def test_a_real_target_survives(self) -> None:
        parsed = _parse_tts_providers(
            {"chatterbox-nano": {"loudness_target_dbfs": -20.0}}
        )
        self.assertEqual(
            parsed["chatterbox-nano"].loudness_target_dbfs, -20.0
        )

    def test_a_nonsense_target_falls_back_rather_than_disabling(self) -> None:
        """Positive dBFS is a misunderstanding, not a quiet target."""
        parsed = _parse_tts_providers(
            {"pocket-tts": {"loudness_target_dbfs": 6.0}}
        )
        self.assertEqual(parsed["pocket-tts"].loudness_target_dbfs, -26.0)

    def test_for_provider_does_not_invent_a_value(self) -> None:
        """Resolving here would hide 'unset' from the engine."""
        self.assertIsNone(
            _settings().for_provider("pocket-tts").loudness_target_dbfs
        )


class RobustnessTests(unittest.TestCase):
    def test_a_settings_object_without_the_accessor_still_builds(self) -> None:
        """Several engine tests pass a namespace, not real settings."""
        from types import SimpleNamespace

        bare = SimpleNamespace(loudness_target_dbfs=GLOBAL)
        self.assertEqual(
            resolve_loudness_target(bare, "pocket-tts", default=0.0), 0.0
        )
        self.assertEqual(
            resolve_loudness_target(
                bare, "chatterbox-nano", default=GLOBAL,
            ),
            GLOBAL,
        )

    def test_a_raising_accessor_falls_back_to_the_default(self) -> None:
        class Hostile:
            loudness_target_dbfs = GLOBAL

            def for_provider(self, _name):
                raise RuntimeError("no")

        self.assertEqual(
            resolve_loudness_target(Hostile(), "pocket-tts", default=0.0), 0.0
        )


if __name__ == "__main__":
    unittest.main()
