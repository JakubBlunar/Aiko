"""Pure-module tests for K73 shared-ritual formation."""
from __future__ import annotations

import unittest
from datetime import date, timedelta

from app.core.relationship import shared_ritual as sr


class DominantShapeTests(unittest.TestCase):
    def test_no_signal_defaults_check_in(self) -> None:
        self.assertEqual(sr.dominant_shape([None, None]), "casual_check_in")
        self.assertEqual(sr.dominant_shape([]), "casual_check_in")

    def test_modal_arc_wins(self) -> None:
        self.assertEqual(
            sr.dominant_shape(["support", "support", None, "planning"]),
            "support",
        )

    def test_single_signal_defines_shape(self) -> None:
        self.assertEqual(sr.dominant_shape([None, "reflection"]), "reflection")


class DetectRitualsTests(unittest.TestCase):
    def _slot(self, weeks: int):
        return {
            ("friday", "evening", "casual_check_in"): {
                (2026, w) for w in range(1, weeks + 1)
            }
        }

    def test_fires_when_recurring(self) -> None:
        out = sr.detect_rituals(
            self._slot(3), total_weeks=8, min_weeks=3, min_share=0.34,
        )
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.key, "friday:evening:casual_check_in")
        self.assertEqual(c.cadence, "Friday evenings")
        self.assertEqual(c.shape_label, "check-ins")
        self.assertEqual(c.label, "our Friday-evening check-ins")
        self.assertEqual(c.weeks_seen, 3)

    def test_below_min_weeks_silent(self) -> None:
        self.assertEqual(
            sr.detect_rituals(
                self._slot(2), total_weeks=8, min_weeks=3, min_share=0.0,
            ),
            [],
        )

    def test_below_min_share_silent(self) -> None:
        # 3 weeks out of 20 = 0.15 share, below 0.34.
        self.assertEqual(
            sr.detect_rituals(
                self._slot(3), total_weeks=20, min_weeks=3, min_share=0.34,
            ),
            [],
        )

    def test_late_and_support_labels(self) -> None:
        slot = {
            ("saturday", "late", "support"): {(2026, w) for w in range(1, 5)},
        }
        out = sr.detect_rituals(slot, total_weeks=8, min_weeks=3)
        self.assertEqual(out[0].label, "our late-night Saturday heart-to-hearts")
        self.assertEqual(out[0].cadence, "Saturday late nights")

    def test_sorted_and_capped(self) -> None:
        slot = {
            ("friday", "evening", "casual_check_in"): {(2026, w) for w in range(1, 4)},
            ("sunday", "morning", "casual_check_in"): {(2026, w) for w in range(1, 6)},
        }
        out = sr.detect_rituals(
            slot, total_weeks=8, min_weeks=3, min_share=0.0, max_rituals=1,
        )
        self.assertEqual(len(out), 1)
        # Sunday recurred more weeks -> wins the cap.
        self.assertEqual(out[0].weekday, "sunday")


class MergeRitualsTests(unittest.TestCase):
    def _cand(self, key="friday:evening:casual_check_in", weeks=3):
        return sr.RitualCandidate(
            key=key, weekday="friday", bucket="evening",
            shape="casual_check_in", cadence="Friday evenings",
            shape_label="check-ins", label="our Friday-evening check-ins",
            weeks_seen=weeks, share=0.4,
        )

    def test_adds_new(self) -> None:
        merged, new = sr.merge_rituals([], [self._cand()], now_date="2026-06-01")
        self.assertEqual(len(merged), 1)
        self.assertEqual(new, ["friday:evening:casual_check_in"])
        self.assertFalse(merged[0]["acknowledged"])
        self.assertEqual(merged[0]["first_seen"], "2026-06-01")

    def test_preserves_acknowledged_and_first_seen(self) -> None:
        existing = [{
            "key": "friday:evening:casual_check_in",
            "label": "old", "weeks_seen": 3, "acknowledged": True,
            "first_seen": "2026-01-01",
        }]
        merged, new = sr.merge_rituals(
            existing, [self._cand(weeks=5)], now_date="2026-06-01",
        )
        self.assertEqual(new, [])
        row = merged[0]
        self.assertTrue(row["acknowledged"])
        self.assertEqual(row["first_seen"], "2026-01-01")
        self.assertEqual(row["weeks_seen"], 5)

    def test_drops_pending_fade(self) -> None:
        existing = [{
            "key": "monday:morning:casual_check_in",
            "label": "x", "weeks_seen": 3, "acknowledged": False,
        }]
        merged, _ = sr.merge_rituals(existing, [], now_date="2026-06-01")
        self.assertEqual(merged, [])

    def test_keeps_acknowledged_fade(self) -> None:
        existing = [{
            "key": "monday:morning:casual_check_in",
            "label": "x", "weeks_seen": 3, "acknowledged": True,
        }]
        merged, _ = sr.merge_rituals(existing, [], now_date="2026-06-01")
        self.assertEqual(len(merged), 1)


