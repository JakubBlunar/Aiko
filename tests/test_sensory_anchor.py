"""K24 unit tests -- ``app.core.conversation.sensory_anchor``.

Pure-module test coverage: posture-kind compatibility matrix,
quantity-weighted item pick, no-repeat ring, cooldown decrement,
arc-weighted probability distribution under a fixed-seed RNG,
and the render output. No DB, no controller, no embedder.
"""
from __future__ import annotations

import random
import unittest
from dataclasses import dataclass

from app.core.conversation.sensory_anchor import (
    SensoryAnchorCadence,
    SensoryBeat,
    _ARC_WEIGHTS,
    _POSTURE_KIND_VERBS,
    _VERB_CLASS_HINT,
    pick_beat,
    render_inner_life_block,
)


# Minimal fake item that exposes only the attributes ``pick_beat``
# reads (``slug``, ``name``, ``kind``, ``quantity``). Mirrors the
# subset of :class:`world_store.Item` we depend on -- keeps the
# tests independent of the world_store schema.
@dataclass
class _FakeItem:
    slug: str
    name: str
    kind: str
    quantity: int = 1


# ── pick_beat ───────────────────────────────────────────────────────


class PickBeatPostureKindMatrixTests(unittest.TestCase):
    """Posture-kind compatibility: incompatible combos drop silently;
    compatible combos return a beat with the expected verb_class."""

    def test_lying_with_only_gadget_returns_none(self) -> None:
        # ``(lying, gadget)`` is intentionally empty -- you can't
        # reach for the keyboard when you're stretched out on the
        # bed. The pool has one item; the filter empties it; the
        # selector returns None.
        items = [_FakeItem(slug="retro_keyboard", name="retro keyboard", kind="gadget")]
        beat = pick_beat(
            posture="lying",
            items=items,
            arc="reflection",
            rng=random.Random(0),
        )
        self.assertIsNone(beat)

    def test_lying_with_only_keepsake_returns_compatible_verb(self) -> None:
        items = [_FakeItem(slug="photo", name="photo of Jacob", kind="keepsake")]
        beat = pick_beat(
            posture="lying",
            items=items,
            arc="reflection",
            rng=random.Random(0),
        )
        assert beat is not None
        self.assertEqual(beat.item_slug, "photo")
        # The verb class must come from the (lying, keepsake) tuple.
        self.assertIn(
            beat.verb_class, _POSTURE_KIND_VERBS[("lying", "keepsake")],
        )

    def test_sitting_picks_only_eligible_when_mixed_pool(self) -> None:
        # Pool mixes an incompatible kind with one that has only
        # one eligible item under "sitting". The selector must
        # ignore the incompatible row even with a small RNG window.
        items = [
            _FakeItem(slug="bookshelf", name="bookshelf", kind="furniture"),
            _FakeItem(slug="tea_pot", name="tea pot", kind="gadget"),
        ]
        beat = pick_beat(
            posture="sitting",
            items=items,
            arc="support",
            rng=random.Random(42),
        )
        assert beat is not None
        self.assertEqual(beat.item_slug, "tea_pot")

    def test_unknown_posture_returns_none(self) -> None:
        # A future world might add a posture (e.g. "kneeling") that
        # the static matrix doesn't know about. The selector must
        # fail closed -- silent, not crashing.
        items = [_FakeItem(slug="tea_pot", name="tea pot", kind="gadget")]
        beat = pick_beat(
            posture="kneeling",
            items=items,
            arc="support",
            rng=random.Random(0),
        )
        self.assertIsNone(beat)

    def test_empty_posture_returns_none(self) -> None:
        items = [_FakeItem(slug="tea_pot", name="tea pot", kind="gadget")]
        beat = pick_beat(
            posture="",
            items=items,
            arc="support",
            rng=random.Random(0),
        )
        self.assertIsNone(beat)


# ── no-repeat ring ──────────────────────────────────────────────────


class PickBeatNoRepeatRingTests(unittest.TestCase):
    def test_recent_slug_is_skipped(self) -> None:
        # Two eligible items; the ring contains one of them. The
        # selector must pick the other one even though the RNG would
        # normally have a 50/50 split.
        items = [
            _FakeItem(slug="tea_pot", name="tea pot", kind="gadget"),
            _FakeItem(slug="blanket", name="plush blanket", kind="decor"),
        ]
        beat = pick_beat(
            posture="sitting",
            items=items,
            arc="support",
            recent_slugs=("tea_pot",),
            rng=random.Random(0),
        )
        assert beat is not None
        self.assertEqual(beat.item_slug, "blanket")

    def test_all_recent_returns_none(self) -> None:
        items = [
            _FakeItem(slug="tea_pot", name="tea pot", kind="gadget"),
        ]
        beat = pick_beat(
            posture="sitting",
            items=items,
            arc="support",
            recent_slugs=("tea_pot",),
            rng=random.Random(0),
        )
        self.assertIsNone(beat)


