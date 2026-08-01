"""The question the cue pool exists to answer: did Aiko actually use it?

Before this, a cue was retired the moment its block rendered, so acting
on one and ignoring one were the same event. These tests are about the
difference: stage A judges her reply, stage B judges what the user says
next, and every path out of ``surfaced`` is bounded so the retry loop
terminates even when the matcher is wrong every single time.

Uses a real :class:`CueStore` on a throwaway file, because the state
machine under test is the store's.
"""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import (
    STATE_AWAITING,
    STATE_EXPIRED,
    STATE_PENDING,
    STATE_USED,
    CueStore,
)
from app.core.session.cue_pool_mixin import CuePoolMixin


class _Host(CuePoolMixin):
    """A SessionController stripped to what the mixin touches."""

    def __init__(self, store: CueStore, *, embedder=None) -> None:
        self._cue_store = store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []
        self._embedder = embedder


class _FakeEmbedder:
    def __init__(self, vec) -> None:
        self._vec = vec
        self.calls = 0

    def embed(self, text: str):
        self.calls += 1
        return self._vec


def _vec(*xs: float) -> np.ndarray:
    arr = np.asarray(xs, dtype=np.float32)
    return arr / float(np.linalg.norm(arr) or 1.0)


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.store = CueStore(ChatDatabase(Path(tmp.name) / "chat.db"))
        self.host = _Host(self.store)

    def _state(self, cue_id: int) -> str:
        rows = self.store.list_for_user()
        return next(r.state for r in rows if r.id == cue_id)

    def _row(self, cue_id: int):
        return next(r for r in self.store.list_for_user() if r.id == cue_id)

    def _surface(self, cue_type: str, subject: str, text: str = "cue") -> int:
        cue_id = self.store.add(cue_type, subject, text)
        row = self.host.take_pool_cue(cue_type)
        self.assertIsNotNone(row)
        self.assertEqual(row.id, cue_id)
        return cue_id


# ── surfacing is not consumption ─────────────────────────────────────────


class SurfacingTests(_Fixture):
    def test_taking_a_cue_marks_it_surfaced_not_used(self) -> None:
        cue_id = self._surface("interest_drift", "film photography")
        self.assertEqual(self._state(cue_id), "surfaced")
        self.assertEqual(self._row(cue_id).surfaced_count, 1)

    def test_a_rejected_candidate_does_not_spend_a_surfacing(self) -> None:
        """Inspecting a cue and deciding against it must be free."""
        cue_id = self.store.add("interest_drift", "film photography", "cue")
        row = self.host.take_pool_cue(
            "interest_drift", relevant=lambda payload: False,
        )
        self.assertIsNone(row)
        self.assertEqual(self._state(cue_id), STATE_PENDING)
        self.assertEqual(self._row(cue_id).surfaced_count, 0)

    def test_force_bypasses_the_provider_gate(self) -> None:
        cue_id = self.store.add("interest_drift", "film photography", "cue")
        row = self.host.take_pool_cue(
            "interest_drift", relevant=lambda payload: False, force=True,
        )
        self.assertEqual(row.id, cue_id)


# ── stage A: fulfilment=spoken ───────────────────────────────────────────


class SpokenTests(_Fixture):
    def test_saying_it_is_the_whole_point(self) -> None:
        cue_id = self._surface("interest_drift", "film photography")
        self.host._settle_pool_cues(
            user_text="what have you been up to",
            assistant_text="I've been weirdly into film photography lately.",
        )
        self.assertEqual(self._state(cue_id), STATE_USED)

    def test_ignoring_it_returns_it_to_the_pool(self) -> None:
        cue_id = self._surface("interest_drift", "film photography")
        self.host._settle_pool_cues(
            user_text="what's for dinner",
            assistant_text="Something with noodles, probably.",
        )
        self.assertEqual(self._state(cue_id), STATE_PENDING)

    def test_the_retry_loop_terminates(self) -> None:
        """max_surfacings is what makes a wrong matcher safe to ship."""
        cue_id = self._surface("interest_drift", "film photography")
        for _ in range(4):
            self.host._settle_pool_cues(
                user_text="unrelated", assistant_text="also unrelated",
            )
            row = self.host.take_pool_cue("interest_drift")
            if row is None:
                break
        self.assertEqual(self._state(cue_id), STATE_EXPIRED)
        self.assertLessEqual(self._row(cue_id).surfaced_count, 2)

    def test_the_subject_is_matched_not_the_cue_sentence(self) -> None:
        """The framing words never appear in her reply; the subject does."""
        cue_id = self._surface(
            "interest_drift",
            "film photography",
            "Heads-up: you've found yourself drawn to film photography.",
        )
        self.host._settle_pool_cues(
            user_text="hey",
            # Shares "photography" with the subject and nothing else with
            # the cue line's framing.
            assistant_text="Been shooting a lot of photography lately.",
        )
        self.assertEqual(self._state(cue_id), STATE_USED)


