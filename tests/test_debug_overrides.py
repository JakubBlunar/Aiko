"""Unit tests for the one-shot debug override registry.

The behaviours worth pinning are the ones the ad-hoc attributes got wrong:
overrides must fire exactly once, must all disappear together on a session
switch, and must reject a name nobody consumes instead of silently accepting
it. The last test class is the regression the registry exists for.
"""
from __future__ import annotations

import threading
import unittest

from app.core.session.debug_overrides import (
    KNOWN_OVERRIDES,
    DebugOverrides,
    UnknownOverride,
)


class ArmAndTakeTests(unittest.TestCase):
    def test_take_returns_the_payload_then_disarms(self) -> None:
        overrides = DebugOverrides()
        overrides.arm("turning_over_force_next")
        self.assertTrue(overrides.take("turning_over_force_next"))
        self.assertIsNone(overrides.take("turning_over_force_next"))

    def test_take_on_an_unarmed_name_returns_the_default(self) -> None:
        overrides = DebugOverrides()
        self.assertIsNone(overrides.take("turning_over_force_next"))
        self.assertFalse(overrides.take("turning_over_force_next", False))

    def test_payload_round_trips(self) -> None:
        """The payload-carrying overrides are the value, not a flag."""
        overrides = DebugOverrides()
        overrides.arm("day_color_force_next", "amber")
        overrides.arm("question_balance_suppress_remaining", 3)
        self.assertEqual(overrides.take("day_color_force_next"), "amber")
        self.assertEqual(overrides.take("question_balance_suppress_remaining"), 3)

    def test_a_falsy_payload_still_counts_as_armed(self) -> None:
        """"Suppress for 0 turns" is an instruction, not an absent override.

        A consumer that tested the payload's truthiness rather than asking
        whether it was armed would silently ignore it.
        """
        overrides = DebugOverrides()
        overrides.arm("question_balance_suppress_remaining", 0)
        self.assertTrue(overrides.is_armed("question_balance_suppress_remaining"))
        self.assertEqual(
            overrides.take("question_balance_suppress_remaining", -1), 0,
        )

    def test_arming_twice_keeps_the_last_payload(self) -> None:
        overrides = DebugOverrides()
        overrides.arm("day_color_force_next", "amber")
        overrides.arm("day_color_force_next", "violet")
        self.assertEqual(overrides.take("day_color_force_next"), "violet")

    def test_peek_leaves_it_armed(self) -> None:
        overrides = DebugOverrides()
        overrides.arm("mood_inertia_force")
        self.assertTrue(overrides.peek("mood_inertia_force"))
        self.assertTrue(overrides.is_armed("mood_inertia_force"))
        self.assertTrue(overrides.take("mood_inertia_force"))

    def test_disarm_is_forgiving(self) -> None:
        overrides = DebugOverrides()
        overrides.disarm("mood_inertia_force")  # never armed; must not raise
        overrides.arm("mood_inertia_force")
        overrides.disarm("mood_inertia_force")
        self.assertFalse(overrides.is_armed("mood_inertia_force"))


class UnknownNameTests(unittest.TestCase):
    def test_arming_an_unregistered_name_raises(self) -> None:
        """The failure this replaces was silent: a dead attribute nobody read."""
        overrides = DebugOverrides()
        with self.assertRaises(UnknownOverride):
            overrides.arm("turnign_over_force_next")

    def test_the_error_names_the_offender_and_where_to_fix_it(self) -> None:
        overrides = DebugOverrides()
        with self.assertRaises(UnknownOverride) as caught:
            overrides.arm("nope")
        self.assertEqual(caught.exception.name, "nope")
        self.assertIn("KNOWN_OVERRIDES", str(caught.exception))

    def test_reading_an_unregistered_name_is_merely_empty(self) -> None:
        """Reads stay lenient; only arming is a programming error."""
        overrides = DebugOverrides()
        self.assertIsNone(overrides.take("nope"))
        self.assertIsNone(overrides.peek("nope"))
        self.assertFalse(overrides.is_armed("nope"))


