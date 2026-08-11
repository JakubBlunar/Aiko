"""L31 evidence admission: the two bars a cited source has to clear.

The gate is pure, so these are about its judgement rather than its wiring:
the cosine floor that keeps a concept from absorbing evidence for something
else, the source ceiling that keeps a vague label from absorbing everything,
and -- the part with teeth -- what counts as "this concept was reinforced"
when nothing got in. Worker wiring lives in
``tests/test_concept_synthesis_worker.py``.
"""
from __future__ import annotations

import json
import unittest

import numpy as np

from app.core.concepts.concept_evidence_admission import (
    ADMISSION_COS,
    FIT_SAMPLE_KEY,
    MAX_SOURCES,
    REFUSED_FULL,
    REFUSED_OFFTOPIC,
    admit,
    load_fit_sample,
    save_fit_sample,
)

# Two orthogonal directions, so "on topic" and "off topic" are unambiguous.
_ON = np.array([1.0, 0.0], dtype=np.float32)
_OFF = np.array([0.0, 1.0], dtype=np.float32)


def _at(cosine: float) -> "np.ndarray":
    """A vector whose cosine against ``_ON`` is exactly ``cosine``."""
    return np.array(
        [cosine, float(np.sqrt(max(0.0, 1.0 - cosine * cosine)))],
        dtype=np.float32,
    )


class FloorTests(unittest.TestCase):
    def _admit(self, cosines, **kw):
        nodes = [("memory", str(i)) for i in range(len(cosines))]
        return admit(
            nodes,
            label_vector=_ON,
            vectors={
                n: _at(c) for n, c in zip(nodes, cosines, strict=True)
            },
            existing_sources=set(),
            **kw,
        )

    def test_evidence_about_the_concept_gets_in(self) -> None:
        verdict = self._admit([0.6, 0.75])
        self.assertEqual(len(verdict.kept), 2)
        self.assertEqual(verdict.refused, [])
        self.assertEqual(verdict.admitted, 2)

    def test_evidence_about_something_else_is_refused(self) -> None:
        # The shape that motivated the bar: "Jacob enjoyed Chainsaw Man's
        # opening song" measured 0.243 against an intimacy aspiration.
        verdict = self._admit([0.243])
        self.assertEqual(verdict.kept, [])
        self.assertEqual(len(verdict.offtopic), 1)
        self.assertEqual(verdict.offtopic[0].reason, REFUSED_OFFTOPIC)
        self.assertAlmostEqual(verdict.offtopic[0].cosine, 0.243, places=3)

    def test_the_bar_is_inclusive(self) -> None:
        # Exactly at the floor is admissible; the refusal is for what falls
        # *below* what was measured as the bottom of the genuine range.
        # Tested at 1.0 because that is the one cosine float32 reproduces
        # exactly, so this pins the comparison rather than the arithmetic.
        node = ("memory", "1")
        verdict = admit(
            [node],
            label_vector=_ON,
            vectors={node: _ON},
            existing_sources=set(),
            floor=1.0,
        )
        self.assertEqual(len(verdict.kept), 1)

    def test_just_under_the_bar_is_refused(self) -> None:
        self.assertEqual(
            len(self._admit([ADMISSION_COS - 0.01]).offtopic), 1
        )

    def test_comfortably_over_the_bar_is_admitted(self) -> None:
        self.assertEqual(len(self._admit([ADMISSION_COS + 0.01]).kept), 1)

    def test_a_mixed_citation_keeps_only_what_belongs(self) -> None:
        verdict = self._admit([0.65, 0.2, 0.5])
        self.assertEqual(verdict.admitted, 2)
        self.assertEqual(len(verdict.offtopic), 1)

    def test_every_measured_cosine_is_reported(self) -> None:
        verdict = self._admit([0.65, 0.2])
        self.assertEqual(len(verdict.cosines), 2)

    def test_a_zero_floor_turns_the_bar_off(self) -> None:
        verdict = self._admit([0.01], floor=0.0)
        self.assertEqual(len(verdict.kept), 1)
        # Nothing was judged, so nothing is reported to the tuner either.
        self.assertEqual(verdict.cosines, [])

    def test_an_unembedded_concept_cannot_judge_and_does_not_try(self) -> None:
        nodes = [("memory", "1")]
        verdict = admit(
            nodes,
            label_vector=np.zeros(0, dtype=np.float32),
            vectors={nodes[0]: _OFF},
            existing_sources=set(),
        )
        self.assertEqual(len(verdict.kept), 1)


