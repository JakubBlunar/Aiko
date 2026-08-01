"""Tests for the SessionController curiosity-seed surfaces (K9).

We don't spin up a full ``SessionController`` -- the integration is
expensive and requires a real DB, embedder, ollama, etc. Instead the
tests bind the unbound render method onto a tiny fixture object carrying
the cue-pool mixin and just the attributes the method reads.

Resolution moved to the pool's ``either_party`` fulfilment at schema v29
and is covered in ``test_cue_pool_consumption``; what is left here is the
block itself plus the one thing the move had to preserve -- that a seed
retires when the conversation reaches it, whether or not it surfaced.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import STATE_PENDING, STATE_USED, CueStore
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.session_controller import SessionController


class _Fixture(CuePoolMixin):
    """A SessionController stripped to what the seed surfaces touch."""

    def __init__(
        self,
        store: CueStore,
        *,
        enabled: bool = True,
        suppressed: bool = False,
        resolve_threshold: float = 0.50,
    ) -> None:
        self._cue_store = store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []
        self._embedder = None
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(
                curiosity_seed_enabled=enabled,
                curiosity_seed_resolve_threshold=resolve_threshold,
            ),
        )
        self._suppressed = suppressed

    # K47: the render block early-returns when the question/share gate
    # is armed. Default to "not suppressed" so these tests exercise the
    # curiosity-seed path itself.
    def _question_balance_suppressed(self) -> bool:
        return self._suppressed


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _seed(self, subject: str, **kw) -> int:
        return self.store.add("curiosity_seed", subject, subject, **kw)

    def _state(self, cue_id: int) -> str:
        return next(
            r.state for r in self.store.list_for_user() if r.id == cue_id
        )

    def _render(self, fixture: _Fixture) -> str:
        return SessionController._render_curiosity_seeds_block(fixture)


# ── inner-life block ────────────────────────────────────────────────


class RenderBlockTests(_Base):
    def test_empty_when_disabled(self) -> None:
        self._seed("tea ritual")
        self.assertEqual(
            self._render(_Fixture(self.store, enabled=False)), "",
        )

    def test_empty_when_the_pool_is_dry(self) -> None:
        self.assertEqual(self._render(_Fixture(self.store)), "")

    def test_renders_two_seeds_freshest_first(self) -> None:
        for subject in ("first", "second", "third"):
            self._seed(subject)
        out = self._render(_Fixture(self.store))
        self.assertIn("Quiet curiosity", out)
        self.assertIn("third", out)
        self.assertIn("second", out)
        self.assertNotIn("first", out)

    def test_an_ignored_seed_yields_to_one_she_has_not_seen(self) -> None:
        """Fairness comes from surfaced_count, not from age."""
        stale = self._seed("stale")
        self.store.mark_surfaced(stale)
        self.store.release(stale, evidence="test")
        self._seed("fresh")
        out = self._render(_Fixture(self.store))
        self.assertEqual(
            out.splitlines()[1:], ["- fresh", "- stale"],
        )

    def test_spent_seeds_are_gone_from_the_shelf(self) -> None:
        self._seed("still active")
        used = self._seed("already mentioned")
        self.store.mark_used(used, evidence="test")
        out = self._render(_Fixture(self.store))
        self.assertIn("still active", out)
        self.assertNotIn("already mentioned", out)

    def test_rendering_marks_both_seeds_surfaced(self) -> None:
        """They were in the prompt, so both are judged post-turn."""
        first = self._seed("first")
        second = self._seed("second")
        fixture = _Fixture(self.store)
        self._render(fixture)
        self.assertEqual(self._state(first), "surfaced")
        self.assertEqual(self._state(second), "surfaced")
        self.assertEqual(len(fixture._surfaced_pool_cues), 2)

    def test_the_question_balance_gate_still_wins(self) -> None:
        self._seed("tea ritual")
        fixture = _Fixture(self.store, suppressed=True)
        self.assertEqual(self._render(fixture), "")
        self.assertEqual(self._state(1), STATE_PENDING)


# ── consumption, via the pool ───────────────────────────────────────


class AutoResolveTests(_Base):
    def test_a_turn_that_lands_on_the_subject_spends_it(self) -> None:
        cue_id = self._seed("tea ritual")
        fixture = _Fixture(self.store)
        self._render(fixture)
        fixture._settle_pool_cues(
            user_text="we drank some tea today",
            assistant_text="what's the ritual like?",
        )
        self.assertEqual(self._state(cue_id), STATE_USED)

    def test_an_unrelated_turn_leaves_it_pending(self) -> None:
        cue_id = self._seed("tea ritual")
        fixture = _Fixture(self.store)
        self._render(fixture)
        fixture._settle_pool_cues(
            user_text="work was brutal", assistant_text="oof, sorry",
        )
        self.assertEqual(self._state(cue_id), STATE_PENDING)

    def test_a_seed_is_spent_even_if_it_never_surfaced(self) -> None:
        """The behaviour K9 had, and the reason for ``either_party``.

        The old resolver scanned every active seed each turn regardless
        of what was in the prompt. Holding one open for a topic the two
        of them just discussed would be the opposite of curiosity.
        """
        cue_id = self._seed("tea ritual")
        fixture = _Fixture(self.store)
        fixture._settle_pool_cues(
            user_text="we drank some tea today",
            assistant_text="the ritual sounds lovely",
        )
        self.assertEqual(self._state(cue_id), STATE_USED)

    def test_an_unsurfaced_miss_costs_the_seed_nothing(self) -> None:
        """It was never shown to her, so there is nothing to hold against it."""
        cue_id = self._seed("tea ritual")
        fixture = _Fixture(self.store)
        for _ in range(4):
            fixture._settle_pool_cues(
                user_text="unrelated", assistant_text="also unrelated",
            )
        row = next(r for r in self.store.list_for_user() if r.id == cue_id)
        self.assertEqual(row.state, STATE_PENDING)
        self.assertEqual(row.surfaced_count, 0)

    def test_the_configured_threshold_is_still_honoured(self) -> None:
        """``curiosity_seed_resolve_threshold`` predates the policy registry."""
        vec = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        cue_id = self._seed("tea ritual", embedding=vec)
        # No shared words; only a cosine can decide, and 1.0 clears any
        # threshold at or below it.
        fixture = _Fixture(self.store, resolve_threshold=0.99)
        fixture._settle_pool_cues(
            user_text="hey", assistant_text="mm", turn_vec=vec,
        )
        self.assertEqual(self._state(cue_id), STATE_USED)

    def test_a_threshold_above_the_score_holds_the_seed(self) -> None:
        vec = np.asarray([1.0, 0.0, 0.0], dtype=np.float32)
        near = np.asarray([0.8, 0.6, 0.0], dtype=np.float32)
        cue_id = self._seed("tea ritual", embedding=vec)
        fixture = _Fixture(self.store, resolve_threshold=0.95)
        fixture._settle_pool_cues(
            user_text="hey", assistant_text="mm", turn_vec=near,
        )
        self.assertEqual(self._state(cue_id), STATE_PENDING)


if __name__ == "__main__":
    unittest.main()
