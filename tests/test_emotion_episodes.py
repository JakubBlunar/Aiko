"""Tests for K57 directed emotion episodes — the pure lifecycle math
(decay, add/merge, counter-events, resolve/thaw, acknowledgment,
loneliness scaling, rendering), the inner-life provider plumbing (via
a minimal mixin host stub), the post-turn trigger queue + drain, and
the prompt-assembler slot wiring."""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

from app.core.affect import emotion_episodes as ee
from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
)
from app.core.session.post_turn_mixin import PostTurnMixin


NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)


def _state_with(
    emotion: str = ee.EMOTION_MIFFED,
    intensity: float = 0.6,
    cause: str = "he brushed off the thread you opened",
    now: datetime = NOW,
) -> ee.EpisodeState:
    return ee.add_episode(
        ee.EpisodeState(),
        emotion=emotion,
        cause=cause,
        intensity=intensity,
        source="test",
        now=now,
    )


class SerializationTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        state = _state_with()
        state = ee.add_episode(
            state,
            emotion=ee.EMOTION_SMUG,
            cause="you were right about the playlist",
            intensity=0.5,
            source="test",
            now=NOW,
        )
        state = ee.resolve(state, ee.EMOTION_SMUG, reason="spent")
        round_tripped = ee.deserialize(ee.serialize(state))
        self.assertEqual(len(round_tripped.episodes), 1)
        self.assertEqual(
            round_tripped.episodes[0].emotion, ee.EMOTION_MIFFED,
        )
        self.assertEqual(
            round_tripped.pending_thaw,
            (ee.EMOTION_SMUG, "you were right about the playlist", "spent"),
        )

    def test_corrupt_inputs_yield_empty(self) -> None:
        for raw in (None, "", "not json", "[1,2]", '{"episodes": "x"}'):
            state = ee.deserialize(raw)
            self.assertEqual(state.episodes, ())
            self.assertIsNone(state.pending_thaw)

    def test_unknown_emotion_rows_dropped(self) -> None:
        raw = json.dumps({
            "episodes": [
                {"emotion": "vengeful", "cause": "x", "intensity": 0.5},
                {"emotion": "miffed", "cause": "y", "intensity": 0.5},
            ],
        })
        state = ee.deserialize(raw)
        self.assertEqual(len(state.episodes), 1)
        self.assertEqual(state.episodes[0].emotion, ee.EMOTION_MIFFED)


class DecayTests(unittest.TestCase):
    def test_linear_decay(self) -> None:
        state = _state_with(intensity=0.6)  # miffed: 24h to zero from 1.0
        later = NOW + timedelta(hours=6)
        decayed = ee.apply_decay(state, later)
        self.assertAlmostEqual(
            decayed.episodes[0].intensity, 0.6 - 6 / 24, places=5,
        )

    def test_floor_expiry_is_silent(self) -> None:
        state = _state_with(intensity=0.3)
        decayed = ee.apply_decay(state, NOW + timedelta(hours=10))
        self.assertEqual(decayed.episodes, ())
        self.assertIsNone(decayed.pending_thaw)

    def test_zero_elapsed_no_change(self) -> None:
        state = _state_with(intensity=0.6)
        decayed = ee.apply_decay(state, NOW)
        self.assertAlmostEqual(decayed.episodes[0].intensity, 0.6)