class ClearTests(unittest.TestCase):
    """The regression the registry exists for.

    A session switch used to clear 11 of the 43 flags and a memory wipe 14,
    from two hand-written lists that had drifted apart by three names. Anything
    they missed stayed armed and fired later in an unrelated conversation.
    """

    def test_clear_drops_every_override(self) -> None:
        overrides = DebugOverrides()
        for name in KNOWN_OVERRIDES:
            overrides.arm(name)
        self.assertEqual(len(overrides), len(KNOWN_OVERRIDES))

        dropped = overrides.clear()

        self.assertEqual(dropped, len(KNOWN_OVERRIDES))
        self.assertEqual(len(overrides), 0)
        for name in KNOWN_OVERRIDES:
            with self.subTest(name=name):
                self.assertIsNone(overrides.take(name))

    def test_clear_on_an_empty_registry_is_a_no_op(self) -> None:
        self.assertEqual(DebugOverrides().clear(), 0)

    def test_nothing_can_be_forgotten_by_construction(self) -> None:
        """No list to maintain: a new override is covered the day it is added.

        This is the structural property, as opposed to the test above which
        checks today's names. Adding a name to KNOWN_OVERRIDES cannot fail to
        be cleared, because clear() does not enumerate names at all.
        """
        overrides = DebugOverrides()
        overrides.arm("day_color_force_next", "amber")
        overrides.arm("question_balance_suppress_remaining", 5)
        overrides.clear()
        self.assertEqual(overrides.snapshot(), {})


class SnapshotTests(unittest.TestCase):
    def test_snapshot_reports_names_and_payloads(self) -> None:
        overrides = DebugOverrides()
        overrides.arm("turning_over_force_next")
        overrides.arm("day_color_force_next", "amber")
        self.assertEqual(
            overrides.snapshot(),
            {"turning_over_force_next": True, "day_color_force_next": "amber"},
        )

    def test_snapshot_is_a_copy(self) -> None:
        overrides = DebugOverrides()
        overrides.arm("turning_over_force_next")
        overrides.snapshot()["turning_over_force_next"] = "tampered"
        self.assertTrue(overrides.take("turning_over_force_next") is True)


class RegistryContentTests(unittest.TestCase):
    def test_every_name_has_a_description(self) -> None:
        for name, description in KNOWN_OVERRIDES.items():
            with self.subTest(name=name):
                self.assertTrue(description.strip(), f"{name} has no description")

    def test_names_are_bare_not_private_attributes(self) -> None:
        """They are registry keys now, not attributes on the controller."""
        for name in KNOWN_OVERRIDES:
            with self.subTest(name=name):
                self.assertFalse(name.startswith("_"))

    def test_the_registry_is_immutable(self) -> None:
        with self.assertRaises(TypeError):
            KNOWN_OVERRIDES["sneaky"] = "nope"  # type: ignore[index]


class ConcurrencyTests(unittest.TestCase):
    def test_only_one_thread_can_take_an_override(self) -> None:
        """"One-shot" has to hold when two providers race for the same cue."""
        overrides = DebugOverrides()
        overrides.arm("turning_over_force_next")
        start = threading.Barrier(8)
        wins: list[object] = []
        lock = threading.Lock()

        def contend() -> None:
            start.wait()
            got = overrides.take("turning_over_force_next")
            if got is not None:
                with lock:
                    wins.append(got)

        threads = [threading.Thread(target=contend) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        self.assertEqual(len(wins), 1)

    def test_clear_during_contention_does_not_deadlock(self) -> None:
        overrides = DebugOverrides()
        stop = threading.Event()

        def arm_and_take() -> None:
            while not stop.is_set():
                overrides.arm("mood_inertia_force")
                overrides.take("mood_inertia_force")

        workers = [threading.Thread(target=arm_and_take) for _ in range(4)]
        for worker in workers:
            worker.start()
        for _ in range(200):
            overrides.clear()
            overrides.snapshot()
        stop.set()
        for worker in workers:
            worker.join(timeout=5)
            self.assertFalse(worker.is_alive())


if __name__ == "__main__":
    unittest.main()