class FailOpenTests(unittest.TestCase):
    """A missing vector must never cost a concept its evidence."""

    def _one(self, vectors):
        return admit(
            [("memory", "1")],
            label_vector=_ON,
            vectors=vectors,
            existing_sources=set(),
        )

    def test_a_source_with_no_vector_at_all_is_admitted(self) -> None:
        self.assertEqual(len(self._one({}).kept), 1)

    def test_a_none_vector_is_admitted(self) -> None:
        self.assertEqual(len(self._one({("memory", "1"): None}).kept), 1)

    def test_a_zero_vector_is_admitted(self) -> None:
        verdict = self._one({("memory", "1"): np.zeros(2, dtype=np.float32)})
        self.assertEqual(len(verdict.kept), 1)

    def test_a_wrong_width_vector_is_admitted(self) -> None:
        # Embedding-model swap: dimensions stop matching. Refusing here
        # would starve every concept until the corpus was re-embedded.
        verdict = self._one(
            {("memory", "1"): np.ones(384, dtype=np.float32)}
        )
        self.assertEqual(len(verdict.kept), 1)
        self.assertEqual(verdict.cosines, [])


class CeilingTests(unittest.TestCase):
    def _admit(self, new: int, *, held: int, ceiling: int = MAX_SOURCES):
        existing = {("memory", f"h{i}") for i in range(held)}
        nodes = [("memory", f"n{i}") for i in range(new)]
        return admit(
            nodes,
            label_vector=_ON,
            vectors={n: _ON for n in nodes},
            existing_sources=existing,
            ceiling=ceiling,
        )

    def test_a_concept_under_the_ceiling_keeps_growing(self) -> None:
        verdict = self._admit(2, held=5, ceiling=10)
        self.assertEqual(verdict.admitted, 2)
        self.assertEqual(verdict.full, [])

    def test_a_full_concept_takes_nothing_new(self) -> None:
        verdict = self._admit(3, held=10, ceiling=10)
        self.assertEqual(verdict.kept, [])
        self.assertEqual(len(verdict.full), 3)
        self.assertEqual(verdict.full[0].reason, REFUSED_FULL)

    def test_the_ceiling_is_filled_exactly_not_overshot(self) -> None:
        # Two slots left, four offered: the first two get in.
        verdict = self._admit(4, held=8, ceiling=10)
        self.assertEqual(verdict.admitted, 2)
        self.assertEqual(len(verdict.full), 2)

    def test_a_row_already_over_the_ceiling_simply_stops(self) -> None:
        # The forward-only rule: the 145-source ritual keeps its history.
        verdict = self._admit(2, held=145, ceiling=24)
        self.assertEqual(verdict.kept, [])
        self.assertEqual(len(verdict.full), 2)

    def test_a_zero_ceiling_turns_the_cap_off(self) -> None:
        verdict = self._admit(5, held=999, ceiling=0)
        self.assertEqual(verdict.admitted, 5)

    def test_off_topic_is_judged_before_the_ceiling(self) -> None:
        # Which refusal a source gets decides whether the concept counts as
        # reinforced, so the order matters rather than being cosmetic.
        nodes = [("memory", "1")]
        verdict = admit(
            nodes,
            label_vector=_ON,
            vectors={nodes[0]: _OFF},
            existing_sources={("memory", f"h{i}") for i in range(30)},
            ceiling=24,
        )
        self.assertEqual(len(verdict.offtopic), 1)
        self.assertEqual(verdict.full, [])
        self.assertFalse(verdict.reinforced)


class AlreadyHeldTests(unittest.TestCase):
    def test_re_citing_a_held_source_always_passes(self) -> None:
        # The edge write upserts, so this cannot grow the count -- and a
        # full concept must not lose the ability to restate its own
        # evidence.
        node = ("memory", "1")
        verdict = admit(
            [node],
            label_vector=_ON,
            vectors={node: _OFF},
            existing_sources={node, *{("memory", f"h{i}") for i in range(30)}},
            ceiling=24,
        )
        self.assertEqual(verdict.kept, [node])
        self.assertEqual(verdict.refused, [])
        self.assertEqual(verdict.admitted, 0)
        self.assertEqual(verdict.cosines, [])

    def test_a_source_cited_twice_counts_once(self) -> None:
        node = ("memory", "1")
        verdict = admit(
            [node, node],
            label_vector=_ON,
            vectors={node: _ON},
            existing_sources=set(),
            ceiling=1,
        )
        self.assertEqual(verdict.admitted, 1)
        self.assertEqual(verdict.full, [])