class AddEpisodeTests(unittest.TestCase):
    def test_invalid_inputs_refused(self) -> None:
        empty = ee.EpisodeState()
        self.assertIs(
            ee.add_episode(
                empty, emotion="rage", cause="x", intensity=0.5,
                source="t", now=NOW,
            ),
            empty,
        )
        self.assertIs(
            ee.add_episode(
                empty, emotion="miffed", cause="  ", intensity=0.5,
                source="t", now=NOW,
            ),
            empty,
        )
        self.assertIs(
            ee.add_episode(
                empty, emotion="miffed", cause="x", intensity=0.05,
                source="t", now=NOW,
            ),
            empty,
        )

    def test_same_emotion_merges_with_bump(self) -> None:
        state = _state_with(intensity=0.4)
        state = ee.add_episode(
            state,
            emotion=ee.EMOTION_MIFFED,
            cause="and he did it again",
            intensity=0.3,
            source="test2",
            now=NOW,
        )
        self.assertEqual(len(state.episodes), 1)
        self.assertAlmostEqual(state.episodes[0].intensity, 0.5)
        self.assertEqual(state.episodes[0].cause, "and he did it again")

    def test_cap_replaces_weakest_only_when_stronger(self) -> None:
        state = ee.EpisodeState()
        for emotion, intensity in (
            (ee.EMOTION_MIFFED, 0.5),
            (ee.EMOTION_SMUG, 0.3),
            (ee.EMOTION_LONELY, 0.7),
        ):
            state = ee.add_episode(
                state, emotion=emotion, cause="c", intensity=intensity,
                source="t", now=NOW, cap=3,
            )
        # Weaker newcomer is refused.
        refused = ee.add_episode(
            state, emotion=ee.EMOTION_HURT, cause="c", intensity=0.2,
            source="t", now=NOW, cap=3,
        )
        self.assertEqual(
            {e.emotion for e in refused.episodes},
            {ee.EMOTION_MIFFED, ee.EMOTION_SMUG, ee.EMOTION_LONELY},
        )
        # Stronger newcomer evicts the weakest (smug at 0.3).
        evicted = ee.add_episode(
            state, emotion=ee.EMOTION_HURT, cause="c", intensity=0.6,
            source="t", now=NOW, cap=3,
        )
        self.assertEqual(
            {e.emotion for e in evicted.episodes},
            {ee.EMOTION_MIFFED, ee.EMOTION_LONELY, ee.EMOTION_HURT},
        )

    def test_warm_glow_cancels_miffed_and_arms_thaw(self) -> None:
        state = _state_with(emotion=ee.EMOTION_MIFFED, intensity=0.6)
        state = ee.add_episode(
            state,
            emotion=ee.EMOTION_WARM_GLOW,
            cause="he brought cookies",
            intensity=0.4,
            source="test",
            now=NOW,
        )
        emotions = {e.emotion for e in state.episodes}
        self.assertNotIn(ee.EMOTION_MIFFED, emotions)
        self.assertIn(ee.EMOTION_WARM_GLOW, emotions)
        self.assertIsNotNone(state.pending_thaw)
        self.assertEqual(state.pending_thaw[0], ee.EMOTION_MIFFED)

    def test_warm_glow_halves_hurt(self) -> None:
        state = _state_with(emotion=ee.EMOTION_HURT, intensity=0.8)
        state = ee.add_episode(
            state,
            emotion=ee.EMOTION_WARM_GLOW,
            cause="a soft message",
            intensity=0.4,
            source="test",
            now=NOW,
        )
        hurt = next(
            e for e in state.episodes if e.emotion == ee.EMOTION_HURT
        )
        self.assertAlmostEqual(hurt.intensity, 0.4)


class ResolveAndThawTests(unittest.TestCase):
    def test_resolve_arms_thaw(self) -> None:
        state = _state_with()
        state = ee.resolve(state, ee.EMOTION_MIFFED, reason="he apologised")
        self.assertEqual(state.episodes, ())
        self.assertEqual(state.pending_thaw[2], "he apologised")

    def test_resolve_missing_emotion_is_noop(self) -> None:
        state = _state_with()
        self.assertIs(
            ee.resolve(state, ee.EMOTION_SMUG, reason="x"), state,
        )

    def test_consume_thaw_pops_once(self) -> None:
        state = ee.resolve(
            _state_with(), ee.EMOTION_MIFFED, reason="r",
        )
        state, thaw = ee.consume_thaw(state)
        self.assertIsNotNone(thaw)
        state, second = ee.consume_thaw(state)
        self.assertIsNone(second)