# ── stage A: cosine, and where it is trusted ─────────────────────────────


class CosineTests(_Fixture):
    def test_a_pivot_with_no_shared_words_counts_for_wander(self) -> None:
        """Off-topic by construction, so a high cosine means she pivoted."""
        vec = _vec(1.0, 0.0, 0.0)
        cue_id = self.store.add(
            "associative_wander", "rust debugging", "cue", embedding=vec,
        )
        self.host.take_pool_cue("associative_wander")
        self.host._settle_pool_cues(
            user_text="hi",
            assistant_text="funny how patience shows up in both",
            reply_vec=vec,
        )
        self.assertEqual(self._state(cue_id), STATE_USED)
        self.assertTrue(self._row(cue_id).used_evidence.startswith("semantic"))

    def test_the_same_cosine_does_not_count_for_an_on_topic_cue(self) -> None:
        """Its subject IS the live topic, so cosine only measures that."""
        vec = _vec(1.0, 0.0, 0.0)
        cue_id = self.store.add(
            "interest_drift", "rust debugging", "cue", embedding=vec,
        )
        self.host.take_pool_cue("interest_drift")
        self.host._settle_pool_cues(
            user_text="hi", assistant_text="mm, sure", reply_vec=vec,
        )
        self.assertEqual(self._state(cue_id), STATE_PENDING)

    def test_a_near_miss_cosine_is_still_recorded(self) -> None:
        """The distribution is the calibration data; discarding it wastes it."""
        cue_id = self.store.add(
            "interest_drift", "rust debugging", "cue",
            embedding=_vec(1.0, 0.0, 0.0),
        )
        self.host.take_pool_cue("interest_drift")
        self.host._settle_pool_cues(
            user_text="hi",
            assistant_text="mm, sure",
            reply_vec=_vec(0.8, 0.6, 0.0),
        )
        evidence = self._row(cue_id).used_evidence
        self.assertTrue(evidence.startswith("none:"))
        self.assertAlmostEqual(float(evidence.split(":")[1]), 0.8, places=1)

    def test_a_wander_matches_the_far_half_of_the_pair(self) -> None:
        cue_id = self.store.add(
            "associative_wander",
            "hiking trails / rust debugging",
            "cue",
            payload={"match_subject": "rust debugging"},
        )
        row = self.host.take_pool_cue("associative_wander")
        row.payload["match_subject"] = "rust debugging"
        self.host._settle_pool_cues(
            user_text="planning a hike",
            # Only the distant half appears. Matching the near half would
            # only have proved she stayed on topic.
            assistant_text="reminds me of debugging, oddly",
        )
        self.assertEqual(self._state(cue_id), STATE_USED)


# ── stage A -> B: fulfilment=answered ────────────────────────────────────


class AnsweredTests(_Fixture):
    def _ask(self, subject: str = "garage band") -> int:
        cue_id = self._surface("dormant_interest", subject)
        self.host._settle_pool_cues(
            user_text="hey",
            assistant_text="whatever happened with the garage band?",
        )
        return cue_id

    def test_asking_is_not_using(self) -> None:
        cue_id = self._ask()
        self.assertEqual(self._state(cue_id), STATE_AWAITING)
        self.assertEqual(self._row(cue_id).ask_count, 1)

    def test_the_answer_settles_it_on_the_next_turn(self) -> None:
        cue_id = self._ask()
        self.host._settle_awaiting_cues(
            user_text="the band fizzled out honestly",
        )
        self.assertEqual(self._state(cue_id), STATE_USED)

    def test_an_answer_about_something_else_leaves_it_alive(self) -> None:
        """She asked and got nothing, so the curiosity is not satisfied."""
        cue_id = self._ask()
        self.host._settle_awaiting_cues(user_text="anyway, work was brutal")
        row = self._row(cue_id)
        self.assertEqual(row.state, STATE_PENDING)
        self.assertIsNotNone(row.not_before)

    def test_the_reask_cooldown_keeps_it_off_the_shelf(self) -> None:
        self._ask()
        self.host._settle_awaiting_cues(user_text="anyway, work was brutal")
        self.assertEqual(self.store.count_pending("dormant_interest"), 0)

    def test_asks_are_bounded_too(self) -> None:
        cue_id = self._ask()
        self.host._settle_awaiting_cues(user_text="anyway, work was brutal")
        # Second ask, still no answer -> out of asks.
        self.store.mark_surfaced(cue_id)
        self.store.mark_asked(cue_id)
        self.host._settle_awaiting_cues(user_text="totally different subject")
        self.assertEqual(self._state(cue_id), STATE_EXPIRED)
        self.assertEqual(self._row(cue_id).used_evidence, "max_asks")

    def test_the_user_message_is_embedded_only_when_it_could_help(
        self,
    ) -> None:
        embedder = _FakeEmbedder(_vec(1.0, 0.0, 0.0))
        self.host._embedder = embedder
        # knowledge_gap_notice is lexical-only, so there is nothing a
        # vector could decide and the round-trip is skipped.
        cue_id = self._surface("knowledge_gap_notice", "type theory")
        self.host._settle_pool_cues(
            user_text="hey", assistant_text="I keep circling type theory.",
        )
        self.assertEqual(self._state(cue_id), STATE_AWAITING)
        self.host._settle_awaiting_cues(user_text="tell me about lunch")
        self.assertEqual(embedder.calls, 0)


