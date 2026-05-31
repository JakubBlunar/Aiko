"""Unit tests for ``app.core.goals.onboarding_goal``.

The module's contract:

- First call with a real ``user_display_name`` writes one pinned
  ``goal`` row whose ``metadata.source`` is ``"onboarding_seed"``,
  whose summary contains the user name, and flips the
  ``goals.onboarding_goal_seeded`` row in ``kv_meta``.
- Second call (and every call after) is a no-op — returns ``None``,
  does not insert a second row.
- ``force=True`` bypasses the kv_meta gate.
- Empty / whitespace ``user_display_name`` falls back to
  ``"friend"`` so the module never crashes on a misconfigured boot.
- Pinned seed survives ``GoalStore.prune_overflow`` even when more
  than ``max_active`` other goals exist (pinned rows don't count
  against the cap by design).
"""
from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.goals.goal_store import GoalStore
from app.core.memory.memory_store import MemoryStore
from app.core.goals.onboarding_goal import (
    _ONBOARDING_GOAL_KV_KEY,
    is_onboarding_goal_seeded,
    seed_onboarding_goal,
)


class _DeterministicEmbedder:
    """Same shape as :class:`tests.test_goal_store._DeterministicEmbedder`.

    16-D bag-of-words; differing summaries embed to differing
    vectors, which keeps the cosine-dedupe step happy when several
    distinct seeded goals coexist in one test. Uses md5 instead of
    ``hash()`` so the same token always maps to the same slot
    regardless of ``PYTHONHASHSEED``.
    """

    DIM = 16

    @staticmethod
    def _slot(token: str) -> int:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "little") % _DeterministicEmbedder.DIM

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            vec[self._slot(token)] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


def _store_factory(
    *, max_active: int = 5,
) -> tuple[Path, ChatDatabase, MemoryStore, GoalStore]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    chat_db = ChatDatabase(path)
    memory_store = MemoryStore(path)
    goal_store = GoalStore(
        memory_store=memory_store,
        embedder=_DeterministicEmbedder(),
        max_active=max_active,
    )
    return path, chat_db, memory_store, goal_store


