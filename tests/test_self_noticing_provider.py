"""Controller-level tests for the K30 self-noticing provider.

Exercises ``InnerLifeProvidersMixin._render_self_noticing_block`` via
a minimal stub host that simulates the controller surface the
provider reads from (``_settings`` / ``_chat_db`` / the K30 rings and
cooldown / force flags). Avoids spinning up the full
:class:`SessionController` which would import half the world.

The three pure detectors themselves are covered exhaustively in
``tests/test_self_pattern_detector.py``; this module focuses on the
provider plumbing -- master switch + per-sub switches, cooldown
arming + decrement, force-flag bypass, multi-cue fan-out, and the
one-shot consumption rule for the repeated-thought flag.
"""
from __future__ import annotations

import unittest
from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


# ── stubs ──────────────────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class _Row:
    role: str
    content: str


class _FakeChatDb:
    def __init__(self, rows: list[_Row]) -> None:
        self._rows = rows

    def get_messages(
        self, session_id: str, *, limit: int | None = None,
    ) -> list[_Row]:  # noqa: ARG002
        if limit is None:
            return list(self._rows)
        return list(self._rows[-limit:])


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        self_noticing_enabled=True,
        self_noticing_agreement_streak_enabled=True,
        self_noticing_flat_affect_enabled=True,
        self_noticing_repeated_thought_enabled=True,
        self_noticing_window=6,
        self_noticing_warmup=4,
        self_noticing_agreement_threshold=0.80,
        self_noticing_max_pushback=0,
        self_noticing_flat_valence_range=0.10,
        self_noticing_flat_arousal_range=0.10,
        self_noticing_repeated_cosine_threshold=0.85,
        self_noticing_cooldown_turns=5,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _agreement_history(n: int = 6) -> list[_Row]:
    """Build N assistant rows that all read as agreement."""
    yeses = [
        "yeah totally",
        "for sure",
        "exactly, that makes sense",
        "right? absolutely",
        "totally agreed",
        "yep, of course",
        "agreed, makes sense",
    ]
    rows: list[_Row] = []
    for i in range(n):
        rows.append(_Row(role="assistant", content=yeses[i % len(yeses)]))
    return rows


def _flat_samples(n: int = 6) -> list[tuple[float, float, str | None]]:
    """Build N (val, aro, reaction) triples that all sit in low-band
    and inside the flat thresholds. Returns a list ready to be fed
    to ``deque(maxlen=...)``."""
    return [(0.0, 0.4, "neutral")] * n