class BroadcastTests(_Fixture):
    def test_a_used_cue_reaches_the_listener(self) -> None:
        seen: list[dict] = []
        self.host.add_cue_pool_listener(seen.append)
        self._surface("interest_drift", "film photography")
        self.host._settle_pool_cues(
            user_text="hi", assistant_text="into film photography lately",
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["state"], "used")
        self.assertEqual(seen[0]["subject"], "film photography")

    def test_an_ignored_cue_is_not_broadcast(self) -> None:
        seen: list[dict] = []
        self.host.add_cue_pool_listener(seen.append)
        self._surface("interest_drift", "film photography")
        self.host._settle_pool_cues(user_text="hi", assistant_text="mm")
        self.assertEqual(seen, [])


class SurfaceTimeLedgerTests(_Fixture):
    """Cues whose content is chosen when the block renders.

    The gap family has no worker to draft ahead: the arming event is a
    duration, and which reflection or journal beat gets used depends on
    the message being answered. So the row is written at the moment it
    surfaces -- and from there it is an ordinary pool cue, which is the
    whole point of doing it this way.
    """

    # A reflection, which is what this cue's subject actually is -- a
    # sentence rather than a topic label, which is why its policy asks
    # for three shared content words instead of one.
    _SUBJECT = "whether the interview prep is leaving him any sleep"
    _TEXT = "Turning over: you've been thinking about this -- '...'"

    def _record(self):
        row = self.host.record_surfaced_cue(
            "turning_over", self._SUBJECT, self._TEXT,
        )
        self.assertIsNotNone(row)
        return row

    def test_recording_lands_the_row_already_surfaced(self) -> None:
        row = self._record()
        self.assertEqual(self._state(row.id), "surfaced")
        self.assertEqual(self._row(row.id).surfaced_count, 1)

    def test_a_miss_comes_back_for_the_next_render(self) -> None:
        """The leak this closes: a one-shot slot spent on a turn she ignored."""
        row = self._record()
        self.host._settle_pool_cues(
            user_text="what's for dinner", assistant_text="pasta, probably",
        )
        self.assertEqual(self._state(row.id), STATE_PENDING)

        again = self.host.take_pool_cue("turning_over")
        self.assertIsNotNone(again)
        self.assertEqual(again.id, row.id)
        self.assertEqual(again.text, self._TEXT)

    def test_using_it_retires_it(self) -> None:
        row = self._record()
        self.host._settle_pool_cues(
            user_text="hey",
            assistant_text=(
                "honestly I kept wondering whether the interview prep was "
                "leaving you any sleep"
            ),
        )
        self.assertEqual(self._state(row.id), STATE_USED)
        self.assertIsNone(self.host.take_pool_cue("turning_over"))

    def test_the_retry_budget_still_applies(self) -> None:
        row = self._record()
        for _ in range(3):
            self.host._settle_pool_cues(user_text="hi", assistant_text="mm")
            self.host.take_pool_cue("turning_over")
        self.assertEqual(self._state(row.id), STATE_EXPIRED)

    def test_recording_without_a_store_is_a_no_op(self) -> None:
        self.assertIsNone(
            _Host(None).record_surfaced_cue("turning_over", "x", "y"),
        )