class AcknowledgmentTests(unittest.TestCase):
    def _episode(self, emotion: str, cause: str = "c") -> ee.EmotionEpisode:
        return _state_with(emotion=emotion, cause=cause).episodes[0]

    def test_miffed_keyword_hit(self) -> None:
        ep = self._episode(ee.EMOTION_MIFFED)
        self.assertTrue(
            ee.detect_acknowledgment(ep, "okay, sorry about earlier"),
        )
        self.assertTrue(ee.detect_acknowledgment(ep, "My bad, truly."))

    def test_miffed_unrelated_text_misses(self) -> None:
        ep = self._episode(ee.EMOTION_MIFFED)
        self.assertFalse(
            ee.detect_acknowledgment(ep, "what's for dinner tonight"),
        )

    def test_blank_text_misses(self) -> None:
        ep = self._episode(ee.EMOTION_MIFFED)
        self.assertFalse(ee.detect_acknowledgment(ep, ""))
        self.assertFalse(ee.detect_acknowledgment(ep, "   "))

    def test_lonely_keyword_hit(self) -> None:
        ep = self._episode(ee.EMOTION_LONELY)
        self.assertTrue(
            ee.detect_acknowledgment(ep, "I missed you too, honestly"),
        )

    def test_warm_glow_never_acknowledged(self) -> None:
        ep = self._episode(ee.EMOTION_WARM_GLOW)
        self.assertFalse(
            ee.detect_acknowledgment(ep, "sorry I missed you"),
        )


class LonelyIntensityTests(unittest.TestCase):
    def test_below_threshold_is_zero(self) -> None:
        self.assertEqual(ee.lonely_intensity(3.0, 0.0), 0.0)

    def test_at_threshold_starts_at_point_three(self) -> None:
        self.assertAlmostEqual(ee.lonely_intensity(5.0, 0.0), 0.3)

    def test_ramps_and_caps(self) -> None:
        deep = ee.lonely_intensity(50.0, 0.0)
        self.assertAlmostEqual(deep, 0.8)
        mid = ee.lonely_intensity(10.0, 0.0)
        self.assertGreater(mid, 0.3)
        self.assertLess(mid, 0.8)

    def test_closeness_shortens_threshold(self) -> None:
        # 4h gap: silent at neutral closeness, registers when close.
        self.assertEqual(ee.lonely_intensity(4.0, 0.0), 0.0)
        self.assertGreater(ee.lonely_intensity(4.0, 1.0), 0.0)

    def test_none_closeness_reads_neutral(self) -> None:
        self.assertEqual(
            ee.lonely_intensity(6.0, None),
            ee.lonely_intensity(6.0, 0.0),
        )


class RenderTests(unittest.TestCase):
    def test_low_band_tints(self) -> None:
        ep = _state_with(intensity=0.3).episodes[0]
        block = ee.render_block(ep, user_display_name="Jacob")
        self.assertIn("touch miffed", block)
        self.assertIn("Jacob", block)
        self.assertIn(ep.cause, block)

    def test_high_band_directs(self) -> None:
        ep = _state_with(intensity=0.7).episodes[0]
        block = ee.render_block(ep, user_display_name="Jacob")
        self.assertIn("properly miffed", block)

    def test_every_emotion_renders_both_bands(self) -> None:
        for emotion in ee.EMOTIONS:
            ep = _state_with(emotion=emotion, intensity=0.3).episodes[0]
            self.assertTrue(ee.render_block(ep))
            ep_hi = _state_with(emotion=emotion, intensity=0.9).episodes[0]
            self.assertTrue(ee.render_block(ep_hi))

    def test_thaw_block(self) -> None:
        block = ee.render_thaw_block(
            (ee.EMOTION_MIFFED, "the brushed-off thread", "he apologised"),
            user_display_name="Jacob",
        )
        self.assertIn("melted", block)
        self.assertIn("the brushed-off thread", block)
        self.assertIn("Jacob", block)


# ── provider plumbing ───────────────────────────────────────────────