class OrderTests(unittest.TestCase):
    def test_citation_order_survives_the_filter(self) -> None:
        # ``sequence`` concepts (L8 narrative, L14 aspiration) stamp each
        # edge's ordinal by position, so a filtered list that reordered
        # would scramble the arc.
        nodes = [("memory", str(i)) for i in range(5)]
        cosines = [0.9, 0.1, 0.8, 0.1, 0.7]
        verdict = admit(
            nodes,
            label_vector=_ON,
            vectors={
                n: _at(c) for n, c in zip(nodes, cosines, strict=True)
            },
            existing_sources=set(),
        )
        self.assertEqual(
            verdict.kept,
            [("memory", "0"), ("memory", "2"), ("memory", "4")],
        )


class ReinforcedTests(unittest.TestCase):
    """Whether ``last_reinforced_at`` may move.

    The load-bearing case is the ceiling: a capped concept whose timestamp
    froze would drift active -> dormant -> retired via the L46 dormancy TTL
    while the evidence for it kept arriving, so the gate would delete the
    graph's best-supported beliefs through a side door.
    """

    def _verdict(self, *, cosine: float, held: int, ceiling: int):
        node = ("memory", "new")
        return admit(
            [node],
            label_vector=_ON,
            vectors={node: _at(cosine)},
            existing_sources={("memory", f"h{i}") for i in range(held)},
            ceiling=ceiling,
        )

    def test_something_admitted_is_a_reinforcement(self) -> None:
        self.assertTrue(self._verdict(cosine=0.8, held=2, ceiling=24).reinforced)

    def test_a_capped_concept_was_still_observed(self) -> None:
        verdict = self._verdict(cosine=0.8, held=24, ceiling=24)
        self.assertEqual(verdict.kept, [])
        self.assertTrue(verdict.reinforced)

    def test_off_topic_only_is_not_a_reinforcement(self) -> None:
        self.assertFalse(self._verdict(cosine=0.1, held=2, ceiling=24).reinforced)

    def test_nothing_cited_is_not_a_reinforcement(self) -> None:
        verdict = admit(
            [],
            label_vector=_ON,
            vectors={},
            existing_sources=set(),
        )
        self.assertFalse(verdict.reinforced)

    def test_re_citing_held_evidence_still_counts(self) -> None:
        node = ("memory", "1")
        verdict = admit(
            [node],
            label_vector=_ON,
            vectors={node: _ON},
            existing_sources={node},
        )
        self.assertTrue(verdict.reinforced)


class FitSampleTests(unittest.TestCase):
    """The rolling sample the L45 tuner reads as an observed population."""

    def setUp(self) -> None:
        self.kv: dict[str, str] = {}

    def _get(self, key: str) -> "str | None":
        return self.kv.get(key)

    def _set(self, key: str, value: str) -> None:
        self.kv[key] = value

    def test_a_round_trip_preserves_the_values(self) -> None:
        save_fit_sample(self._set, [0.4, 0.55])
        self.assertEqual(load_fit_sample(self._get), [0.4, 0.55])

    def test_an_empty_store_reports_no_samples(self) -> None:
        self.assertEqual(load_fit_sample(self._get), [])

    def test_the_sample_is_bounded_oldest_first(self) -> None:
        save_fit_sample(self._set, [i / 1000.0 for i in range(50)], cap=10)
        kept = load_fit_sample(self._get)
        self.assertEqual(len(kept), 10)
        self.assertAlmostEqual(kept[0], 0.04)
        self.assertAlmostEqual(kept[-1], 0.049)

    def test_a_malformed_row_reads_as_no_data_rather_than_raising(self) -> None:
        self.kv[FIT_SAMPLE_KEY] = "{not json"
        self.assertEqual(load_fit_sample(self._get), [])

    def test_a_non_list_row_reads_as_no_data(self) -> None:
        self.kv[FIT_SAMPLE_KEY] = json.dumps({"cos": 0.5})
        self.assertEqual(load_fit_sample(self._get), [])

    def test_unparseable_entries_are_skipped_not_fatal(self) -> None:
        self.kv[FIT_SAMPLE_KEY] = json.dumps([0.3, "x", None, 0.6])
        self.assertEqual(load_fit_sample(self._get), [0.3, 0.6])

    def test_a_failing_store_is_survived(self) -> None:
        def boom(key: str) -> "str | None":
            raise RuntimeError("kv down")

        self.assertEqual(load_fit_sample(boom), [])

        def boom_set(key: str, value: str) -> None:
            raise RuntimeError("kv down")

        save_fit_sample(boom_set, [0.5])  # must not raise


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
