"""The narrow second dedupe gate: the same fact restated minutes later.

The global ``dedupe_threshold`` compares across every kind and every age,
so it has to stay high (0.92) -- a false merge there silently destroys a
distinct memory. That left the extractor free to write a fresh row every
time it reworded the same fact, and the live store showed the cost: one
plan became six rows ("Jacob's premium chocolate cookies will arrive in a
few days" / "Jacob expects his premium chocolate cookie order for Aiko to
arrive in a few days", written ten minutes apart at cosine 0.85), and
every consumer keyed on memory id then treated them as separate subjects.

``_is_restatement`` closes that with a lower floor plus three narrowing
conditions -- same kind, same temporal type, and written inside a short
window. The window is what makes the lower floor safe: measured over the
live store, same-kind pairs a few hours apart are overwhelmingly
restatements, while the similarly-scoring pairs a day or more apart are
distinct facts sharing a frame ("allergies make breathing hard outdoors"
vs "allergies improve after rain" sit at 0.82).

Embeddings here are constructed at exact angles rather than sampled, so
each case pins one side of a threshold instead of hoping a fake embedder
lands there.
"""
from __future__ import annotations

import math
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import numpy as np

from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.memory_store import MemoryStore


_DIM = 8


def _at_cosine(target: float) -> np.ndarray:
    """A unit vector whose cosine with ``_base()`` is ``target``."""
    angle = math.acos(max(-1.0, min(1.0, target)))
    vec = np.zeros(_DIM, dtype=np.float32)
    vec[0] = math.cos(angle)
    vec[1] = math.sin(angle)
    return vec


def _base() -> np.ndarray:
    vec = np.zeros(_DIM, dtype=np.float32)
    vec[0] = 1.0
    return vec


class _Fixture:
    def __init__(self, **kwargs) -> None:
        self._kwargs = kwargs

    def __enter__(self) -> MemoryStore:
        self._dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        path = Path(self._dir.name) / "mem.db"
        ChatDatabase(path)
        self.store = MemoryStore(path, **self._kwargs)
        return self.store

    def __exit__(self, *exc) -> None:
        try:
            self._dir.cleanup()
        except PermissionError:
            pass


class _Base(unittest.TestCase):
    def _first(self, store, **kwargs):
        """The row already in the store, at the canonical angle."""
        defaults = {"kind": "event", "temporal_type": "future_plan"}
        defaults.update(kwargs)
        return store.add(
            "Jacob's cookies will arrive in a few days.",
            defaults.pop("kind"),
            _base(),
            **defaults,
        )

    def _restate(self, store, cosine=0.88, **kwargs):
        defaults = {"kind": "event", "temporal_type": "future_plan"}
        defaults.update(kwargs)
        return store.add(
            "Jacob expects his cookie order to arrive in a few days.",
            defaults.pop("kind"),
            _at_cosine(cosine),
            **defaults,
        )


class RestatementTests(_Base):
    def test_a_reworded_fact_minutes_later_is_the_same_fact(self) -> None:
        with _Fixture() as store:
            first = self._first(store)
            self.assertIsNotNone(first)
            self.assertIsNone(self._restate(store))
            self.assertEqual(store.count(), 1)

    def test_the_survivor_keeps_the_stronger_salience(self) -> None:
        """A merge must not quietly demote what it absorbed."""
        with _Fixture() as store:
            first = self._first(store, salience=0.4)
            self._restate(store, salience=0.9)
            self.assertGreaterEqual(store.get(first.id).salience, 0.9)

    def test_below_the_restate_floor_it_is_a_new_row(self) -> None:
        """0.82 is where genuinely distinct facts start showing up."""
        with _Fixture() as store:
            self._first(store)
            self.assertIsNotNone(self._restate(store, cosine=0.82))
            self.assertEqual(store.count(), 2)

    def test_the_global_gate_still_catches_a_near_copy(self) -> None:
        with _Fixture() as store:
            self._first(store)
            self.assertIsNone(self._restate(store, cosine=0.95))
            self.assertEqual(store.count(), 1)