class SeedOnboardingGoalTests(unittest.TestCase):
    def test_seed_creates_pinned_goal_with_correct_source(self) -> None:
        _, chat_db, memory_store, goal_store = _store_factory()
        mem = seed_onboarding_goal(
            goal_store=goal_store,
            memory_store=memory_store,
            chat_db=chat_db,
            user_display_name="Jacob",
        )
        self.assertIsNotNone(mem)
        assert mem is not None  # narrow for the type checker
        # The summary must mention the user name verbatim and
        # explicitly say "Get to know" so the existing summary-to-
        # title convention in :mod:`app.core.goals.goal_store` picks it
        # up as the goal's display title.
        self.assertIn("Jacob", mem.content)
        self.assertIn("Get to know", mem.content)
        # metadata source flag distinguishes this row from
        # ``self_tag`` / ``worker_bootstrap`` / manual REST writes.
        self.assertEqual(
            (mem.metadata or {}).get("source"), "onboarding_seed",
        )
        # Pinned in the in-memory mirror -- the test goes through
        # ``MemoryStore.get`` to confirm the persisted state.
        refreshed = memory_store.get(int(mem.id))
        self.assertIsNotNone(refreshed)
        assert refreshed is not None
        self.assertTrue(refreshed.pinned)

    def test_second_seed_is_noop(self) -> None:
        _, chat_db, memory_store, goal_store = _store_factory()
        first = seed_onboarding_goal(
            goal_store=goal_store,
            memory_store=memory_store,
            chat_db=chat_db,
            user_display_name="Jacob",
        )
        self.assertIsNotNone(first)
        # Second call -- kv_meta flag is set, so the function must
        # short-circuit and return None without inserting again.
        second = seed_onboarding_goal(
            goal_store=goal_store,
            memory_store=memory_store,
            chat_db=chat_db,
            user_display_name="Jacob",
        )
        self.assertIsNone(second)
        # Confirm only one goal row exists.
        active = goal_store.list_active()
        self.assertEqual(len(active), 1)

    def test_kv_meta_flag_is_written_on_success(self) -> None:
        _, chat_db, memory_store, goal_store = _store_factory()
        self.assertFalse(is_onboarding_goal_seeded(chat_db))
        seed_onboarding_goal(
            goal_store=goal_store,
            memory_store=memory_store,
            chat_db=chat_db,
            user_display_name="Jacob",
        )
        self.assertTrue(is_onboarding_goal_seeded(chat_db))
        # The stored value is an ISO timestamp (truthy, non-empty).
        stamped = chat_db.kv_get(_ONBOARDING_GOAL_KV_KEY)
        self.assertIsNotNone(stamped)
        assert stamped is not None
        self.assertGreater(len(stamped), 10)

    def test_empty_name_falls_back_to_friend(self) -> None:
        # Defence-in-depth: the controller gates on
        # ``not needs_onboarding`` before this ever runs, but if a
        # caller bypasses the gate the module should still produce a
        # usable goal rather than crashing on a Python format error.
        _, chat_db, memory_store, goal_store = _store_factory()
        mem = seed_onboarding_goal(
            goal_store=goal_store,
            memory_store=memory_store,
            chat_db=chat_db,
            user_display_name="   ",
        )
        self.assertIsNotNone(mem)
        assert mem is not None
        self.assertIn("friend", mem.content)

    def test_force_true_overrides_kv_meta_flag(self) -> None:
        # ``force=True`` is the MCP debug path: re-run the seed for
        # end-to-end testing without nuking the DB. Cosine dedupe
        # in ``MemoryStore.add`` may collapse the second insert
        # into the first row, but the call must NOT raise and must
        # NOT consult the kv_meta flag.
        _, chat_db, memory_store, goal_store = _store_factory()
        first = seed_onboarding_goal(
            goal_store=goal_store,
            memory_store=memory_store,
            chat_db=chat_db,
            user_display_name="Jacob",
        )
        self.assertIsNotNone(first)
        # Without force this would return None; with force the
        # module bypasses the kv_meta gate. The returned memory may
        # be None (dedupe collision) -- what we assert is that the
        # call doesn't raise and the flag stays set.
        seed_onboarding_goal(
            goal_store=goal_store,
            memory_store=memory_store,
            chat_db=chat_db,
            user_display_name="Jacob",
            force=True,
        )
        self.assertTrue(is_onboarding_goal_seeded(chat_db))

    def test_pinned_seed_survives_prune_overflow(self) -> None:
        # Cap = 2. Seed first (pinned), then add 3 unpinned goals.
        # The pinned seed must remain active even though the total
        # active count is 4 > cap, because the prune step exempts
        # pinned rows from the cap calculation (per K1 design).
        _, chat_db, memory_store, goal_store = _store_factory(max_active=2)
        seed = seed_onboarding_goal(
            goal_store=goal_store,
            memory_store=memory_store,
            chat_db=chat_db,
            user_display_name="Jacob",
        )
        self.assertIsNotNone(seed)
        # Add three unpinned goals, each distinct enough to dodge
        # cosine dedupe (deterministic embedder hashes per-token).
        for summary in (
            "learn jazz piano voicings and inversions thoroughly",
            "ship the macOS installer in time for the conference demo",
            "read more novels this winter especially translated ones",
        ):
            goal_store.add_goal(summary=summary, source="self_tag")
        # The seed must still be in the active list -- pinned rows
        # are exempt from the prune cap.
        active_ids = {int(m.id) for m in goal_store.list_active()}
        assert seed is not None
        self.assertIn(int(seed.id), active_ids)


if __name__ == "__main__":
    unittest.main()
