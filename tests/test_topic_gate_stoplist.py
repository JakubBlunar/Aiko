"""The stoplist at admission, and the names that only settings can supply (H47).

Two things are worth asserting here and one is not. Worth asserting: that
a cue can no longer be admitted on a function word, and that a genuinely
shared subject still is -- those are the two halves of the trade H47 makes.
Also worth asserting: that the names reach the predicate, because that is
the part no default can cover and the part that silently degrades to
"slightly worse precision" rather than to an error.

Not worth asserting: the acceptance rate. 32.3% to 3.6% is a property of
this corpus, measured by ``scripts/topic_gate_report.py``, and pinning it
in a unit test would only record the fixture.
"""
from __future__ import annotations

import unittest

from app.core.proactive import topic_match
from app.core.proactive.associative_wander_worker import (
    distant_half,
    wander_relevant,
)
from app.core.proactive.curiosity_gradient_worker import gradient_relevant
from app.core.proactive.interest_drift_worker import drift_relevant
from app.core.proactive.knowledge_gap_notice_worker import topic_relevant
from app.core.session.cue_pool_mixin import CuePoolMixin


class _Assistant:
    def __init__(self, name: str = "Aiko", user_name: str = "Jacob") -> None:
        self.name = name
        self.user_name = user_name


class _Agent:
    def __init__(self, stoplist: bool = True) -> None:
        self.cue_topic_stoplist = stoplist


class _Settings:
    def __init__(self, stoplist: bool = True, **kw) -> None:
        self.agent = _Agent(stoplist)
        self.assistant = _Assistant(**kw)


class _Session(CuePoolMixin):
    """Just enough of a session to read the two settings blocks."""

    def __init__(self, settings) -> None:
        self._settings = settings


class WhatTheStoplistRefusesTests(unittest.TestCase):
    """The function words that carried 82% of the old gate's matches."""

    def test_a_shared_and_is_no_longer_a_match(self) -> None:
        """6,372 hits in the corpus -- the single largest false carrier."""
        self.assertFalse(
            topic_relevant("film photography and darkrooms", "the cpu and the board")
        )

    def test_a_shared_the_is_no_longer_a_match(self) -> None:
        self.assertFalse(topic_relevant("the darkroom", "the weather is awful"))

    def test_the_old_gate_would_have_taken_both(self) -> None:
        """Anchors the change: these are hits, not sentences I made up."""
        shipped = topic_match.GateOptions.shipped()
        self.assertTrue(
            topic_relevant(
                "film photography and darkrooms",
                "the cpu and the board",
                options=shipped,
            )
        )
        self.assertTrue(
            topic_relevant("the darkroom", "the weather is awful", options=shipped)
        )

    def test_a_real_shared_subject_still_matches(self) -> None:
        """The half of the trade that has to survive, or reach is gone."""
        self.assertTrue(
            topic_relevant("film photography", "I developed some film today")
        )

    def test_a_common_content_word_is_not_a_stopword(self) -> None:
        """"sleep" is common and is genuinely a subject; it stays in."""
        self.assertTrue(topic_relevant("sleep debt", "I need to sleep more"))

    def test_a_topic_of_nothing_but_stopwords_matches_nothing(self) -> None:
        """An empty token set must not degrade into matching everything."""
        self.assertFalse(topic_relevant("the and of it", "the and of it"))


class TheNamesTests(unittest.TestCase):
    """``aiko`` was the 4th-biggest false carrier at 580 hits."""

    def _options(self, **kw) -> topic_match.GateOptions:
        return _Session(_Settings(**kw))._topic_gate_options()

    def test_her_name_stops_matching_once_settings_supply_it(self) -> None:
        subject = "jacob's feelings for aiko"
        message = "aiko, what do you think?"
        self.assertTrue(
            topic_relevant(subject, message),
            "without the names this is still a hit -- that is the gap",
        )
        self.assertFalse(
            topic_relevant(subject, message, options=self._options())
        )

    def test_his_name_stops_matching_too(self) -> None:
        self.assertFalse(
            topic_relevant(
                "jacob's technical projects",
                "jacob here, just checking in",
                options=self._options(),
            )
        )

    def test_a_renamed_pair_is_honoured(self) -> None:
        """The names are config, so a hardcoded list would be wrong."""
        options = self._options(name="Luna", user_name="Sam")
        self.assertEqual(set(options.extra_stop), {"luna", "sam"})
        self.assertFalse(
            topic_relevant("luna's day", "hey luna", options=options)
        )

    def test_blank_names_do_not_become_a_stopword(self) -> None:
        """An empty string in ``extra_stop`` would be harmless but untidy."""
        options = self._options(name="", user_name="   ")
        self.assertEqual(options.extra_stop, ())
        self.assertTrue(options.drop_stopwords)

    def test_a_missing_assistant_block_still_yields_a_stoplist(self) -> None:
        class Bare:
            agent = _Agent(True)

        options = _Session(Bare())._topic_gate_options()
        self.assertTrue(options.drop_stopwords)
        self.assertEqual(options.extra_stop, ())


