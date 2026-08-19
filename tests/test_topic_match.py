"""H43 -- the shared topic gate, its two arms, and picking by relevance.

The module under test replaced a fourteen-line word-overlap predicate that
five cue providers share. The tests are organised around the three claims
that justified the change, because each one is a thing that could regress
quietly:

* the stoplist removes function words and *only* function words,
* admission is unchanged, so reach cannot fall,
* selection among admitted cues is by relevance rather than by recency.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive import topic_match
from app.core.proactive.cue_producer import pick_pool_cue
from app.core.proactive.cue_store import CueStore
from app.core.proactive.knowledge_gap_notice_worker import topic_relevant


def _vec(*xs: float) -> np.ndarray:
    arr = np.asarray(xs, dtype=np.float32)
    return arr / float(np.linalg.norm(arr) or 1.0)


class ContentWordTests(unittest.TestCase):
    def test_the_three_char_floor_still_drops_fragments(self) -> None:
        self.assertEqual(
            topic_match.content_words("a to be my ok"), set(),
        )

    def test_function_words_are_dropped(self) -> None:
        """The measured top carriers: and, the, you, with, that, when."""
        got = topic_match.content_words(
            "and the you with that when for your",
        )
        self.assertEqual(got, set())

    def test_real_subjects_survive_the_stoplist(self) -> None:
        """Common *content* words are subjects and must not be stopped."""
        got = topic_match.content_words(
            "sleep and rest, work food guitar records",
        )
        self.assertEqual(
            got, {"sleep", "rest", "work", "food", "guitar", "records"},
        )

    def test_names_are_stoppable_but_not_hardcoded(self) -> None:
        text = "aiko and jacob talking about bread"
        self.assertIn("aiko", topic_match.content_words(text))
        self.assertNotIn(
            "aiko",
            topic_match.content_words(text, extra_stop=["Aiko", "Jacob"]),
        )
        self.assertIn(
            "bread",
            topic_match.content_words(text, extra_stop=["Aiko", "Jacob"]),
        )

    def test_stoplist_can_be_turned_off_for_the_admission_path(self) -> None:
        got = topic_match.content_words("and the", drop_stopwords=False)
        self.assertEqual(got, {"and", "the"})


class LexicalArmTests(unittest.TestCase):
    def test_a_shared_subject_word_is_a_hit(self) -> None:
        self.assertTrue(
            topic_match.lexical_overlap(
                "learning to play guitar", "I picked up the guitar again",
            )
        )

    def test_a_shared_function_word_is_not(self) -> None:
        """The bug in one line: these two sentences share only 'and'."""
        self.assertFalse(
            topic_match.lexical_overlap(
                "bread baking and sourdough", "the driver and the reboot",
            )
        )

    def test_the_shipped_admission_rule_still_accepts_it(self) -> None:
        """Admission is deliberately unchanged -- reach must not fall."""
        self.assertTrue(
            topic_relevant("bread baking and sourdough", "the driver and so on")
        )

    def test_an_empty_topic_never_matches(self) -> None:
        self.assertFalse(topic_match.lexical_overlap("", "anything at all"))
        self.assertFalse(topic_relevant("", "anything at all"))


class CosineTests(unittest.TestCase):
    def test_a_missing_vector_is_no_opinion_not_zero(self) -> None:
        """``None`` falls through to lexical; 0.0 would assert unrelated."""
        self.assertIsNone(topic_match.cosine(None, _vec(1, 0)))
        self.assertIsNone(topic_match.cosine(_vec(1, 0), None))

    def test_mismatched_dimensions_are_no_opinion(self) -> None:
        self.assertIsNone(topic_match.cosine([1.0, 0.0], [1.0, 0.0, 0.0]))

    def test_a_zero_vector_is_no_opinion(self) -> None:
        self.assertIsNone(topic_match.cosine([0.0, 0.0], [1.0, 0.0]))

    def test_lists_and_arrays_interoperate(self) -> None:
        """Cue vectors come back from SQLite as lists, live embeds as arrays."""
        got = topic_match.cosine([1.0, 0.0], _vec(1, 0))
        assert got is not None
        self.assertAlmostEqual(got, 1.0, places=5)


class TopicalArmReportingTests(unittest.TestCase):
    def test_lexical_wins_and_still_reports_its_cosine(self) -> None:
        """The asymmetry that made the old calibration unreadable."""
        hit, arm, cos = topic_match.topical(
            "guitar practice",
            "my guitar again",
            topic_vec=_vec(1, 0),
            user_vec=_vec(0, 1),
        )
        self.assertTrue(hit)
        self.assertEqual(arm, topic_match.ARM_LEXICAL)
        self.assertIsNotNone(cos)

    def test_the_cosine_arm_catches_what_words_cannot(self) -> None:
        hit, arm, _cos = topic_match.topical(
            "sleep and rest",
            "I went to bed far too late",
            topic_vec=_vec(1, 0),
            user_vec=_vec(1, 0),
        )
        self.assertTrue(hit)
        self.assertEqual(arm, topic_match.ARM_COSINE)

    def test_below_the_floor_is_a_miss(self) -> None:
        hit, arm, _cos = topic_match.topical(
            "sleep and rest",
            "the graphics driver crashed",
            topic_vec=_vec(1, 0),
            user_vec=_vec(0, 1),
        )
        self.assertFalse(hit)
        self.assertEqual(arm, topic_match.ARM_NONE)

    def test_the_arm_can_be_disabled_without_losing_the_score(self) -> None:
        hit, _arm, cos = topic_match.topical(
            "sleep and rest",
            "I went to bed far too late",
            topic_vec=_vec(1, 0),
            user_vec=_vec(1, 0),
            min_cosine=None,
        )
        self.assertFalse(hit)
        self.assertIsNotNone(cos)

    def test_the_default_floor_sits_above_the_measured_null(self) -> None:
        """The null's p99 was 0.559-0.574; the floor must be in that band."""
        self.assertGreaterEqual(topic_match.DEFAULT_MIN_COSINE, 0.5)
        self.assertLessEqual(topic_match.DEFAULT_MIN_COSINE, 0.7)


