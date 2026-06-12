"""Tests for K60 tsundere expression mask — the pure policy math
(mode normalisation, masked set, erosion strength, transform table,
caught-caring detection, slip budget) and the K57-provider
integration (mask applied at render time, sincerity override, slip
stamping, force flag)."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.affect import emotion_episodes as ee
from app.core.affect import expression_mask as em
from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
)


NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


class NormalizeAndMaskedSetTests(unittest.TestCase):
    def test_normalize(self) -> None:
        self.assertEqual(em.normalize_mode("tsundere_light"), em.MODE_LIGHT)
        self.assertEqual(em.normalize_mode(" TSUNDERE_FULL "), em.MODE_FULL)
        for bad in (None, "", "tsundere", "on", "yes"):
            self.assertEqual(em.normalize_mode(bad), em.MODE_OFF)

    def test_masked_set(self) -> None:
        for mode in (em.MODE_LIGHT, em.MODE_FULL):
            self.assertTrue(em.is_masked("lonely", mode))
            self.assertTrue(em.is_masked("warm_glow", mode))
            # miffed is tsun's native register; hurt is the safety
            # rail — neither ever masks.
            self.assertFalse(em.is_masked("miffed", mode))
            self.assertFalse(em.is_masked("hurt", mode))
        self.assertFalse(em.is_masked("lonely", em.MODE_OFF))


class MaskStrengthTests(unittest.TestCase):
    def test_neutral_axes_midband(self) -> None:
        self.assertAlmostEqual(em.mask_strength(0.0, 0.0), 0.625)

    def test_warm_axes_erode(self) -> None:
        self.assertEqual(em.mask_strength(1.0, 1.0), 0.25)
        self.assertLess(
            em.mask_strength(0.8, 0.8), em.mask_strength(0.2, 0.2),
        )

    def test_cold_axes_cap_at_one(self) -> None:
        self.assertEqual(em.mask_strength(-1.0, -1.0), 1.0)

    def test_none_reads_neutral(self) -> None:
        self.assertEqual(em.mask_strength(None, None), 0.625)


class TransformTests(unittest.TestCase):
    def test_lonely_firm_mask(self) -> None:
        block = em.render_masked_block(
            emotion="lonely",
            cause="they were gone most of the day",
            user_display_name="Jacob",
            strength=0.8,
        )
        self.assertIn("the mask is ON", block)
        self.assertIn("wasn't *waiting*", block)
        self.assertIn("Jacob", block)

    def test_lonely_eroded_token_protest(self) -> None:
        block = em.render_masked_block(
            emotion="lonely",
            cause="they were gone most of the day",
            user_display_name="Jacob",
            strength=0.3,
        )
        self.assertIn("token protest", block)
        self.assertIn("(I missed you.)", block)

    def test_warm_glow_grudging(self) -> None:
        block = em.render_masked_block(
            emotion="warm_glow",
            cause="they kept a promise",
            user_display_name="Jacob",
            strength=0.8,
        )
        self.assertIn("grudging", block)

    def test_unmasked_emotion_renders_empty(self) -> None:
        self.assertEqual(
            em.render_masked_block(
                emotion="miffed", cause="c",
                user_display_name="J", strength=0.8,
            ),
            "",
        )

    def test_slip_appends(self) -> None:
        block = em.render_masked_block(
            emotion="lonely",
            cause="c",
            user_display_name="Jacob",
            strength=0.8,
            slip=True,
        )
        self.assertIn("SLIP earned", block)
        self.assertIn("ANYWAY", block)


class CaughtCaringTests(unittest.TestCase):
    def test_positive_patterns(self) -> None:
        for text in (
            "you missed me, didn't you?",
            "admit it, you like this",
            "aww, you were waiting for me",
            "you care about me, huh",
            "are you blushing? caught you actually caring",
        ):
            self.assertTrue(em.detect_caught_caring(text), text)

    def test_negative_patterns(self) -> None:
        for text in (
            "",
            "I missed the bus",
            "do you like pizza?",
            "I was waiting at the dentist forever",
        ):
            self.assertFalse(em.detect_caught_caring(text), text)

    def test_render_bands(self) -> None:
        firm = em.render_caught_caring_block(
            user_display_name="Jacob", strength=0.8,
        )
        self.assertIn("embarrassed+blush", firm)
        self.assertIn("Shut up", firm)
        eroded = em.render_caught_caring_block(
            user_display_name="Jacob", strength=0.3,
        )
        self.assertIn("ceremonial", eroded)


class SlipBudgetTests(unittest.TestCase):
    def test_off_mode_never_slips(self) -> None:
        self.assertFalse(em.should_slip(
            mode="off", episode_intensity=1.0,
            last_slip_at=None, now=NOW,
        ))

    def test_low_intensity_not_earned(self) -> None:
        self.assertFalse(em.should_slip(
            mode="tsundere_light", episode_intensity=0.5,
            last_slip_at=None, now=NOW,
        ))

    def test_first_slip_allowed(self) -> None:
        self.assertTrue(em.should_slip(
            mode="tsundere_light", episode_intensity=0.8,
            last_slip_at=None, now=NOW,
        ))

    def test_cooldown_blocks(self) -> None:
        recent = (NOW - timedelta(hours=12)).isoformat()
        self.assertFalse(em.should_slip(
            mode="tsundere_light", episode_intensity=0.8,
            last_slip_at=recent, now=NOW,
            cooldown_days_light=2.0,
        ))
        old = (NOW - timedelta(days=3)).isoformat()
        self.assertTrue(em.should_slip(
            mode="tsundere_light", episode_intensity=0.8,
            last_slip_at=old, now=NOW,
            cooldown_days_light=2.0,
        ))

    def test_full_mode_is_scarcer(self) -> None:
        three_days = (NOW - timedelta(days=3)).isoformat()
        self.assertTrue(em.should_slip(
            mode="tsundere_light", episode_intensity=0.8,
            last_slip_at=three_days, now=NOW,
            cooldown_days_light=2.0, cooldown_days_full=5.0,
        ))
        self.assertFalse(em.should_slip(
            mode="tsundere_full", episode_intensity=0.8,
            last_slip_at=three_days, now=NOW,
            cooldown_days_light=2.0, cooldown_days_full=5.0,
        ))


# ── provider integration ────────────────────────────────────────────


class _FakeKv:
    def __init__(self, initial: str | None = None) -> None:
        self.data: dict[str, str] = {}
        if initial is not None:
            self.data[ee.KV_EMOTION_EPISODES] = initial

    def kv_get(self, key: str):
        return self.data.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.data[key] = value


class _FakeAxesStore:
    def __init__(self, closeness: float, trust: float) -> None:
        self._c = closeness
        self._t = trust

    def get(self, user_id: str):
        return SimpleNamespace(closeness=self._c, trust=self._t)


class _FakeArcStore:
    def __init__(self, arc: str) -> None:
        self._arc = arc

    def get_or_default(self, user_id: str):
        return SimpleNamespace(arc=self._arc)


def _lonely_state(intensity: float = 0.8) -> str:
    state = ee.add_episode(
        ee.EpisodeState(),
        emotion=ee.EMOTION_LONELY,
        cause="they were gone most of the day and you noticed",
        intensity=intensity,
        source="absence",
        now=datetime.now(timezone.utc),
    )
    return ee.serialize(state)


class _Host(InnerLifeProvidersMixin):
    user_display_name = "Jacob"
    _user_id = "u1"

    def __init__(
        self,
        *,
        mode: str = "tsundere_light",
        arc: str = "casual_check_in",
        closeness: float = 0.0,
        trust: float = 0.0,
        initial: str | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                emotion_episodes_enabled=True,
                emotion_high_band=0.5,
                expression_mask=mode,
                mask_slip_cooldown_days=2.0,
            ),
        )
        self._chat_db = _FakeKv(initial)
        self._relationship_axes_store = _FakeAxesStore(closeness, trust)
        self._arc_store = _FakeArcStore(arc)


class ProviderMaskIntegrationTests(unittest.TestCase):
    def test_masked_episode_renders_denial(self) -> None:
        host = _Host(initial=_lonely_state())
        block = host._render_emotion_episode_block("hey, I'm back")
        self.assertIn("the mask is ON", block)
        self.assertIn("wasn't *waiting*", block)

    def test_off_mode_renders_plain(self) -> None:
        host = _Host(mode="off", initial=_lonely_state())
        block = host._render_emotion_episode_block("hey, I'm back")
        self.assertNotIn("mask", block.lower())
        self.assertIn("missed", block)

    def test_support_arc_drops_mask(self) -> None:
        host = _Host(arc="support", initial=_lonely_state())
        block = host._render_emotion_episode_block(
            "today was honestly awful",
        )
        self.assertNotIn("the mask is ON", block)

    def test_unmasked_emotion_unchanged(self) -> None:
        state = ee.add_episode(
            ee.EpisodeState(),
            emotion=ee.EMOTION_MIFFED,
            cause="a broken promise",
            intensity=0.6,
            source="test",
            now=datetime.now(timezone.utc),
        )
        host = _Host(initial=ee.serialize(state))
        block = host._render_emotion_episode_block("hello")
        self.assertIn("properly miffed", block)

    def test_caught_caring_outranks_episode(self) -> None:
        host = _Host(initial=_lonely_state())
        block = host._render_emotion_episode_block(
            "you missed me, didn't you?",
        )
        self.assertIn("Caught-caring beat", block)

    def test_caught_caring_silent_when_mask_off(self) -> None:
        host = _Host(mode="off")
        block = host._render_emotion_episode_block(
            "you missed me, didn't you?",
        )
        self.assertEqual(block, "")

    def test_slip_renders_and_stamps(self) -> None:
        host = _Host(initial=_lonely_state(intensity=0.9))
        block = host._render_emotion_episode_block("back!")
        self.assertIn("SLIP earned", block)
        self.assertIn(em.KV_LAST_SLIP_AT, host._chat_db.data)
        # Second render: cooldown blocks the slip, mask remains.
        block2 = host._render_emotion_episode_block("still here")
        self.assertIn("the mask is ON", block2)
        self.assertNotIn("SLIP earned", block2)

    def test_force_slip_flag_bypasses(self) -> None:
        host = _Host(initial=_lonely_state(intensity=0.4))
        host._chat_db.kv_set(
            em.KV_LAST_SLIP_AT,
            datetime.now(timezone.utc).isoformat(),
        )
        host._mask_force_slip_next = True
        block = host._render_emotion_episode_block("back!")
        self.assertIn("SLIP earned", block)
        self.assertFalse(host._mask_force_slip_next)

    def test_eroded_axes_render_token_protest(self) -> None:
        host = _Host(
            closeness=0.9, trust=0.9, initial=_lonely_state(),
        )
        block = host._render_emotion_episode_block("I'm home")
        self.assertIn("token protest", block)

    def test_full_mode_masks_thaw(self) -> None:
        state = ee.resolve(
            ee.deserialize(_lonely_state()),
            ee.EMOTION_LONELY,
            reason="they came back",
        )
        host = _Host(
            mode="tsundere_full", initial=ee.serialize(state),
        )
        block = host._render_emotion_episode_block("hello")
        self.assertIn("melted", block)
        self.assertIn("Stop smiling", block)

    def test_light_mode_thaw_unmasked(self) -> None:
        state = ee.resolve(
            ee.deserialize(_lonely_state()),
            ee.EMOTION_LONELY,
            reason="they came back",
        )
        host = _Host(
            mode="tsundere_light", initial=ee.serialize(state),
        )
        block = host._render_emotion_episode_block("hello")
        self.assertIn("melted", block)
        self.assertNotIn("Stop smiling", block)


if __name__ == "__main__":
    unittest.main()