class TheOffSwitchTests(unittest.TestCase):
    def test_off_restores_the_pre_h47_gate_exactly(self) -> None:
        options = _Session(_Settings(stoplist=False))._topic_gate_options()
        self.assertFalse(options.drop_stopwords)
        self.assertEqual(options.extra_stop, ())
        self.assertTrue(
            topic_relevant(
                "film photography and darkrooms",
                "the cpu and the board",
                options=options,
            )
        )

    def test_off_does_not_smuggle_the_names_in(self) -> None:
        """Names are only stopwords when there is a stoplist to add to."""
        options = _Session(_Settings(stoplist=False))._topic_gate_options()
        self.assertTrue(
            topic_relevant(
                "jacob's feelings for aiko",
                "aiko, what do you think?",
                options=options,
            )
        )

    def test_a_session_with_no_settings_at_all_stays_strict(self) -> None:
        """Default-on has to survive a half-built session, not crash it."""
        class Nothing:
            pass

        options = _Session(Nothing())._topic_gate_options()
        self.assertTrue(options.drop_stopwords)


class EveryProviderHelperHonoursItTests(unittest.TestCase):
    """Four helpers forward the options; a missed one is a silent hole."""

    def setUp(self) -> None:
        self.options = _Session(_Settings())._topic_gate_options()

    def test_interest_drift(self) -> None:
        entry = {"topic": "film photography and darkrooms"}
        self.assertFalse(
            drift_relevant(entry, "the cpu and the board", options=self.options)
        )
        self.assertTrue(
            drift_relevant(entry, "developing film tonight", options=self.options)
        )

    def test_curiosity_gradient_checks_both_sides(self) -> None:
        entry = {"dense_topic": "aiko and jacob", "thin_topic": "darkroom chemistry"}
        self.assertFalse(
            gradient_relevant(entry, "aiko and the cpu", options=self.options)
        )
        self.assertTrue(
            gradient_relevant(entry, "darkroom setup", options=self.options)
        )

    def test_associative_wander_checks_both_sides(self) -> None:
        entry = {"topic_a": "aiko and jacob", "topic_b": "film photography"}
        self.assertFalse(
            wander_relevant(entry, "aiko and the weather", options=self.options)
        )
        self.assertTrue(
            wander_relevant(entry, "shooting film", options=self.options)
        )

    def test_the_distant_half_is_chosen_under_the_stoplist(self) -> None:
        """The near/far split decides what post-turn matches against."""
        entry = {"topic_a": "film photography", "topic_b": "cpu builds"}
        self.assertEqual(
            distant_half(entry, "developing film tonight", options=self.options),
            "cpu builds",
        )

    def test_a_name_only_hit_no_longer_picks_the_near_half(self) -> None:
        """Pre-H47 a shared 'aiko' decided which half was live."""
        entry = {"topic_a": "aiko and jacob", "topic_b": "film photography"}
        self.assertEqual(
            distant_half(entry, "aiko, hello", options=self.options),
            "film photography",
            "neither half is live, so it falls back to b",
        )
        self.assertEqual(
            distant_half(
                entry, "aiko, hello", options=topic_match.GateOptions.shipped(),
            ),
            "film photography",
        )


class GateOptionsTests(unittest.TestCase):
    def test_the_default_is_strict(self) -> None:
        self.assertTrue(topic_match.DEFAULT_OPTIONS.drop_stopwords)

    def test_options_are_hashable_so_a_test_can_assert_on_them(self) -> None:
        a = topic_match.GateOptions(extra_stop=("aiko",))
        b = topic_match.GateOptions(extra_stop=("aiko",))
        self.assertEqual(a, b)
        self.assertEqual(len({a, b}), 1)

    def test_topical_now_defaults_to_the_stoplist_too(self) -> None:
        hit, arm, _ = topic_match.topical(
            "film photography and darkrooms", "the cpu and the board",
        )
        self.assertFalse(hit)
        self.assertEqual(arm, topic_match.ARM_NONE)


if __name__ == "__main__":
    unittest.main()