class _PickFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))

    def _add(self, subject: str, vec) -> int:
        return self.store.add(
            "interest_drift",
            subject,
            f"cue about {subject}",
            payload={"topic": subject},
            embedding=list(vec),
        )


class RankByRelevanceTests(_PickFixture):
    """The change: 'best' means closest to the message, not least recent."""

    def test_the_most_relevant_admitted_cue_wins(self) -> None:
        # Added second, so first-past-the-post (surfacings, then recency
        # descending) would put this one *first*.
        self._add("the far one", _vec(0, 1))
        self._add("the near one", _vec(1, 0))
        pick = pick_pool_cue(self.store, "interest_drift", user_vec=_vec(1, 0))
        assert pick.row is not None
        self.assertEqual(pick.row.subject, "the near one")
        self.assertEqual(pick.admitted, 2)

    def test_recency_still_decides_when_nothing_can_be_compared(self) -> None:
        """No embedder, no user vector: exactly the old behaviour."""
        self._add("older", _vec(1, 0))
        self._add("newer", _vec(0, 1))
        pick = pick_pool_cue(self.store, "interest_drift")
        assert pick.row is not None
        self.assertEqual(pick.row.subject, "newer")

    def test_a_cue_without_an_embedding_is_still_pickable(self) -> None:
        """It just cannot win on relevance, and must not crash or vanish."""
        self.store.add(
            "interest_drift", "vectorless", "x", payload={"topic": "vectorless"},
        )
        pick = pick_pool_cue(self.store, "interest_drift", user_vec=_vec(1, 0))
        assert pick.row is not None
        self.assertEqual(pick.row.subject, "vectorless")

    def test_ranking_never_reaches_past_the_predicate(self) -> None:
        """A closer cue the provider refused must not win on cosine alone."""
        self._add("refused but close", _vec(1, 0))
        self._add("allowed but far", _vec(0, 1))
        pick = pick_pool_cue(
            self.store,
            "interest_drift",
            relevant=lambda p: p.get("topic") == "allowed but far",
            user_vec=_vec(1, 0),
            min_cosine=None,
        )
        assert pick.row is not None
        self.assertEqual(pick.row.subject, "allowed but far")