class _Host(InnerLifeProvidersMixin):
    """Minimal mixin host with the attributes the K30 provider reads."""

    def __init__(
        self,
        *,
        history: list[_Row] | None = None,
        affect_samples: list[tuple[float, float, str | None]] | None = None,
        vec_ring_size: int = 3,
        agent_settings: SimpleNamespace | None = None,
        repeated_thought_fired: bool = False,
        agreement_cooldown: int = 0,
        flat_cooldown: int = 0,
        force_agreement: bool = False,
        force_flat: bool = False,
        force_repeated: bool = False,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._chat_db = _FakeChatDb(history or [])
        self.session_key = "stub-session"
        self._self_noticing_affect_samples: deque[
            tuple[float, float, str | None]
        ] = deque(maxlen=12)
        if affect_samples:
            for s in affect_samples:
                self._self_noticing_affect_samples.append(s)
        self._self_noticing_aiko_vecs: deque[Any] = deque(maxlen=vec_ring_size)
        self._self_noticing_force_agreement = force_agreement
        self._self_noticing_force_flat_affect = force_flat
        self._self_noticing_force_repeated_thought = force_repeated
        self._self_noticing_agreement_cooldown = agreement_cooldown
        self._self_noticing_flat_affect_cooldown = flat_cooldown
        self._repeated_thought_fired_last_turn = repeated_thought_fired
        self._repeated_thought_last_cosine = 0.0
        self._repeated_thought_last_matched_index = -1
        self._last_self_noticing_agreement: Any = None
        self._last_self_noticing_flat_affect: Any = None


# ── master switch ──────────────────────────────────────────────────


class MasterSwitchTests(unittest.TestCase):
    def test_master_off_silences_everything(self) -> None:
        host = _Host(
            agent_settings=_make_agent_settings(self_noticing_enabled=False),
            history=_agreement_history(6),
            affect_samples=_flat_samples(6),
            repeated_thought_fired=True,
        )
        self.assertEqual(host._render_self_noticing_block(), "")
        # The force flags shouldn't bypass the master gate either.
        host._self_noticing_force_agreement = True
        host._self_noticing_force_flat_affect = True
        host._self_noticing_force_repeated_thought = True
        self.assertEqual(host._render_self_noticing_block(), "")


# ── sub-switches ───────────────────────────────────────────────────


class SubSwitchTests(unittest.TestCase):
    def test_agreement_sub_off_silences_agreement_only(self) -> None:
        host = _Host(
            agent_settings=_make_agent_settings(
                self_noticing_agreement_streak_enabled=False,
            ),
            history=_agreement_history(6),
            affect_samples=_flat_samples(6),
            repeated_thought_fired=True,
        )
        block = host._render_self_noticing_block()
        self.assertNotIn("agreeing with everything", block)
        self.assertIn("even-keel", block)
        self.assertIn("close to something you already said", block)

    def test_flat_affect_sub_off_silences_flat_only(self) -> None:
        host = _Host(
            agent_settings=_make_agent_settings(
                self_noticing_flat_affect_enabled=False,
            ),
            history=_agreement_history(6),
            affect_samples=_flat_samples(6),
            repeated_thought_fired=True,
        )
        block = host._render_self_noticing_block()
        self.assertIn("agreeing with everything", block)
        self.assertNotIn("even-keel", block)
        self.assertIn("close to something you already said", block)

    def test_repeated_sub_off_silences_repeated_only(self) -> None:
        host = _Host(
            agent_settings=_make_agent_settings(
                self_noticing_repeated_thought_enabled=False,
            ),
            history=_agreement_history(6),
            affect_samples=_flat_samples(6),
            repeated_thought_fired=True,
        )
        block = host._render_self_noticing_block()
        self.assertIn("agreeing with everything", block)
        self.assertIn("even-keel", block)
        self.assertNotIn("close to something you already said", block)


# ── individual sub-detector fires ─────────────────────────────────


class AgreementFireTests(unittest.TestCase):
    def test_agreement_streak_fires(self) -> None:
        host = _Host(history=_agreement_history(6))
        block = host._render_self_noticing_block()
        self.assertIn("agreeing with everything", block)
        # Cooldown was armed to the configured 5.
        self.assertEqual(host._self_noticing_agreement_cooldown, 5)

    def test_agreement_with_one_pushback_no_fire(self) -> None:
        history = _agreement_history(5) + [
            _Row(role="assistant", content="hmm, not so sure about that one")
        ]
        host = _Host(history=history)
        block = host._render_self_noticing_block()
        self.assertNotIn("agreeing with everything", block)

    def test_agreement_below_warmup_no_fire(self) -> None:
        # Three rows < min_samples=4.
        host = _Host(history=_agreement_history(3))
        self.assertEqual(host._render_self_noticing_block(), "")

    def test_agreement_skipped_when_in_cooldown(self) -> None:
        host = _Host(history=_agreement_history(6), agreement_cooldown=3)
        self.assertEqual(host._render_self_noticing_block(), "")
        # Cooldown decremented by one regardless of fire state.
        self.assertEqual(host._self_noticing_agreement_cooldown, 2)


class FlatAffectFireTests(unittest.TestCase):
    def test_flat_affect_fires_on_flat_ring(self) -> None:
        host = _Host(affect_samples=_flat_samples(6))
        block = host._render_self_noticing_block()
        self.assertIn("even-keel", block)
        self.assertEqual(host._self_noticing_flat_affect_cooldown, 5)

    def test_flat_affect_silent_with_notable_reaction(self) -> None:
        samples = _flat_samples(5) + [(0.0, 0.4, "playful")]
        host = _Host(affect_samples=samples)
        block = host._render_self_noticing_block()
        self.assertNotIn("even-keel", block)

    def test_flat_affect_silent_with_wide_range(self) -> None:
        # Valence jumps by 0.5 -> above the 0.10 threshold.
        samples: list[tuple[float, float, str | None]] = [
            (0.0, 0.4, "neutral"),
            (0.5, 0.4, "neutral"),
            (0.0, 0.4, "neutral"),
            (0.0, 0.4, "calm"),
            (0.0, 0.4, "neutral"),
            (0.0, 0.4, "friendly"),
        ]
        host = _Host(affect_samples=samples)
        block = host._render_self_noticing_block()
        self.assertNotIn("even-keel", block)

    def test_flat_affect_skipped_when_in_cooldown(self) -> None:
        host = _Host(affect_samples=_flat_samples(6), flat_cooldown=2)
        self.assertEqual(host._render_self_noticing_block(), "")
        # Cooldown decremented even when sub-detector skipped.
        self.assertEqual(host._self_noticing_flat_affect_cooldown, 1)


class RepeatedThoughtFireTests(unittest.TestCase):
    def test_fires_on_carry_forward_flag(self) -> None:
        host = _Host(repeated_thought_fired=True)
        block = host._render_self_noticing_block()
        self.assertIn("close to something you already said", block)
        # Flag was consumed.
        self.assertFalse(host._repeated_thought_fired_last_turn)

    def test_no_fire_without_flag(self) -> None:
        host = _Host(repeated_thought_fired=False)
        self.assertEqual(host._render_self_noticing_block(), "")

    def test_one_shot_consumption(self) -> None:
        # First call fires, second call does not (flag was consumed).
        host = _Host(repeated_thought_fired=True)
        first = host._render_self_noticing_block()
        self.assertIn("close to something you already said", first)
        second = host._render_self_noticing_block()
        self.assertEqual(second, "")


# ── force flags ────────────────────────────────────────────────────


class ForceFlagTests(unittest.TestCase):
    def test_force_agreement_bypasses_cooldown(self) -> None:
        # Cooldown is non-zero, but the force flag should still fire.
        host = _Host(
            history=_agreement_history(6),
            agreement_cooldown=4,
            force_agreement=True,
        )
        block = host._render_self_noticing_block()
        self.assertIn("agreeing with everything", block)
        # Force flag was consumed (one-shot).
        self.assertFalse(host._self_noticing_force_agreement)

    def test_force_agreement_with_empty_history_no_fire(self) -> None:
        # The force flag is consumed when there's no eligible history
        # (no assistant rows means the detector never gets a chance
        # to fire, force or otherwise).
        host = _Host(history=[], force_agreement=True)
        block = host._render_self_noticing_block()
        self.assertNotIn("agreeing with everything", block)

    def test_force_flat_affect_bypasses_cooldown(self) -> None:
        host = _Host(
            affect_samples=_flat_samples(6),
            flat_cooldown=4,
            force_flat=True,
        )
        block = host._render_self_noticing_block()
        self.assertIn("even-keel", block)
        self.assertFalse(host._self_noticing_force_flat_affect)

    def test_force_repeated_thought(self) -> None:
        # Even without the carry-forward flag, force should fire.
        host = _Host(
            repeated_thought_fired=False,
            force_repeated=True,
        )
        block = host._render_self_noticing_block()
        self.assertIn("close to something you already said", block)
        self.assertFalse(host._self_noticing_force_repeated_thought)


# ── fan-out (multiple cues in one block) ──────────────────────────


class FanOutTests(unittest.TestCase):
    def test_all_three_fire_in_one_block(self) -> None:
        host = _Host(
            history=_agreement_history(6),
            affect_samples=_flat_samples(6),
            repeated_thought_fired=True,
        )
        block = host._render_self_noticing_block()
        self.assertIn("agreeing with everything", block)
        self.assertIn("even-keel", block)
        self.assertIn("close to something you already said", block)
        # Three Heads-up lines.
        self.assertEqual(block.count("Heads-up:"), 3)

    def test_two_of_three_fire(self) -> None:
        # Agreement + flat-affect, but no repeated-thought flag.
        host = _Host(
            history=_agreement_history(6),
            affect_samples=_flat_samples(6),
            repeated_thought_fired=False,
        )
        block = host._render_self_noticing_block()
        self.assertIn("agreeing with everything", block)
        self.assertIn("even-keel", block)
        self.assertNotIn("close to something you already said", block)
        self.assertEqual(block.count("Heads-up:"), 2)

    def test_silent_when_nothing_fires(self) -> None:
        host = _Host(
            history=[
                _Row(role="assistant", content="the weather is unusual"),
                _Row(role="assistant", content="that's interesting"),
                _Row(role="assistant", content="i wonder why"),
                _Row(role="assistant", content="curious"),
            ],
            affect_samples=[
                (0.0, 0.4, "playful"),
                (0.2, 0.5, "warm"),
                (0.1, 0.6, "thoughtful"),
            ],
            repeated_thought_fired=False,
        )
        self.assertEqual(host._render_self_noticing_block(), "")


# ── cooldown counters ──────────────────────────────────────────────


class CooldownDecrementTests(unittest.TestCase):
    def test_cooldown_decrements_each_call(self) -> None:
        # Even on a silent turn, cooldown ticks down. This is important
        # because otherwise a long stretch without a streak would leave
        # the cooldown stuck at the last armed value forever.
        host = _Host(agreement_cooldown=3, flat_cooldown=2)
        host._render_self_noticing_block()
        self.assertEqual(host._self_noticing_agreement_cooldown, 2)
        self.assertEqual(host._self_noticing_flat_affect_cooldown, 1)
        host._render_self_noticing_block()
        self.assertEqual(host._self_noticing_agreement_cooldown, 1)
        self.assertEqual(host._self_noticing_flat_affect_cooldown, 0)


if __name__ == "__main__":
    unittest.main()