class BudgetStarvationTests(unittest.TestCase):
    """The permanent record must not consume the pending budget.

    K73 named one ritual and then stopped forever. Acknowledged rituals
    are permanent by design and the cap covered the whole store, so the
    pending budget was ``max(0, max_active - len(acknowledged))`` -- at
    six acknowledged that is zero, every newly-formed ritual was trimmed
    away before the save, and ``pick_unacknowledged`` returned ``None``
    on every sweep from then on. The live store reached exactly that
    state: 6 of 6 acknowledged, ``drafted=0`` on all eight recorded runs.
    """

    def _full_record(self, n=sr.DEFAULT_MAX_ACTIVE):
        start = date(2026, 1, 1)
        return [
            {
                "key": "day%d:evening:casual_check_in" % i,
                "label": "our thing %d" % i,
                "weeks_seen": 4,
                "acknowledged": True,
                "first_seen": (start + timedelta(days=i)).isoformat(),
            }
            for i in range(n)
        ]

    def _new_cand(self, key="saturday:afternoon:casual_check_in"):
        return sr.RitualCandidate(
            key=key, weekday="saturday", bucket="afternoon",
            shape="casual_check_in", cadence="Saturday afternoons",
            shape_label="check-ins",
            label="our Saturday-afternoon check-ins",
            weeks_seen=4, share=0.5,
        )

    def test_a_new_ritual_survives_a_full_record(self) -> None:
        merged, _ = sr.merge_rituals(
            self._full_record(), [self._new_cand()], now_date="2026-08-11",
        )
        keys = {r["key"] for r in merged}
        self.assertIn("saturday:afternoon:casual_check_in", keys)

    def test_and_is_therefore_offerable(self) -> None:
        """The assertion that actually matters -- surviving the trim is
        only useful if the publish step can then see it."""
        merged, _ = sr.merge_rituals(
            self._full_record(), [self._new_cand()], now_date="2026-08-11",
        )
        pick = sr.pick_unacknowledged(merged)
        self.assertIsNotNone(pick)
        assert pick is not None
        self.assertEqual(pick["key"], "saturday:afternoon:casual_check_in")

    def test_new_keys_only_names_rows_that_survived(self) -> None:
        """``new_keys`` was collected before the trim, so the sweep log
        reported the same doomed key as "new" on every run forever --
        eight identical lines that read like progress."""
        merged, new = sr.merge_rituals(
            self._full_record(),
            [self._new_cand()],
            now_date="2026-08-11",
            max_active=0,
        )
        kept = {r["key"] for r in merged}
        for key in new:
            self.assertIn(key, kept)

    def test_the_record_stays_bounded(self) -> None:
        """Independent budgets must not mean an unbounded blob: the
        record is trimmed against its own cap, oldest first."""
        merged, _ = sr.merge_rituals(
            self._full_record(n=40), [], now_date="2026-08-11",
        )
        self.assertEqual(len(merged), sr.DEFAULT_MAX_ACKNOWLEDGED)

    def test_the_record_drops_the_oldest_first(self) -> None:
        merged, _ = sr.merge_rituals(
            self._full_record(n=40), [], now_date="2026-08-11",
            max_acknowledged=2,
        )
        self.assertEqual(
            sorted(r["first_seen"] for r in merged),
            ["2026-02-08", "2026-02-09"],
        )

    def test_pending_is_still_capped(self) -> None:
        cands = [
            self._new_cand(key="d%d:evening:casual_check_in" % i)
            for i in range(20)
        ]
        merged, _ = sr.merge_rituals([], cands, now_date="2026-08-11")
        self.assertEqual(len(merged), sr.DEFAULT_MAX_ACTIVE)