class NarrowingTests(_Base):
    """Each condition has to be load-bearing on its own."""

    def test_a_different_kind_is_left_alone(self) -> None:
        with _Fixture() as store:
            self._first(store, kind="event")
            self.assertIsNotNone(self._restate(store, kind="fact"))
            self.assertEqual(store.count(), 2)

    def test_a_plan_and_the_event_it_became_stay_separate(self) -> None:
        """Near-identical wording, genuinely different rows."""
        with _Fixture() as store:
            self._first(store, temporal_type="future_plan")
            self.assertIsNotNone(
                self._restate(store, temporal_type="past_event")
            )
            self.assertEqual(store.count(), 2)

    def test_a_worker_minted_kind_is_left_alone(self) -> None:
        """``knowledge_gap`` and friends run their own inventory.

        One row per distinct subject, a cap the producer enforces itself,
        and a caller that reads the returned row to know the write
        landed -- so a merge here both loses an item and reads as a
        failed write.
        """
        with _Fixture() as store:
            self._first(store, kind="knowledge_gap", temporal_type="durable")
            self.assertIsNotNone(
                self._restate(
                    store, kind="knowledge_gap", temporal_type="durable",
                )
            )
            self.assertEqual(store.count(), 2)

    def test_the_same_wording_a_day_later_is_a_new_row(self) -> None:
        with _Fixture() as store:
            self._first(store)
            later = timephrase.utcnow() + timedelta(hours=30)
            with patch.object(timephrase, "utcnow", return_value=later):
                self.assertIsNotNone(self._restate(store))
            self.assertEqual(store.count(), 2)

    def test_just_inside_the_window_still_merges(self) -> None:
        with _Fixture() as store:
            self._first(store)
            later = timephrase.utcnow() + timedelta(hours=5)
            with patch.object(timephrase, "utcnow", return_value=later):
                self.assertIsNone(self._restate(store))
            self.assertEqual(store.count(), 1)


class ContradictionGuardTests(_Base):
    """A correction is not a restatement, and scores like one.

    F5's conflict detector reads ``[0.80, 0.92)`` precisely because a
    negation flip barely moves an embedding, so a contradiction lands
    above this gate's floor. Merging one keeps the older row and discards
    the correction -- so the same pure-Python heuristic F5 uses gets the
    last word.
    """

    def _contradict(self, store, text, cosine=0.88, **kwargs):
        defaults = {"kind": "fact", "temporal_type": "durable"}
        defaults.update(kwargs)
        return store.add(
            text, defaults.pop("kind"), _at_cosine(cosine), **defaults,
        )

    def _stated(self, store, text, **kwargs):
        defaults = {"kind": "fact", "temporal_type": "durable"}
        defaults.update(kwargs)
        return store.add(text, defaults.pop("kind"), _base(), **defaults)

    def test_a_negation_flip_stays_two_rows(self) -> None:
        with _Fixture() as store:
            self._stated(store, "Jacob loves spicy food.")
            self.assertIsNotNone(
                self._contradict(store, "Jacob does not love spicy food.")
            )
            self.assertEqual(store.count(), 2)

    def test_an_antonym_stays_two_rows(self) -> None:
        with _Fixture() as store:
            self._stated(store, "Jacob loves horror films.")
            self.assertIsNotNone(
                self._contradict(store, "Jacob hates horror films.")
            )
            self.assertEqual(store.count(), 2)

    def test_a_changed_number_stays_two_rows(self) -> None:
        """Borderline is enough to keep them apart; F5 adjudicates."""
        with _Fixture() as store:
            self._stated(store, "Jacob has 2 cats at home.")
            self.assertIsNotNone(
                self._contradict(store, "Jacob has 4 cats at home.")
            )
            self.assertEqual(store.count(), 2)

    def test_the_global_gate_is_not_softened_by_the_guard(self) -> None:
        """Above ``dedupe_threshold`` the old behaviour is untouched.

        The guard narrows the *new* gate only. Changing what the 0.92
        threshold does would be a silent behaviour change to every
        existing caller.
        """
        with _Fixture() as store:
            self._stated(store, "Jacob loves spicy food.")
            self.assertIsNone(
                self._contradict(
                    store, "Jacob does not love spicy food.", cosine=0.95,
                )
            )
            self.assertEqual(store.count(), 1)


class SwitchTests(_Base):
    def test_a_zero_window_disables_the_gate(self) -> None:
        with _Fixture(restate_window_hours=0.0) as store:
            self._first(store)
            self.assertIsNotNone(self._restate(store))
            self.assertEqual(store.count(), 2)

    def test_the_floor_is_configurable(self) -> None:
        with _Fixture(restate_threshold=0.75) as store:
            self._first(store)
            self.assertIsNone(self._restate(store, cosine=0.80))
            self.assertEqual(store.count(), 1)

    def test_pinned_writes_still_bypass_everything(self) -> None:
        """A curated row is never merged into a fuzzy neighbour."""
        with _Fixture() as store:
            self._first(store)
            self.assertIsNotNone(self._restate(store, pinned=True))
            self.assertEqual(store.count(), 2)

    def test_skip_dedupe_still_bypasses_everything(self) -> None:
        with _Fixture() as store:
            self._first(store)
            self.assertIsNotNone(self._restate(store, skip_dedupe=True))
            self.assertEqual(store.count(), 2)


if __name__ == "__main__":
    unittest.main()
