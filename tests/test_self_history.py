"""L19: the self-history arc builder -- eras, classification, thin records."""
from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

from app.core.concepts.concept_learning_event_store import (
    ConceptAlias,
    ConceptLearningEventStore,
    LearningEvent,
)
from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.self_history import build_self_history
from app.core.infra.chat_database import ChatDatabase


NOW = datetime(2026, 8, 20, 12, 0, tzinfo=timezone.utc)


def _iso(days_ago: float) -> str:
    return (NOW - timedelta(days=days_ago)).isoformat()


class Harness:
    def __init__(self) -> None:
        self.db = ChatDatabase(Path(tempfile.mkdtemp()) / "test.db")
        self.store = ConceptStore(self.db)
        self.learning = ConceptLearningEventStore(self.db)
        self._fp = 0

    def concept(self, label: str, *, days_ago: float = 40, **kw) -> int:
        base = dict(
            kind="identity",
            subject="aiko",
            status="active",
            confidence=0.8,
            plasticity=0.3,
            first_evidence_at=_iso(days_ago),
        )
        base.update(kw)
        return self.store.add(
            Concept(
                label=label,
                embedding=np.array([1.0, 0.0], dtype=np.float32),
                **base,  # type: ignore[arg-type]
            )
        )

    def event(self, cid: int, shape: str, *, days_ago: float = 5, **kw) -> int:
        self._fp += 1
        base = dict(
            shape=shape,
            concept_id=cid,
            subject="aiko",
            kind="identity",
            new_label="a belief",
            because=f"the reason for {shape}",
            salience=0.6,
            fingerprint=f"fp-{self._fp}",
            created_at=_iso(days_ago),
        )
        base.update(kw)
        return self.learning.add(LearningEvent(**base))  # type: ignore[arg-type]

    def build(self, **kw):
        params = dict(
            concept_store=self.store,
            learning_store=self.learning,
            subject="aiko",
            now=NOW,
        )
        params.update(kw)
        return build_self_history(**params)  # type: ignore[arg-type]


def _entries(arc) -> list:
    return [e for era in arc.eras for e in era.entries]


def _by_id(arc, cid: int):
    for entry in _entries(arc):
        if entry.concept_id == cid:
            return entry
    raise AssertionError(f"no entry for concept {cid}")


class EmptyRecordTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_an_empty_store_is_a_thin_record(self) -> None:
        arc = self.h.build()
        self.assertTrue(arc.thin_record)
        self.assertEqual(arc.eras, ())
        self.assertEqual(arc.total_concepts, 0)

    def test_beliefs_she_has_only_held_are_not_a_history_of_change(
        self,
    ) -> None:
        # The failure this guards: a store full of settled beliefs must not
        # license "yes, I've changed a lot".
        for i in range(6):
            self.h.concept(f"a steady belief {i}")
        arc = self.h.build()
        self.assertTrue(arc.thin_record)
        self.assertEqual(arc.counts.get("settled"), 6)

    def test_enough_real_change_clears_the_thin_flag(self) -> None:
        for i in range(3):
            cid = self.h.concept(f"a changed belief {i}", status="retired")
            self.h.event(cid, "loss", days_ago=3 + i)
        arc = self.h.build()
        self.assertFalse(arc.thin_record)
        self.assertEqual(arc.counts.get("faded"), 3)

    def test_the_floor_is_configurable(self) -> None:
        cid = self.h.concept("one changed belief", status="retired")
        self.h.event(cid, "loss")
        self.assertTrue(self.h.build().thin_record)
        self.assertFalse(self.h.build(min_entries=1).thin_record)

    def test_a_concept_with_no_datable_origin_is_omitted(self) -> None:
        # Guessing a date in a self-history is worse than an omission.
        self.h.concept("undated", first_evidence_at="")
        arc = self.h.build()
        self.assertEqual(_entries(arc), [])
        self.assertEqual(arc.total_concepts, 1)


class ClassificationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_a_superseded_belief_reads_as_flipped_with_its_prior(self) -> None:
        cid = self.h.concept("what I think now")
        self.h.event(
            cid,
            "succession",
            old_label="what I used to think",
            because="what looked like A turned out to be B",
        )
        entry = _by_id(self.h.build(), cid)
        self.assertEqual(entry.change, "flipped")
        self.assertEqual(entry.prior_label, "what I used to think")
        self.assertIn("turned out to be", entry.because)

    def test_a_rewritten_belief_also_reads_as_flipped(self) -> None:
        cid = self.h.concept("said more precisely")
        self.h.event(cid, "relabel", old_label="said vaguely")
        self.assertEqual(_by_id(self.h.build(), cid).change, "flipped")

    def test_a_retired_belief_reads_as_faded(self) -> None:
        cid = self.h.concept("no longer held", status="retired")
        self.h.event(cid, "loss", because="the support fell away")
        entry = _by_id(self.h.build(), cid)
        self.assertEqual(entry.change, "faded")
        self.assertEqual(entry.because, "the support fell away")

    def test_a_dormant_belief_also_reads_as_faded(self) -> None:
        cid = self.h.concept("gone quiet", status="dormant")
        self.assertEqual(_by_id(self.h.build(), cid).change, "faded")

    def test_a_flip_outranks_the_fade_of_its_losing_side(self) -> None:
        # A succession's old side is retired too, but "it was replaced by
        # something better" says more than "it stopped".
        cid = self.h.concept("the old reading", status="retired")
        self.h.event(cid, "loss", days_ago=6)
        self.h.event(cid, "succession", days_ago=5, old_label="the old reading")
        self.assertEqual(_by_id(self.h.build(), cid).change, "flipped")

    def test_a_revived_belief_is_its_own_category(self) -> None:
        cid = self.h.concept("it came back")
        self.h.event(cid, "revival", because="it came back after fading")
        self.assertEqual(_by_id(self.h.build(), cid).change, "revived")

    def test_an_emerged_belief_reads_as_born(self) -> None:
        cid = self.h.concept("newly held")
        self.h.event(cid, "emergence", because="enough moments pointed here")
        self.assertEqual(_by_id(self.h.build(), cid).change, "born")

    def test_an_unchanged_belief_reads_as_settled(self) -> None:
        cid = self.h.concept("held all along")
        entry = _by_id(self.h.build(), cid)
        self.assertEqual(entry.change, "settled")
        self.assertEqual(entry.because, "")

    def test_retired_beliefs_are_included_not_filtered_out(self) -> None:
        # The whole point: what she no longer holds is the answer to "what
        # were you like before".
        gone = self.h.concept("what I dropped", status="retired")
        self.h.event(gone, "loss")
        self.assertIn(gone, [e.concept_id for e in _entries(self.h.build())])


class ProvenanceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_every_entry_carries_the_ids_behind_it(self) -> None:
        cid = self.h.concept("a changed belief")
        first = self.h.event(cid, "emergence", days_ago=9)
        second = self.h.event(cid, "relabel", days_ago=4)
        entry = _by_id(self.h.build(), cid)
        self.assertEqual(entry.concept_id, cid)
        self.assertEqual(
            sorted(entry.learning_event_ids), sorted([first, second])
        )

    def test_a_succession_appears_on_both_endpoints(self) -> None:
        old = self.h.concept("the old belief", status="retired")
        new = self.h.concept("the new belief")
        self.h.event(
            new, "succession", prior_concept_id=old, old_label="the old belief"
        )
        arc = self.h.build()
        self.assertEqual(_by_id(arc, new).change, "flipped")
        # The faded side keeps the event too, so its story is not a dead end.
        self.assertEqual(_by_id(arc, old).change, "flipped")

    def test_a_merged_away_belief_surfaces_on_its_survivor(self) -> None:
        cid = self.h.concept("the surviving belief")
        self.h.learning.record_alias(
            ConceptAlias(
                absorbed_id=9001,
                canonical_id=cid,
                absorbed_label="the belief that was folded in",
                subject="aiko",
                merged_at=_iso(6),
            )
        )
        entry = _by_id(self.h.build(), cid)
        self.assertEqual(
            entry.absorbed_labels, ("the belief that was folded in",)
        )

    def test_a_change_is_dated_when_it_happened(self) -> None:
        # Not when the belief started: "I used to think X" belongs in the
        # era where it stopped being true.
        cid = self.h.concept("a long-held belief", days_ago=60)
        self.h.event(cid, "relabel", days_ago=2, old_label="the old wording")
        entry = _by_id(self.h.build(), cid)
        self.assertGreater(entry.at, _iso(3))

    def test_a_settled_belief_is_dated_from_its_origin(self) -> None:
        cid = self.h.concept("held all along", days_ago=50)
        self.assertEqual(_by_id(self.h.build(), cid).at[:10], _iso(50)[:10])


class EraTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_a_short_history_is_grouped_into_weeks(self) -> None:
        for days in (2, 9, 16):
            cid = self.h.concept(f"belief from {days} days ago", days_ago=days)
            self.h.event(cid, "emergence", days_ago=days)
        arc = self.h.build()
        self.assertGreater(len(arc.eras), 1)
        self.assertTrue(all("week of" in era.label for era in arc.eras))

    def test_a_long_history_is_grouped_into_months(self) -> None:
        for days in (10, 60, 150):
            cid = self.h.concept(f"belief from {days} days ago", days_ago=days)
            self.h.event(cid, "emergence", days_ago=days)
        arc = self.h.build()
        self.assertTrue(all("week of" not in era.label for era in arc.eras))
        self.assertIn("2026", arc.eras[0].label)

    def test_eras_read_oldest_first(self) -> None:
        for days in (3, 40, 200):
            cid = self.h.concept(f"belief from {days} days ago", days_ago=days)
            self.h.event(cid, "emergence", days_ago=days)
        labels = [era.start for era in self.h.build().eras]
        self.assertEqual(labels, sorted(labels))

    def test_an_era_keeps_its_most_informative_lines_when_capped(self) -> None:
        flipped = self.h.concept("the flipped one")
        self.h.event(flipped, "relabel", days_ago=3, old_label="before")
        for i in range(5):
            self.h.concept(f"a settled belief {i}", days_ago=3)
        arc = self.h.build(max_entries_per_era=2)
        [era] = arc.eras
        self.assertEqual(len(era.entries), 2)
        self.assertEqual(era.entries[0].concept_id, flipped)
        self.assertEqual(era.truncated, 4)

    def test_too_many_eras_keeps_the_most_recent(self) -> None:
        for days in (400, 370, 340, 10):
            cid = self.h.concept(f"belief from {days} days ago", days_ago=days)
            self.h.event(cid, "emergence", days_ago=days)
        arc = self.h.build(max_eras=2)
        self.assertEqual(len(arc.eras), 2)
        self.assertGreater(arc.eras[-1].start, _iso(40))

    def test_the_span_is_measured_from_the_earliest_evidence(self) -> None:
        cid = self.h.concept("an old belief", days_ago=100)
        self.h.event(cid, "emergence", days_ago=100)
        arc = self.h.build()
        self.assertAlmostEqual(arc.span_days, 100.0, delta=1.0)
        self.assertEqual(arc.first_evidence_at[:10], _iso(100)[:10])


class SubjectTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_the_subject_filters_the_arc(self) -> None:
        mine = self.h.concept("about me", subject="aiko")
        theirs = self.h.concept("about them", subject="user")
        self.h.event(mine, "emergence", subject="aiko")
        self.h.event(theirs, "emergence", subject="user", concept_id=theirs)
        ids = [e.concept_id for e in _entries(self.h.build(subject="aiko"))]
        self.assertIn(mine, ids)
        self.assertNotIn(theirs, ids)

    def test_the_user_arc_is_available_too(self) -> None:
        theirs = self.h.concept("about them", subject="user")
        arc = self.h.build(subject="user")
        self.assertEqual(arc.subject, "user")
        self.assertIn(theirs, [e.concept_id for e in _entries(arc)])


class PayloadTests(unittest.TestCase):
    def setUp(self) -> None:
        self.h = Harness()

    def test_as_dict_is_json_shaped_and_flags_a_thin_record(self) -> None:
        payload = self.h.build().as_dict()
        self.assertTrue(payload["thin_record"])
        self.assertEqual(payload["eras"], [])
        self.assertIn("span_days", payload)

    def test_empty_optional_fields_are_omitted(self) -> None:
        # A tool payload full of empty keys reads to the model as "I looked
        # and found nothing" about things it never asked.
        self.h.concept("held all along")
        payload = self.h.build().as_dict()
        entry = payload["eras"][0]["entries"][0]
        self.assertNotIn("because", entry)
        self.assertNotIn("prior_label", entry)
        self.assertNotIn("absorbed_labels", entry)
        self.assertIn("change", entry)

    def test_a_populated_entry_carries_its_provenance_in_the_payload(
        self,
    ) -> None:
        cid = self.h.concept("a flipped belief")
        self.h.event(cid, "relabel", old_label="the old wording")
        payload = self.h.build().as_dict()
        entry = payload["eras"][0]["entries"][0]
        self.assertEqual(entry["prior_label"], "the old wording")
        self.assertEqual(entry["concept_id"], cid)
        self.assertTrue(entry["learning_event_ids"])


class ReadCostTests(unittest.TestCase):
    def test_the_stores_are_read_a_bounded_number_of_times(self) -> None:
        # Flat in the number of concepts: a query per concept would be
        # hundreds of round trips on a mature store.
        h = Harness()
        for i in range(30):
            cid = h.concept(f"belief {i}")
            h.event(cid, "emergence", days_ago=3)
        calls: list[str] = []
        real_list = h.learning.list
        real_aliases = h.learning.list_aliases
        h.learning.list = lambda **kw: (  # type: ignore[method-assign]
            calls.append("list") or real_list(**kw)
        )
        h.learning.list_aliases = lambda **kw: (  # type: ignore[method-assign]
            calls.append("aliases") or real_aliases(**kw)
        )
        h.build()
        self.assertEqual(calls.count("list"), 1)
        self.assertEqual(calls.count("aliases"), 1)


if __name__ == "__main__":
    unittest.main()