class EvictedRitualIsNotReAnnouncedTests(unittest.TestCase):
    """An evicted ritual that still qualifies must not be offered again.

    The cap H22 fixed starved the *pending* budget. This is the second
    cap in the same function, and it bites from the other side: a row
    trimmed by ``max_acknowledged`` while still an active candidate
    re-enters through the brand-new branch, so it arrives
    ``acknowledged=False`` with a fresh ``first_seen`` and a slot in
    ``new_keys`` -- an offer for a ritual already named. Its refreshed
    ``first_seen`` then makes it one of the newest rows, so the next
    trim evicts a genuinely older one and the loop rotates through the
    record.

    The reason it went unseen is the fixture: the test above evicts 40
    rows while passing **no candidates**, so nothing it drops can come
    back. Every assertion here passes candidates for the rows it evicts.
    """

    def _acknowledged(self, n, *, start=date(2026, 1, 1)):
        return [
            {
                "key": "day%d:evening:casual_check_in" % i,
                "label": "our thing %d" % i,
                "weeks_seen": 4,
                "share": 0.5,
                "acknowledged": True,
                "first_seen": (start + timedelta(days=i)).isoformat(),
            }
            for i in range(n)
        ]

    def _cand_for(self, row):
        return sr.RitualCandidate(
            key=row["key"], weekday="day", bucket="evening",
            shape="casual_check_in", cadence="evenings",
            shape_label="check-ins", label=row["label"],
            weeks_seen=4, share=0.5,
        )

    def test_the_ledger_keeps_an_evicted_ritual_acknowledged(self) -> None:
        record = self._acknowledged(4)
        cands = [self._cand_for(r) for r in record]
        merged, new = sr.merge_rituals(
            record[2:], cands, now_date="2026-08-11",
            max_acknowledged=2,
            named_keys={r["key"] for r in record},
        )
        by_key = {r["key"]: r for r in merged}
        for row in record[:2]:
            with self.subTest(key=row["key"]):
                self.assertTrue(
                    by_key[row["key"]]["acknowledged"],
                    msg="an evicted-then-redetected ritual came back "
                        "pending, so it is owed a second announcement",
                )
        self.assertEqual(new, [], msg=f"reported as new: {new}")

    def test_without_the_ledger_it_is_offered_again(self) -> None:
        """Pins the defect itself, so the ledger cannot be dropped."""
        record = self._acknowledged(4)
        cands = [self._cand_for(r) for r in record]
        merged, new = sr.merge_rituals(
            record[2:], cands, now_date="2026-08-11", max_acknowledged=2,
        )
        pick = sr.pick_unacknowledged(merged)
        self.assertIsNotNone(pick)
        self.assertTrue(new)

    def test_a_genuinely_new_ritual_is_still_offered(self) -> None:
        """The ledger must not become a blanket veto."""
        record = self._acknowledged(2)
        fresh = sr.RitualCandidate(
            key="sunday:late:support", weekday="sunday", bucket="late",
            shape="support", cadence="Sunday late nights",
            shape_label="heart-to-hearts",
            label="our late-night Sunday heart-to-hearts",
            weeks_seen=5, share=0.6,
        )
        merged, new = sr.merge_rituals(
            record, [self._cand_for(record[0]), fresh],
            now_date="2026-08-11",
            named_keys={r["key"] for r in record},
        )
        self.assertEqual(new, ["sunday:late:support"])
        pick = sr.pick_unacknowledged(merged)
        assert pick is not None
        self.assertEqual(pick["key"], "sunday:late:support")

    def test_the_ledger_round_trips_and_stays_bounded(self) -> None:
        store: dict[str, str] = {}
        sr.save_named_keys(
            store.__setitem__, {"b:late:support", "a:evening:playful"},
        )
        self.assertEqual(
            sr.load_named_keys(store.get),
            {"b:late:support", "a:evening:playful"},
        )
        sr.save_named_keys(
            store.__setitem__,
            {"k%03d" % i for i in range(50)},
            max_keys=10,
        )
        self.assertEqual(len(sr.load_named_keys(store.get)), 10)

    def test_a_missing_or_corrupt_ledger_reads_as_empty(self) -> None:
        self.assertEqual(sr.load_named_keys(lambda _k: None), set())
        self.assertEqual(sr.load_named_keys(lambda _k: "{oh no"), set())
        self.assertEqual(sr.load_named_keys(lambda _k: '{"a": 1}'), set())


class PickAndRenderTests(unittest.TestCase):
    def test_pick_unacknowledged_strongest(self) -> None:
        rituals = [
            {"key": "a", "weeks_seen": 3, "acknowledged": True},
            {"key": "b", "weeks_seen": 4, "acknowledged": False},
            {"key": "c", "weeks_seen": 6, "acknowledged": False},
        ]
        self.assertEqual(sr.pick_unacknowledged(rituals)["key"], "c")

    def test_pick_none_when_all_acknowledged(self) -> None:
        self.assertIsNone(
            sr.pick_unacknowledged([{"key": "a", "acknowledged": True}])
        )

    def test_render_warm_cue(self) -> None:
        line = sr.render_inner_life_block(
            {"label": "our Friday-evening wind-downs", "weeks_seen": 5},
            user_display_name="Jacob",
        )
        self.assertIn("our Friday-evening wind-downs", line)
        self.assertIn("Jacob", line)
        self.assertIn("our thing", line)
        self.assertIn("ONCE", line)

    def test_render_empty_without_label(self) -> None:
        self.assertEqual(sr.render_inner_life_block({"weeks_seen": 5}), "")

    def test_mark_acknowledged(self) -> None:
        rituals = [{"key": "a", "acknowledged": False}]
        out = sr.mark_acknowledged(rituals, "a")
        self.assertTrue(out[0]["acknowledged"])
        # original not mutated
        self.assertFalse(rituals[0]["acknowledged"])


class KvHelperTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store: dict[str, str] = {}

    def test_round_trip(self) -> None:
        sr.save_rituals(self.store.__setitem__, [{"key": "a"}])
        self.assertEqual(
            sr.load_rituals(self.store.get)[0]["key"], "a",
        )

    def test_garbage_tolerated(self) -> None:
        self.store[sr.SHARED_RITUALS_KEY] = "not json"
        self.assertEqual(sr.load_rituals(self.store.get), [])


if __name__ == "__main__":
    unittest.main()