class SurfacingCadenceTests(_Fixture):
    """``surface_cooldown_hours``: how often she may open a *new* one.

    The types that carry it are rare by nature rather than by scarcity,
    and their rarity used to be an accident of production -- a worker
    that drafted one cue a fortnight surfaced one a fortnight.
    Deficit-driven scheduling keeps the shelf stocked, so the cadence had
    to move to where it is actually about the reader.

    ``self_callback`` is the type under test because it is the one with a
    non-zero cooldown; the rest have no cadence and must be unaffected.
    """

    def _queue(self, subject: str) -> int:
        return self.store.add("self_callback", subject, f"cue about {subject}")

    def test_a_second_new_cue_waits_out_the_window(self) -> None:
        self._queue("the restless stretch back in spring")
        self.assertIsNotNone(self.host.take_pool_cue("self_callback"))
        self._queue("wanting to get back into astronomy")
        self.assertIsNone(self.host.take_pool_cue("self_callback"))

    def test_a_retry_is_never_delayed_by_the_cadence(self) -> None:
        """An in-flight cue is unfinished business, not a new one."""
        cue_id = self._queue("the restless stretch back in spring")
        self.host.take_pool_cue("self_callback")
        self.host._settle_pool_cues(user_text="hi", assistant_text="mm")
        self.assertEqual(self._state(cue_id), STATE_PENDING)

        again = self.host.take_pool_cue("self_callback")
        self.assertIsNotNone(again)
        self.assertEqual(again.id, cue_id)

    def test_a_retry_is_reachable_from_behind_a_blocked_new_cue(self) -> None:
        """The reason the gate filters candidates instead of the winner.

        ``pending`` sorts unseen cues first, so the fresh row is the one
        offered up -- and rejecting it after the fact would hide the
        retry sitting directly behind it.
        """
        retried = self._queue("the restless stretch back in spring")
        self.host.take_pool_cue("self_callback")
        self.host._settle_pool_cues(user_text="hi", assistant_text="mm")
        fresh = self._queue("wanting to get back into astronomy")

        claimed = self.host.take_pool_cue("self_callback")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, retried)
        self.assertEqual(self._state(fresh), STATE_PENDING)

    def test_a_used_cue_still_counts_as_a_recent_surfacing(self) -> None:
        """Landing one is the strongest reason not to open another."""
        self._queue("the restless stretch back in spring")
        self.host.take_pool_cue("self_callback")
        self.host._settle_pool_cues(
            user_text="hey",
            assistant_text=(
                "funny, I told you about that restless stretch back in "
                "spring -- it's settled down"
            ),
        )
        self._queue("wanting to get back into astronomy")
        self.assertIsNone(self.host.take_pool_cue("self_callback"))

    def test_force_ignores_the_cadence(self) -> None:
        self._queue("the restless stretch back in spring")
        self.host.take_pool_cue("self_callback")
        self._queue("wanting to get back into astronomy")
        self.assertIsNotNone(
            self.host.take_pool_cue("self_callback", force=True),
        )

    def test_a_type_without_a_cadence_is_untouched(self) -> None:
        self.store.add("interest_drift", "film photography", "cue")
        self.store.add("interest_drift", "bread", "cue")
        self.assertIsNotNone(self.host.take_pool_cue("interest_drift"))
        self.assertIsNotNone(self.host.take_pool_cue("interest_drift"))


class SelfCorrectionPoolTests(_Fixture):
    """K38's owed correction, now that it outlives the process."""

    _SUBJECT = "I really love hiking in the mountains."

    def test_an_owed_correction_survives_a_restart(self) -> None:
        """The concrete thing the in-memory slot could not do.

        A crash or a restart between the slip and the next turn used to
        drop the correction silently, leaving Aiko's wrong statement
        standing as the last word on it.
        """
        self.store.add("self_correction", self._SUBJECT, "Heads-up: ...")
        restarted = _Host(self.store)
        row = restarted.take_pool_cue("self_correction")
        self.assertIsNotNone(row)
        self.assertEqual(row.text, "Heads-up: ...")

    def test_owning_the_correction_retires_it(self) -> None:
        cue_id = self._surface(
            "self_correction", self._SUBJECT, "Heads-up: ...",
        )
        self.host._settle_pool_cues(
            user_text="wait, really?",
            assistant_text=(
                "oh hang on, I had that backwards -- I love hiking in the "
                "mountains, I don't hate it"
            ),
        )
        self.assertEqual(self._state(cue_id), STATE_USED)

    def test_reading_past_it_gets_one_more_turn_then_stops(self) -> None:
        cue_id = self._surface(
            "self_correction", self._SUBJECT, "Heads-up: ...",
        )
        self.host._settle_pool_cues(user_text="ok", assistant_text="anyway")
        self.assertEqual(self._state(cue_id), STATE_PENDING)
        self.host.take_pool_cue("self_correction")
        self.host._settle_pool_cues(user_text="ok", assistant_text="anyway")
        self.assertEqual(self._state(cue_id), STATE_EXPIRED)


class NoStoreTests(unittest.TestCase):
    """A session without a pool must behave as it did before one existed."""

    def test_every_entry_point_is_a_no_op(self) -> None:
        host = _Host(None)
        self.assertIsNone(host.take_pool_cue("interest_drift"))
        self.assertIsNone(host.record_surfaced_cue("turning_over", "a", "b"))
        self.assertFalse(host._queue_pool_cue("self_correction", "a", "b"))
        host._settle_pool_cues(user_text="a", assistant_text="b")
        host._settle_awaiting_cues(user_text="a")


if __name__ == "__main__":
    unittest.main()
