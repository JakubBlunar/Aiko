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
3. ``force_arm`` (the MCP path) bypasses age + cooldown gates.
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
