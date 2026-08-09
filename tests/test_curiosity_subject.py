"""Tests for :mod:`app.core.proactive.curiosity_subject` (K87).

The module is pure, so these are just the two decisions it makes: is a
drafted line pointing at a person, and does the quota still owe us a
subject-mode draft.
"""
from __future__ import annotations

import unittest

from app.core.proactive.curiosity_subject import (
    MODE_PERSON,
    MODE_SUBJECT,
    deficit,
    is_person_directed,
    subject_share,
    wants_subject,
)


class PersonDirectedTests(unittest.TestCase):
    def test_the_legacy_shape_is_person_directed(self) -> None:
        self.assertTrue(
            is_person_directed(
                "Maybe ask Jacob how the interview went.", "Jacob",
            )
        )

    def test_second_person_counts_without_the_name(self) -> None:
        self.assertTrue(is_person_directed("Maybe ask how your week went"))

    def test_a_subject_wondering_is_not(self) -> None:
        self.assertFalse(
            is_person_directed(
                "Maybe bring up whether sourdough starters really improve "
                "with age.",
                "Jacob",
            )
        )

    def test_a_subject_wondering_that_names_him_errs_toward_person(self) -> None:
        # Conservative on purpose: over-counting the interview side makes
        # the quota produce more subject material, not less.
        self.assertTrue(
            is_person_directed(
                "Maybe bring up the fermentation thing Jacob mentioned.",
                "Jacob",
            )
        )

    def test_the_name_must_be_a_whole_word(self) -> None:
        self.assertFalse(is_person_directed("Maybe bring up jacobean drama", "Jacob"))

    def test_empty_text_is_not_directed(self) -> None:
        self.assertFalse(is_person_directed("", "Jacob"))


class ShareTests(unittest.TestCase):
    def test_empty_history_is_zero(self) -> None:
        self.assertEqual(subject_share([]), 0.0)

    def test_counts_only_subject_entries(self) -> None:
        modes = [MODE_SUBJECT, MODE_PERSON, MODE_PERSON, MODE_SUBJECT]
        self.assertAlmostEqual(subject_share(modes), 0.5)


class QuotaTests(unittest.TestCase):
    def test_a_cold_start_leads_with_her_own_subject(self) -> None:
        self.assertTrue(wants_subject([], quota=0.4))

    def test_a_starved_history_asks_for_subject(self) -> None:
        self.assertTrue(wants_subject([MODE_PERSON] * 5, quota=0.4))

    def test_a_history_at_quota_asks_for_person(self) -> None:
        modes = [MODE_SUBJECT, MODE_SUBJECT, MODE_PERSON, MODE_PERSON, MODE_PERSON]
        self.assertFalse(wants_subject(modes, quota=0.4))

    def test_it_converges_rather_than_alternating_forever(self) -> None:
        # Ten drafts at a 0.4 quota should land near 0.4, which a coin
        # flip only manages in expectation.
        modes: list[str] = []
        for _ in range(10):
            modes.append(
                MODE_SUBJECT if wants_subject(modes, quota=0.4) else MODE_PERSON
            )
        self.assertAlmostEqual(subject_share(modes), 0.4, delta=0.1)

    def test_zero_quota_disables_it(self) -> None:
        self.assertFalse(wants_subject([], quota=0.0))

    def test_full_quota_always_asks_for_subject(self) -> None:
        self.assertTrue(wants_subject([MODE_SUBJECT] * 9, quota=1.0))


class DeficitTests(unittest.TestCase):
    def test_an_empty_pool_needs_its_share_of_the_batch(self) -> None:
        self.assertEqual(deficit([], quota=0.5, total=4), 2)

    def test_a_batch_of_one_still_owes_a_subject_from_empty(self) -> None:
        # Rounding down here would pin a one-per-run writer at zero
        # subject seeds forever, which is the common configuration.
        self.assertEqual(deficit([], quota=0.4, total=1), 1)

    def test_stock_already_at_quota_needs_none(self) -> None:
        modes = [MODE_SUBJECT] * 4 + [MODE_PERSON] * 2
        self.assertEqual(deficit(modes, quota=0.4, total=2), 0)

    def test_it_never_asks_for_more_than_the_batch(self) -> None:
        modes = [MODE_PERSON] * 20
        self.assertEqual(deficit(modes, quota=0.9, total=2), 2)

    def test_zero_quota_needs_none(self) -> None:
        self.assertEqual(deficit([], quota=0.0, total=5), 0)
