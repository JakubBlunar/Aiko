"""Tests for K43 — promise lifecycle + follow-through worker.

Three pinned contracts:

1. :mod:`app.core.memory.promise_lifecycle` — pure helpers: status
   defaulting (legacy rows read as ``open``), sidedness via metadata
   with the legacy content-prefix fallback, and the lexical fulfilment
   matcher.
2. :class:`PromiseFollowthroughWorker` — arming picks the oldest open
   assistant promise past the age gate, stamps it ``surfaced``, writes
   the one-shot kv pending slot, respects cooldown / pending / disabled
   gates, and ages out stale promises to ``dropped``.
3. ``demand()`` — the P36 admission signal: full pressure only when the
   pending slot is free and the arm cooldown is spent, with ``is_ready``
   reduced to the enable veto.
4. ``force_arm`` (the MCP path) bypasses age + cooldown gates.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone

from app.core.memory import promise_lifecycle as lifecycle
from app.core.proactive.promise_followthrough_worker import (
    PENDING_KEY,
    PromiseFollowthroughWorker,
    clear_pending,
    load_pending,
)


def _iso_ago(hours: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(hours=hours)
    ).isoformat()


class _FakeMemory:
    def __init__(
        self,
        mid: int,
        content: str,
        *,
        kind: str = "promise",
        metadata: dict | None = None,
        created_at: str | None = None,
    ) -> None:
        self.id = mid
        self.content = content
        self.kind = kind
        self.metadata = metadata or {}
        self.created_at = created_at or _iso_ago(8.0)


class _FakeMemoryStore:
    def __init__(self, memories: list[_FakeMemory] | None = None) -> None:
        self.memories = memories or []
        self.update_calls: list[tuple[int, dict]] = []

    def iter_by_kind(self, kind: str) -> list[_FakeMemory]:
        return [m for m in self.memories if m.kind == kind]

    def get(self, memory_id: int) -> _FakeMemory | None:
        for m in self.memories:
            if m.id == int(memory_id):
                return m
        return None

    def update(self, memory_id, *, metadata=None, metadata_merge=False, **kw):
        self.update_calls.append((memory_id, dict(metadata or {})))
        mem = self.get(memory_id)
        if mem is not None and metadata:
            if metadata_merge:
                mem.metadata.update(metadata)
            else:
                mem.metadata = dict(metadata)


class _FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


def _make_worker(
    store: _FakeMemoryStore,
    kv: _FakeKv,
    *,
    enabled: bool = True,
    min_age_hours: float = 4.0,
    cooldown_hours: float = 6.0,
    drop_after_days: float = 14.0,
) -> PromiseFollowthroughWorker:
    return PromiseFollowthroughWorker(
        memory_store=store,
        kv_get=kv.get,
        kv_set=kv.set,
        enabled_provider=lambda: enabled,
        min_age_hours=min_age_hours,
        cooldown_hours=cooldown_hours,
        drop_after_days=drop_after_days,
    )


# ── lifecycle helpers ────────────────────────────────────────────────────


class PromiseStatusTests(unittest.TestCase):
    def test_missing_metadata_reads_open(self) -> None:
        mem = _FakeMemory(1, "Aiko promised: check the docs")
        self.assertEqual(lifecycle.promise_status(mem), "open")

    def test_explicit_statuses_round_trip(self) -> None:
        for status in ("open", "surfaced", "fulfilled", "dropped"):
            mem = _FakeMemory(
                1, "x", metadata={"promise_status": status},
            )
            self.assertEqual(lifecycle.promise_status(mem), status)

    def test_garbage_status_reads_open(self) -> None:
        mem = _FakeMemory(1, "x", metadata={"promise_status": "banana"})
        self.assertEqual(lifecycle.promise_status(mem), "open")


class SidednessTests(unittest.TestCase):
    def test_metadata_stamp_wins(self) -> None:
        mem = _FakeMemory(
            1,
            "Jacob promised: call mom",
            metadata={"promise_who": "assistant"},
        )
        self.assertTrue(lifecycle.is_assistant_promise(mem))

    def test_metadata_user_side(self) -> None:
        mem = _FakeMemory(
            1, "Aiko promised: x", metadata={"promise_who": "user"},
        )
        self.assertFalse(lifecycle.is_assistant_promise(mem))

    def test_legacy_prefix_fallback(self) -> None:
        aiko = _FakeMemory(1, "Aiko promised: look into LanceDB indexing")
        user = _FakeMemory(2, "Jacob promised: call his mom")
        self.assertTrue(lifecycle.is_assistant_promise(aiko))
        self.assertFalse(lifecycle.is_assistant_promise(user))


class PromiseWhatTests(unittest.TestCase):
    def test_strips_actor_prefix(self) -> None:
        mem = _FakeMemory(1, "Aiko promised: look into LanceDB indexing")
        self.assertEqual(
            lifecycle.promise_what(mem), "look into LanceDB indexing",
        )

    def test_no_prefix_returns_content(self) -> None:
        mem = _FakeMemory(1, "look into LanceDB indexing")
        self.assertEqual(
            lifecycle.promise_what(mem), "look into LanceDB indexing",
        )

    def test_strips_the_deadline_suffix(self) -> None:
        # The cue states the timing itself, from the parsed deadline, so
        # leaving the suffix in read the storage format out loud: "you'd
        # call the dentist (by Wed Aug 19)".
        mem = _FakeMemory(
            1, "Jacob promised: call the dentist (by Wed Aug 19)",
        )
        self.assertEqual(
            lifecycle.promise_what(mem), "call the dentist",
        )

    def test_keeps_a_bracketed_aside_that_is_not_a_deadline(self) -> None:
        mem = _FakeMemory(
            1, "Aiko promised: check the logs (the noisy ones) tonight",
        )
        self.assertEqual(
            lifecycle.promise_what(mem),
            "check the logs (the noisy ones) tonight",
        )


class AgeHelpersTests(unittest.TestCase):
    def test_age_hours(self) -> None:
        mem = _FakeMemory(1, "x", created_at=_iso_ago(10.0))
        age = lifecycle.promise_age_hours(mem)
        self.assertIsNotNone(age)
        self.assertAlmostEqual(age, 10.0, delta=0.1)

    def test_bad_timestamp_returns_none(self) -> None:
        mem = _FakeMemory(1, "x", created_at="not-a-date")
        self.assertIsNone(lifecycle.promise_age_hours(mem))

    def test_humanize_bands(self) -> None:
        self.assertEqual(lifecycle.humanize_age(3.0), "earlier today")
        self.assertEqual(lifecycle.humanize_age(15.0), "yesterday")
        self.assertEqual(lifecycle.humanize_age(72.0), "3 days ago")
        self.assertEqual(lifecycle.humanize_age(7 * 24.0), "a week ago")
        self.assertIn("weeks ago", lifecycle.humanize_age(20 * 24.0))


class DeadlineHelpersTests(unittest.TestCase):
    """Late and old are different questions (H41).

    Everything used to be decided from creation age, so a promise made
    this morning and due by lunch read as fresh all afternoon while a
    standing commitment with no deadline read as late for having been
    made a while back.
    """

    def test_no_deadline_is_never_overdue(self) -> None:
        mem = _FakeMemory(1, "Aiko promised: help when asked",
                          created_at=_iso_ago(30 * 24.0))
        self.assertIsNone(lifecycle.promise_deadline(mem))
        self.assertIsNone(lifecycle.overdue_hours(mem))
        self.assertFalse(lifecycle.is_overdue(mem))

    def test_a_passed_deadline_is_overdue_however_young_the_promise(self) -> None:
        mem = _FakeMemory(
            1,
            "Jacob promised: call the dentist",
            created_at=_iso_ago(3.0),
            metadata={"promise_deadline": _iso_ago(1.0)},
        )
        late = lifecycle.overdue_hours(mem)
        assert late is not None
        self.assertAlmostEqual(late, 1.0, delta=0.1)
        self.assertTrue(lifecycle.is_overdue(mem))

    def test_a_future_deadline_is_not_overdue_however_old_the_promise(self) -> None:
        mem = _FakeMemory(
            1,
            "Jacob promised: book the flight",
            created_at=_iso_ago(20 * 24.0),
            metadata={
                "promise_deadline": (
                    datetime.now(timezone.utc) + timedelta(days=2)
                ).isoformat(),
            },
        )
        self.assertFalse(lifecycle.is_overdue(mem))

    def test_an_unreadable_deadline_is_treated_as_absent(self) -> None:
        mem = _FakeMemory(
            1, "x", metadata={"promise_deadline": "next Tuesday-ish"},
        )
        self.assertIsNone(lifecycle.promise_deadline(mem))
        self.assertFalse(lifecycle.is_overdue(mem))


class FindFulfilledTests(unittest.TestCase):
    def test_overlap_match_fulfils(self) -> None:
        mem = _FakeMemory(
            1, "Aiko promised: look into LanceDB vector indexing options",
        )
        hits = lifecycle.find_fulfilled(
            [mem],
            "so I dug into LanceDB — for vector indexing the options are "
            "IVF_PQ and HNSW",
            min_overlap=3,
        )
        self.assertEqual([m.id for m in hits], [1])

    def test_unrelated_reply_no_match(self) -> None:
        mem = _FakeMemory(
            1, "Aiko promised: look into LanceDB vector indexing options",
        )
        hits = lifecycle.find_fulfilled(
            [mem], "anyway, how was your day at the gym?", min_overlap=3,
        )
        self.assertEqual(hits, [])

    def test_terminal_statuses_skipped(self) -> None:
        for status in ("fulfilled", "dropped"):
            mem = _FakeMemory(
                1,
                "Aiko promised: look into LanceDB vector indexing",
                metadata={"promise_status": status},
            )
            hits = lifecycle.find_fulfilled(
                [mem], "LanceDB vector indexing is neat", min_overlap=3,
            )
            self.assertEqual(hits, [], status)

    def test_surfaced_still_fulfillable(self) -> None:
        mem = _FakeMemory(
            1,
            "Aiko promised: look into LanceDB vector indexing",
            metadata={"promise_status": "surfaced"},
        )
        hits = lifecycle.find_fulfilled(
            [mem], "LanceDB vector indexing turns out great", min_overlap=3,
        )
        self.assertEqual([m.id for m in hits], [1])

    def test_user_promises_ignored(self) -> None:
        mem = _FakeMemory(1, "Jacob promised: look into LanceDB indexing")
        hits = lifecycle.find_fulfilled(
            [mem], "LanceDB indexing looks fine", min_overlap=2,
        )
        self.assertEqual(hits, [])

    def test_short_body_needs_all_words(self) -> None:
        # Body has only 2 content words -> both must appear even though
        # min_overlap is 3.
        mem = _FakeMemory(1, "Aiko promised: check espresso")
        self.assertEqual(
            [m.id for m in lifecycle.find_fulfilled(
                [mem], "I did check that espresso thing", min_overlap=3,
            )],
            [1],
        )
        self.assertEqual(
            lifecycle.find_fulfilled(
                [mem], "I did check the weather", min_overlap=3,
            ),
            [],
        )


# ── worker ───────────────────────────────────────────────────────────────


class WorkerArmingTests(unittest.TestCase):
    def test_arms_oldest_open_assistant_promise(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                created_at=_iso_ago(8.0),
            ),
            _FakeMemory(
                2, "Aiko promised: get back to you about the gpu",
                created_at=_iso_ago(30.0),
            ),
            _FakeMemory(
                3, "Jacob promised: call his mom",
                created_at=_iso_ago(50.0),
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv).run()
        self.assertEqual(result["armed"], 1)
        pending = load_pending(kv.get)
        self.assertIsNotNone(pending)
        self.assertEqual(pending["memory_id"], 2)  # oldest assistant row
        self.assertIn("gpu", pending["what"])
        # Row stamped surfaced + watermark written.
        self.assertEqual(lifecycle.promise_status(store.get(2)), "surfaced")
        self.assertTrue(kv.get("promise_followthrough.last_fired_at"))

    def test_age_gate_skips_young_promises(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                created_at=_iso_ago(1.0),
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv, min_age_hours=4.0).run()
        self.assertEqual(result["armed"], 0)
        self.assertIsNone(load_pending(kv.get))

    def test_cooldown_blocks_back_to_back_fires(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(1, "Aiko promised: look into LanceDB"),
        ])
        kv = _FakeKv()
        kv.set(
            "promise_followthrough.last_fired_at",
            datetime.now(timezone.utc).isoformat(),
        )
        result = _make_worker(store, kv, cooldown_hours=6.0).run()
        self.assertTrue(result.get("skipped_cooldown"))
        self.assertIsNone(load_pending(kv.get))

    def test_existing_pending_slot_blocks(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(1, "Aiko promised: look into LanceDB"),
        ])
        kv = _FakeKv()
        kv.set(PENDING_KEY, json.dumps({"memory_id": 99, "what": "x"}))
        result = _make_worker(store, kv).run()
        self.assertTrue(result.get("skipped_pending"))

    def test_stale_promises_flip_to_dropped(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                created_at=_iso_ago(20 * 24.0),
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv, drop_after_days=14.0).run()
        self.assertEqual(result["armed"], 0)
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(lifecycle.promise_status(store.get(1)), "dropped")
        self.assertIn(
            "promise_resolved_at", store.get(1).metadata,
        )

    def test_disabled_short_circuits(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(1, "Aiko promised: look into LanceDB"),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv, enabled=False).run()
        self.assertTrue(result.get("skipped_disabled"))
        self.assertIsNone(load_pending(kv.get))

    def test_surfaced_rows_not_rearmed_by_run(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                metadata={"promise_status": "surfaced"},
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv).run()
        self.assertEqual(result["armed"], 0)


class OverdueArmingTests(unittest.TestCase):
    """A missed deadline is the most interesting promise there is (H41)."""

    def test_a_missed_deadline_outranks_a_merely_older_promise(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB indexing",
                created_at=_iso_ago(60.0),
            ),
            _FakeMemory(
                2, "Aiko promised: send the hardware build notes",
                created_at=_iso_ago(9.0),
                metadata={"promise_deadline": _iso_ago(2.0)},
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv).run()
        self.assertEqual(result["armed"], 1)
        pending = load_pending(kv.get)
        assert pending is not None
        # Ordering on age alone would have taken row 1, leaving the only
        # promise with a definite broken obligation unmentioned.
        self.assertEqual(pending["memory_id"], 2)
        self.assertGreater(pending["overdue_hours"], 0.0)

    def test_overdue_bypasses_the_settling_period(self) -> None:
        # min_age_hours exists so she doesn't ask about something she said
        # twenty minutes ago. A promise already late by its own terms has
        # nothing to gain from waiting the period out.
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: check the logs before bed",
                created_at=_iso_ago(1.0),
                metadata={"promise_deadline": _iso_ago(0.5)},
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv, min_age_hours=4.0).run()
        self.assertEqual(result["armed"], 1)

    def test_a_promise_with_no_deadline_carries_no_overdue_claim(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB indexing",
                created_at=_iso_ago(9.0),
            ),
        ])
        kv = _FakeKv()
        _make_worker(store, kv).run()
        pending = load_pending(kv.get)
        assert pending is not None
        self.assertNotIn("overdue_hours", pending)


class RetirementTests(unittest.TestCase):
    """Who ages out, and on which clock (H41).

    Retirement used to be assistant-only, on creation age, with no notion
    of a deadline. That left 86 user-side promises permanently ``open`` --
    the oldest 86 days -- because this module documented them as the
    follow-up worker's job and that worker selects on a field promises
    never carry.
    """

    def test_a_stale_user_promise_is_retired(self) -> None:
        # 84 days is not a round number: it is the age of the oldest row
        # still sitting `open` on the live store when this was found.
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Jacob promised: water the plants",
                created_at=_iso_ago(84 * 24.0),
                metadata={"promise_who": "user", "promise_status": "open"},
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv, drop_after_days=14.0).run()
        self.assertEqual(result["dropped"], 1)
        self.assertEqual(lifecycle.promise_status(store.get(1)), "dropped")

    def test_a_stale_user_promise_is_retired_but_never_surfaced(self) -> None:
        # Retiring his promises is upkeep; raising them is nagging, and
        # that stays out of scope.
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Jacob promised: call the dentist about the filling",
                created_at=_iso_ago(50.0),
                metadata={"promise_who": "user", "promise_status": "open"},
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv).run()
        self.assertEqual(result["armed"], 0)
        self.assertEqual(lifecycle.promise_status(store.get(1)), "open")

    def test_a_deadline_still_ahead_protects_an_old_promise(self) -> None:
        # A commitment agreed three weeks ahead of the day it falls due:
        # the old rule dropped it for being old on that very day.
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Jacob promised: book the flight",
                created_at=_iso_ago(20 * 24.0),
                metadata={
                    "promise_who": "user",
                    "promise_deadline": (
                        datetime.now(timezone.utc) + timedelta(days=1)
                    ).isoformat(),
                },
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv, drop_after_days=14.0).run()
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(lifecycle.promise_status(store.get(1)), "open")

    def test_an_overdue_promise_keeps_a_window_of_its_own(self) -> None:
        # Grace runs from the deadline, not from creation, so a promise
        # that fell due yesterday is not retired for having been made a
        # month ago -- it stays visible while it is worth noticing.
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: send the recap",
                created_at=_iso_ago(40 * 24.0),
                metadata={"promise_deadline": _iso_ago(24.0)},
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv, drop_after_days=14.0).run()
        self.assertEqual(result["dropped"], 0)
        self.assertEqual(result["armed"], 1)

    def test_an_overdue_promise_eventually_runs_out_of_road(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: send the recap",
                created_at=_iso_ago(40 * 24.0),
                metadata={"promise_deadline": _iso_ago(20 * 24.0)},
            ),
        ])
        kv = _FakeKv()
        result = _make_worker(store, kv, drop_after_days=14.0).run()
        self.assertEqual(result["dropped"], 1)


class WorkerDemandTests(unittest.TestCase):
    """P36 admission: two kv reads instead of a fixed interval."""

    def _store(self) -> "_FakeMemoryStore":
        return _FakeMemoryStore([
            _FakeMemory(1, "Aiko promised: look into LanceDB"),
        ])

    def test_a_free_slot_is_full_pressure(self) -> None:
        signal = _make_worker(self._store(), _FakeKv()).demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 1.0)
        self.assertFalse(signal.needs_llm)

    def test_a_waiting_cue_drops_pressure_to_zero(self) -> None:
        kv = _FakeKv()
        kv.set(PENDING_KEY, json.dumps({"memory_id": 99, "what": "x"}))
        signal = _make_worker(self._store(), kv).demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "cue already waiting")

    def test_an_unspent_cooldown_drops_pressure_to_zero(self) -> None:
        now = datetime.now(timezone.utc)
        kv = _FakeKv()
        kv.set(
            "promise_followthrough.last_fired_at",
            (now - timedelta(hours=1)).isoformat(),
        )
        signal = _make_worker(self._store(), kv, cooldown_hours=6.0).demand(
            now=now, last_run_at=None,
        )
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "cooling down")

    def test_a_spent_cooldown_lets_it_back_in(self) -> None:
        now = datetime.now(timezone.utc)
        kv = _FakeKv()
        kv.set(
            "promise_followthrough.last_fired_at",
            (now - timedelta(hours=9)).isoformat(),
        )
        signal = _make_worker(self._store(), kv, cooldown_hours=6.0).demand(
            now=now, last_run_at=None,
        )
        self.assertEqual(signal.pressure, 1.0)

    def test_disabled_reports_no_pressure(self) -> None:
        signal = _make_worker(self._store(), _FakeKv(), enabled=False).demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "disabled")

    def test_nothing_owed_reports_nothing(self) -> None:
        """The P44 regression: pressure meant "nothing is blocking me".

        The old probe returned ``1.0, "slot free"`` whenever the two kv
        gates were clear — both true almost always — and then the scan
        found nothing to arm, 21 runs out of 21. It answered *am I
        allowed to run* rather than *is there work*.
        """
        signal = _make_worker(_FakeMemoryStore([]), _FakeKv()).demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "nothing owed")

    def test_a_promise_too_young_to_ask_about_is_not_work(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                created_at=_iso_ago(0.1),
            ),
        ])
        signal = _make_worker(store, _FakeKv(), min_age_hours=4.0).demand(
            now=datetime.now(timezone.utc), last_run_at=None,
        )
        self.assertEqual(signal.pressure, 0.0)

    def test_is_ready_vetoes_every_guaranteed_no_op(self) -> None:
        """Each gate makes the run a no-op, so each is a hard veto.

        The heartbeat is checked before pressure, so leaving these as
        ``pressure=0.0`` would only deprioritise the worker — it would
        still wake on every interval to find nothing it may do.
        """
        now = datetime.now(timezone.utc)
        store = self._store()
        self.assertTrue(
            _make_worker(store, _FakeKv()).is_ready(now=now, last_run_at=None)
        )

        cooling = _FakeKv()
        cooling.set("promise_followthrough.last_fired_at", now.isoformat())
        self.assertFalse(
            _make_worker(store, cooling, cooldown_hours=6.0).is_ready(
                now=now, last_run_at=None,
            )
        )

        waiting = _FakeKv()
        waiting.set(PENDING_KEY, json.dumps({"memory_id": 99, "what": "x"}))
        self.assertFalse(
            _make_worker(store, waiting).is_ready(now=now, last_run_at=None)
        )

        self.assertFalse(
            _make_worker(_FakeMemoryStore([]), _FakeKv()).is_ready(
                now=now, last_run_at=None,
            )
        )
        self.assertFalse(
            _make_worker(store, _FakeKv(), enabled=False).is_ready(
                now=now, last_run_at=None,
            )
        )

    def test_an_overdue_promise_is_ready_but_not_pressure(self) -> None:
        """Retiring a dropped promise is bookkeeping with no deadline."""
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                created_at=_iso_ago(24 * 30),  # past drop_after_days
            ),
        ])
        worker = _make_worker(store, _FakeKv(), drop_after_days=14.0)
        now = datetime.now(timezone.utc)
        self.assertTrue(worker.is_ready(now=now, last_run_at=None))
        signal = worker.demand(now=now, last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertIn("retire", signal.reason)

    def test_the_probe_never_drops_a_promise(self) -> None:
        """``_scan`` stamps overdue rows; ``_survey`` must only count."""
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                created_at=_iso_ago(24 * 30),
            ),
        ])
        kv = _FakeKv()
        worker = _make_worker(store, kv, drop_after_days=14.0)
        now = datetime.now(timezone.utc)
        worker.demand(now=now, last_run_at=None)
        worker.is_ready(now=now, last_run_at=None)
        self.assertEqual(store.update_calls, [])
        self.assertIsNone(kv.get(PENDING_KEY))


class ForceArmTests(unittest.TestCase):
    def test_force_arm_bypasses_age_and_cooldown(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                created_at=_iso_ago(0.1),  # younger than the age gate
            ),
        ])
        kv = _FakeKv()
        kv.set(
            "promise_followthrough.last_fired_at",
            datetime.now(timezone.utc).isoformat(),
        )
        payload = _make_worker(store, kv).force_arm()
        self.assertIsNotNone(payload)
        self.assertEqual(payload["memory_id"], 1)
        self.assertEqual(lifecycle.promise_status(store.get(1)), "surfaced")

    def test_force_arm_considers_surfaced_rows(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(
                1, "Aiko promised: look into LanceDB",
                metadata={"promise_status": "surfaced"},
            ),
        ])
        kv = _FakeKv()
        payload = _make_worker(store, kv).force_arm()
        self.assertIsNotNone(payload)
        self.assertEqual(payload["memory_id"], 1)

    def test_force_arm_none_when_no_assistant_promise(self) -> None:
        store = _FakeMemoryStore([
            _FakeMemory(1, "Jacob promised: call his mom"),
        ])
        kv = _FakeKv()
        self.assertIsNone(_make_worker(store, kv).force_arm())


class PendingSlotHelpersTests(unittest.TestCase):
    def test_load_and_clear_round_trip(self) -> None:
        kv = _FakeKv()
        kv.set(PENDING_KEY, json.dumps({"memory_id": 7, "what": "x"}))
        self.assertEqual(load_pending(kv.get)["memory_id"], 7)
        clear_pending(kv.set)
        self.assertIsNone(load_pending(kv.get))

    def test_malformed_payload_reads_none(self) -> None:
        kv = _FakeKv()
        kv.set(PENDING_KEY, "{not json")
        self.assertIsNone(load_pending(kv.get))
        kv.set(PENDING_KEY, json.dumps({"what": "no id"}))
        self.assertIsNone(load_pending(kv.get))


if __name__ == "__main__":
    unittest.main()