class _FakeKv:
    def __init__(self, initial: str | None = None) -> None:
        self.data: dict[str, str] = {}
        if initial is not None:
            self.data[ee.KV_EMOTION_EPISODES] = initial

    def kv_get(self, key: str):
        return self.data.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.data[key] = value


class _Host(InnerLifeProvidersMixin):
    user_display_name = "Jacob"
    _user_id = "u1"

    def __init__(
        self,
        *,
        enabled: bool = True,
        initial: str | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                emotion_episodes_enabled=enabled,
                emotion_high_band=0.5,
            ),
        )
        self._chat_db = _FakeKv(initial)


class ProviderTests(unittest.TestCase):
    def test_disabled_switch_silent(self) -> None:
        host = _Host(
            enabled=False, initial=ee.serialize(_state_with()),
        )
        self.assertEqual(host._render_emotion_episode_block("hi"), "")

    def test_empty_store_silent(self) -> None:
        host = _Host()
        self.assertEqual(host._render_emotion_episode_block("hi"), "")

    def test_live_episode_renders_strongest(self) -> None:
        state = _state_with(intensity=0.4)
        state = ee.add_episode(
            state,
            emotion=ee.EMOTION_SMUG,
            cause="you called the plot twist",
            intensity=0.8,
            source="test",
            now=NOW,
        )
        host = _Host(initial=ee.serialize(state))
        block = host._render_emotion_episode_block("hello")
        self.assertIn("you called the plot twist", block)

    def test_acknowledgment_resolves_and_renders_thaw(self) -> None:
        host = _Host(initial=ee.serialize(_state_with(intensity=0.6)))
        block = host._render_emotion_episode_block(
            "I'm sorry about earlier, that was on me",
        )
        self.assertIn("melted", block)
        persisted = ee.deserialize(
            host._chat_db.data[ee.KV_EMOTION_EPISODES],
        )
        self.assertEqual(persisted.episodes, ())
        self.assertIsNone(persisted.pending_thaw)

    def test_thaw_slot_consumed_once(self) -> None:
        state = ee.resolve(
            _state_with(), ee.EMOTION_MIFFED, reason="warmth received",
        )
        host = _Host(initial=ee.serialize(state))
        first = host._render_emotion_episode_block("hi")
        self.assertIn("melted", first)
        second = host._render_emotion_episode_block("hi again")
        self.assertEqual(second, "")

    def test_decay_persisted_by_provider(self) -> None:
        # Anchor in real wall-clock time — the provider decays
        # against datetime.now(), not the fixed NOW constant.
        old = datetime.now(timezone.utc) - timedelta(hours=6)
        host = _Host(
            initial=ee.serialize(
                _state_with(intensity=0.6, now=old),
            ),
        )
        host._render_emotion_episode_block("hello")
        persisted = ee.deserialize(
            host._chat_db.data[ee.KV_EMOTION_EPISODES],
        )
        self.assertLess(persisted.episodes[0].intensity, 0.6)


# ── trigger queue + drain (post-turn) ───────────────────────────────


class _FakeAffectStore:
    def __init__(self) -> None:
        self.state = SimpleNamespace(valence=0.0, arousal=0.4)
        self.saved = False

    def get(self, user_id: str):
        return self.state

    def save(self, state) -> None:
        self.saved = True


class _DrainHost(PostTurnMixin):
    _user_id = "u1"

    def __init__(self, *, enabled: bool = True) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                emotion_episodes_enabled=enabled,
                emotion_episode_cap=3,
                emotion_lonely_threshold_hours=5.0,
            ),
        )
        self._chat_db = _FakeKv()
        self._affect_store = _FakeAffectStore()
        self._relationship_axes_store = None