# ── quantity weighting ──────────────────────────────────────────────


class PickBeatQuantityWeightingTests(unittest.TestCase):
    def test_cookies_dominate_over_plush_under_seeded_runs(self) -> None:
        # 8 cookies vs 1 plush: the weighting should heavily favor
        # cookies. Run 1000 fixed-seed trials and assert the
        # cookie-fire ratio is above 70%. Quantity is clamped at
        # 6 internally so the bias is real but not absolute.
        items = [
            _FakeItem(
                slug="cookies", name="cookies", kind="food", quantity=8,
            ),
            _FakeItem(
                slug="plush", name="cat plush", kind="toy", quantity=1,
            ),
        ]
        rng = random.Random(7)
        cookie_count = 0
        plush_count = 0
        for _ in range(1000):
            beat = pick_beat(
                posture="sitting", items=items, arc="playful", rng=rng,
            )
            assert beat is not None
            if beat.item_slug == "cookies":
                cookie_count += 1
            elif beat.item_slug == "plush":
                plush_count += 1
        self.assertGreater(cookie_count, plush_count)
        # Clamped 6:1 → ~6/7 = 86%; allow slack for clamp + RNG variance.
        self.assertGreater(cookie_count / 1000.0, 0.70)


# ── render ──────────────────────────────────────────────────────────


class RenderInnerLifeBlockTests(unittest.TestCase):
    def test_none_beat_returns_empty_string(self) -> None:
        self.assertEqual(render_inner_life_block(None), "")

    def test_beat_renders_with_item_name_and_hint(self) -> None:
        beat = SensoryBeat(
            item_slug="tea_pot",
            item_name="tea pot",
            verb_class="picking_up",
            arc="support",
            posture="sitting",
        )
        out = render_inner_life_block(beat, user_display_name="Jacob")
        self.assertIn("tea pot", out)
        self.assertIn(_VERB_CLASS_HINT["picking_up"], out)
        self.assertIn("Jacob", out)
        # The persona is taught to use the cue as permission, not
        # a directive -- the render must include "otherwise let it
        # pass" or equivalent escape hatch.
        self.assertIn("otherwise let it pass", out)

    def test_unknown_verb_class_falls_back_to_generic_hint(self) -> None:
        beat = SensoryBeat(
            item_slug="x",
            item_name="x",
            verb_class="totally_made_up",
            arc="silly",
            posture="sitting",
        )
        out = render_inner_life_block(beat)
        self.assertIn("touch it briefly", out)


# ── cadence: cooldown + arc gates + ring ────────────────────────────


class SensoryAnchorCadenceCooldownTests(unittest.TestCase):
    def test_cooldown_is_armed_on_fire_and_decrements(self) -> None:
        cadence = SensoryAnchorCadence(max_recent=4)
        items = [_FakeItem(slug="tea_pot", name="tea pot", kind="gadget")]
        # Force a fire with probability_scale=2.0 (clamped to 1.0 so
        # the dice always pass) and ``support`` arc (4-turn cooldown).
        beat = cadence.tick(
            posture="sitting",
            items=items,
            arc="support",
            min_turn_gap=4,
            probability_scale=10.0,
            max_window=6,
            rng=random.Random(0),
        )
        assert beat is not None
        self.assertEqual(beat.item_slug, "tea_pot")
        self.assertEqual(cadence._cooldown_remaining, 4)

        # Next four ticks must return None regardless of dice.
        for expected_remaining in (3, 2, 1, 0):
            silent = cadence.tick(
                posture="sitting",
                items=items,
                arc="support",
                min_turn_gap=4,
                probability_scale=10.0,
                max_window=6,
                rng=random.Random(0),
            )
            self.assertIsNone(silent)
            self.assertEqual(
                cadence._cooldown_remaining, expected_remaining,
            )

    def test_no_repeat_ring_drains_then_refills(self) -> None:
        # Same single item, ring size 2. First fire stamps the slug;
        # second eligible tick should see it in the ring and skip.
        cadence = SensoryAnchorCadence(max_recent=2)
        items = [_FakeItem(slug="tea_pot", name="tea pot", kind="gadget")]
        first = cadence.tick(
            posture="sitting",
            items=items,
            arc="support",
            min_turn_gap=1,
            probability_scale=10.0,
            rng=random.Random(0),
        )
        self.assertIsNotNone(first)
        self.assertIn("tea_pot", cadence._recent_slugs)
        # Drain cooldown.
        cadence.tick(
            posture="sitting",
            items=items,
            arc="support",
            min_turn_gap=1,
            probability_scale=10.0,
            rng=random.Random(0),
        )
        # Now the dice pass again but the only item is in the ring.
        silent = cadence.tick(
            posture="sitting",
            items=items,
            arc="support",
            min_turn_gap=1,
            probability_scale=10.0,
            rng=random.Random(0),
        )
        self.assertIsNone(silent)