class AdmissionIsUnchangedTests(_PickFixture):
    """Reach cannot fall: the cosine arm only ever *adds*."""

    def test_the_semantic_arm_admits_what_the_predicate_refused(self) -> None:
        self._add("worded differently", _vec(1, 0))
        pick = pick_pool_cue(
            self.store,
            "interest_drift",
            relevant=lambda _p: False,
            user_vec=_vec(1, 0),
            min_cosine=0.55,
        )
        assert pick.row is not None
        self.assertEqual(pick.arm, topic_match.ARM_COSINE)

    def test_a_distant_cue_is_not_admitted_by_the_arm(self) -> None:
        self._add("unrelated", _vec(0, 1))
        pick = pick_pool_cue(
            self.store,
            "interest_drift",
            relevant=lambda _p: False,
            user_vec=_vec(1, 0),
            min_cosine=0.55,
        )
        self.assertIsNone(pick.row)
        self.assertEqual(pick.rejected, 1)

    def test_everything_the_predicate_accepts_is_still_accepted(self) -> None:
        """The monotonicity claim, stated as a test."""
        self._add("accepted", _vec(0, 1))
        pick = pick_pool_cue(
            self.store,
            "interest_drift",
            relevant=lambda _p: True,
            user_vec=_vec(1, 0),
            min_cosine=0.55,
        )
        self.assertIsNotNone(pick.row)
        self.assertEqual(pick.arm, topic_match.ARM_LEXICAL)

    def test_force_still_bypasses_everything(self) -> None:
        self._add("forced", _vec(0, 1))
        pick = pick_pool_cue(
            self.store, "interest_drift", relevant=lambda _p: False, force=True,
        )
        self.assertIsNotNone(pick.row)


class WalkAccountingTests(_PickFixture):
    """The counts that let the caller stop guessing at the decline reason."""

    def test_an_empty_shelf_reports_nothing_considered(self) -> None:
        pick = pick_pool_cue(self.store, "interest_drift")
        self.assertIsNone(pick.row)
        self.assertEqual(pick.considered, 0)
        self.assertEqual(pick.rejected, 0)
        self.assertEqual(pick.held_for_cadence, 0)

    def test_predicate_refusals_are_counted(self) -> None:
        self._add("one", _vec(1, 0))
        self._add("two", _vec(0, 1))
        pick = pick_pool_cue(
            self.store, "interest_drift", relevant=lambda _p: False,
        )
        self.assertEqual(pick.considered, 2)
        self.assertEqual(pick.rejected, 2)

    def test_cadence_holds_are_counted_apart_from_refusals(self) -> None:
        """The mislabelling this exists to prevent: a hold is not a miss."""
        self._add("never shown", _vec(1, 0))
        pick = pick_pool_cue(
            self.store,
            "interest_drift",
            relevant=lambda _p: False,
            allow_first_claim=False,
        )
        self.assertIsNone(pick.row)
        self.assertEqual(pick.held_for_cadence, 1)
        self.assertEqual(pick.rejected, 0)

    def test_no_store_is_an_empty_walk_not_a_crash(self) -> None:
        pick = pick_pool_cue(None, "interest_drift")
        self.assertIsNone(pick.row)
        self.assertFalse(pick)


if __name__ == "__main__":
    unittest.main()