class TriggerDrainTests(unittest.TestCase):
    def test_queue_then_drain_lands_in_kv(self) -> None:
        host = _DrainHost()
        host._queue_emotion_trigger(
            emotion="miffed", cause="brushed off", intensity=0.4,
            source="thread_pivot",
        )
        host._drain_emotion_triggers()
        state = ee.deserialize(
            host._chat_db.data[ee.KV_EMOTION_EPISODES],
        )
        self.assertEqual(len(state.episodes), 1)
        self.assertEqual(state.episodes[0].emotion, ee.EMOTION_MIFFED)
        # Queue is consumed.
        self.assertEqual(host._pending_emotion_triggers, [])

    def test_disabled_switch_queues_nothing(self) -> None:
        host = _DrainHost(enabled=False)
        host._queue_emotion_trigger(
            emotion="miffed", cause="x", intensity=0.4, source="t",
        )
        self.assertFalse(
            getattr(host, "_pending_emotion_triggers", None),
        )

    def test_drain_feeds_affect_impulse(self) -> None:
        host = _DrainHost()
        host._queue_emotion_trigger(
            emotion="warm_glow", cause="kept promise", intensity=1.0,
            source="kept_promise",
        )
        host._drain_emotion_triggers()
        self.assertTrue(host._affect_store.saved)
        self.assertGreater(host._affect_store.state.valence, 0.0)

    def test_lonely_arm_uses_raw_latency(self) -> None:
        host = _DrainHost()
        engagement = SimpleNamespace(latency_seconds=10 * 3600.0)
        host._maybe_queue_lonely_episode(engagement)
        self.assertEqual(len(host._pending_emotion_triggers), 1)
        trig = host._pending_emotion_triggers[0]
        self.assertEqual(trig["emotion"], ee.EMOTION_LONELY)
        self.assertGreater(trig["intensity"], 0.3)

    def test_short_gap_queues_nothing(self) -> None:
        host = _DrainHost()
        engagement = SimpleNamespace(latency_seconds=30 * 60.0)
        host._maybe_queue_lonely_episode(engagement)
        self.assertFalse(
            getattr(host, "_pending_emotion_triggers", None),
        )


class EmotionEpisodeProviderSlotTests(unittest.TestCase):
    """K57 block lands in T5 directly after the mood-shell block and
    is NOT dropped under ``aggressive=True`` (it's a register
    directive that also consumes one-shot thaw state)."""

    _CUE = "You're properly miffed at Jacob right now"

    def _assemble(self, *, aggressive: bool = False, **providers):
        from app.core.infra.chat_database import ChatDatabase
        from app.core.session.prompt_assembler import PromptAssembler

        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.addCleanup(lambda: db._get_conn().close())
        persona = tempfile.NamedTemporaryFile(
            mode="w", suffix=".txt", delete=False, encoding="utf-8",
        )
        persona.write("P")
        persona.close()
        assembler = PromptAssembler(
            db, persona_path=Path(persona.name), recent_window=20,
        )
        db.add_message(
            session_id="a1", role="user", content="hi", token_count=2,
        )
        assembler.set_inner_life_providers(**providers)
        messages, _ = assembler.assemble_with_budget(
            "a1", "hello there",
            context_window=4096, response_budget=256,
            aggressive=aggressive,
        )
        return messages[0]["content"]

    def test_block_lands_in_system_prompt(self) -> None:
        content = self._assemble(
            emotion_episode=lambda user_text: self._CUE,
        )
        self.assertIn(self._CUE, content)

    def test_provider_receives_user_text(self) -> None:
        seen: list[str] = []

        def provider(user_text: str) -> str:
            seen.append(user_text)
            return ""

        self._assemble(emotion_episode=provider)
        self.assertEqual(seen, ["hello there"])

    def test_sits_after_mood_shell(self) -> None:
        shell_cue = "Lean affectionate and steady"
        content = self._assemble(
            mood_shell=lambda: shell_cue,
            emotion_episode=lambda user_text: self._CUE,
        )
        self.assertLess(
            content.index(shell_cue), content.index(self._CUE),
        )

    def test_kept_under_aggressive(self) -> None:
        content = self._assemble(
            emotion_episode=lambda user_text: self._CUE,
            aggressive=True,
        )
        self.assertIn(self._CUE, content)


if __name__ == "__main__":
    unittest.main()