# ── cadence: arc-weighted probability distribution ──────────────────


class SensoryAnchorCadenceArcWeightTests(unittest.TestCase):
    """Fixed-seed sweep: ``planning`` fires much less than ``support``.

    Disable the no-repeat ring + cooldown by using ``min_turn_gap=1``,
    a single-item pool, and a per-tick clearing of the cadence's
    cooldown. We're measuring the *probability gate alone*.
    """

    def _hit_rate(self, arc: str, *, trials: int = 2000) -> float:
        items = [
            _FakeItem(
                slug="tea_pot", name="tea pot", kind="gadget", quantity=1,
            ),
        ]
        rng = random.Random(99)
        hits = 0
        for _ in range(trials):
            cadence = SensoryAnchorCadence(max_recent=1)
            beat = cadence.tick(
                posture="sitting",
                items=items,
                arc=arc,
                min_turn_gap=1,
                probability_scale=1.0,
                rng=rng,
            )
            if beat is not None:
                hits += 1
        return hits / trials

    def test_support_fires_more_than_planning(self) -> None:
        support_rate = self._hit_rate("support")
        planning_rate = self._hit_rate("planning")
        # support is 0.45, planning is 0.05 → support_rate should be
        # at least 4x planning_rate.
        self.assertGreater(support_rate, planning_rate * 4)
        # And support should land near its programmed rate (loose
        # bounds for RNG noise across 2000 trials).
        self.assertGreater(support_rate, 0.35)
        self.assertLess(support_rate, 0.55)
        # And planning should be rare but non-zero on average.
        self.assertLess(planning_rate, 0.10)


# ── cadence: introspection ──────────────────────────────────────────


class SensoryAnchorCadenceIntrospectionTests(unittest.TestCase):
    def test_to_debug_dict_after_fire(self) -> None:
        cadence = SensoryAnchorCadence(max_recent=4)
        items = [_FakeItem(slug="tea_pot", name="tea pot", kind="gadget")]
        cadence.tick(
            posture="sitting",
            items=items,
            arc="support",
            min_turn_gap=4,
            probability_scale=10.0,
            rng=random.Random(0),
        )
        snapshot = cadence.to_debug_dict()
        self.assertEqual(snapshot["last_fired_slug"], "tea_pot")
        self.assertEqual(snapshot["last_arc_seen"], "support")
        self.assertEqual(snapshot["fire_count"], 1)
        self.assertEqual(snapshot["tick_count"], 1)
        self.assertEqual(snapshot["cooldown_remaining"], 4)
        self.assertEqual(snapshot["recent_slugs"], ["tea_pot"])

    def test_reset_clears_state(self) -> None:
        cadence = SensoryAnchorCadence(max_recent=4)
        items = [_FakeItem(slug="tea_pot", name="tea pot", kind="gadget")]
        cadence.tick(
            posture="sitting",
            items=items,
            arc="support",
            min_turn_gap=4,
            probability_scale=10.0,
            rng=random.Random(0),
        )
        cadence.reset()
        snapshot = cadence.to_debug_dict()
        self.assertEqual(snapshot["cooldown_remaining"], 0)
        self.assertEqual(snapshot["recent_slugs"], [])
        self.assertEqual(snapshot["last_fired_slug"], None)
        self.assertEqual(snapshot["fire_count"], 0)


# ── arc weights config sanity ───────────────────────────────────────


class ArcWeightsTableTests(unittest.TestCase):
    def test_all_arcs_present(self) -> None:
        # Every value in VALID_ARCS must have an entry; defending
        # against the matrix being silently truncated.
        from app.core.conversation.conversation_arc import VALID_ARCS

        for arc in VALID_ARCS:
            self.assertIn(arc, _ARC_WEIGHTS)

    def test_support_higher_than_planning(self) -> None:
        support_prob, support_cooldown = _ARC_WEIGHTS["support"]
        planning_prob, planning_cooldown = _ARC_WEIGHTS["planning"]
        self.assertGreater(support_prob, planning_prob)
        # Planning has a longer cooldown so even rare hits stay rare.
        self.assertGreater(planning_cooldown, support_cooldown)


if __name__ == "__main__":
    unittest.main()
