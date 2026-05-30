"""K1 personality backlog tests for the long-term goals journal.

Covers:
- ``[[goal:summary]]`` regex extraction (positive + negative cases).
- ``GoalStore.add_goal`` persists with the documented metadata.
- ``add_progress`` mirrors ``last_progress_note`` / count on the goal.
- ``prune_overflow`` archives oldest unpinned active goal above cap.
- ``prune_progress`` drops oldest progress rows above per-goal cap.
- ``pick_for_reflection`` returns the oldest-touched active goal.
- ``pick_relevant`` returns the top-1 above threshold and ``None`` below.
- ``archive_goal`` / ``unarchive_goal`` move rows between tiers.
- ``active_goal_vectors`` exposes unit-normalised embeddings.
- Response-text strip rule removes ``[[goal:...]]`` from visible text.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import numpy as np

from app.core.chat_database import ChatDatabase
from app.core.goal_store import (
    GoalStore,
    _MAX_SUMMARY_CHARS,
)
from app.core.memory_store import MemoryStore
from app.core.services.response_text_service import (
    extract_goal_tags,
    strip_all_meta_tags,
)


class _DeterministicEmbedder:
    """Returns embeddings stable enough that similar text clusters."""

    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            slot = hash(token) % self.DIM
            vec[slot] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


def _store_factory(
    *,
    max_active: int = 5,
    max_progress_per_goal: int = 12,
) -> tuple[Path, MemoryStore, GoalStore]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    memory_store = MemoryStore(path)
    goal_store = GoalStore(
        memory_store=memory_store,
        embedder=_DeterministicEmbedder(),
        max_active=max_active,
        max_progress_per_goal=max_progress_per_goal,
    )
    return path, memory_store, goal_store


class TestInlineRegex(unittest.TestCase):
    def test_extracts_single_well_formed_tag(self) -> None:
        text = (
            "I want to learn more about jazz piano "
            "[[goal:get better at listening for sevenths and ninths]]."
        )
        tags = extract_goal_tags(text)
        self.assertEqual(len(tags), 1)
        self.assertIn("sevenths", tags[0])

    def test_extracts_multiple_distinct_tags(self) -> None:
        text = (
            "[[goal:write a short essay every weekend]] "
            "[[goal:learn cyrillic alphabet]]"
        )
        tags = extract_goal_tags(text)
        self.assertEqual(len(tags), 2)

    def test_dedupes_repeated_tag_in_same_text(self) -> None:
        text = (
            "[[goal:write more often]] [[goal:write more often]]"
        )
        tags = extract_goal_tags(text)
        self.assertEqual(len(tags), 1)

    def test_rejects_too_short_summary(self) -> None:
        text = "[[goal:hi]]"
        self.assertEqual(extract_goal_tags(text), [])

    def test_rejects_bracket_in_summary(self) -> None:
        text = "[[goal:learn [bad] thing]]"
        self.assertEqual(extract_goal_tags(text), [])

    def test_rejects_newline_in_summary(self) -> None:
        text = "[[goal:line one\nline two]]"
        self.assertEqual(extract_goal_tags(text), [])

    def test_strip_all_meta_tags_removes_goal(self) -> None:
        text = "Hey [[goal:practice piano more]] thanks!"
        cleaned = strip_all_meta_tags(text)
        self.assertNotIn("[[goal:", cleaned)
        self.assertIn("Hey", cleaned)
        self.assertIn("thanks!", cleaned)


class TestGoalStoreWrites(unittest.TestCase):
    def test_add_goal_persists_with_metadata(self) -> None:
        _, mem_store, goals = _store_factory()
        mem = goals.add_goal(
            summary="practice piano scales every other morning",
        )
        self.assertIsNotNone(mem)
        assert mem is not None
        self.assertEqual(mem.kind, "goal")
        self.assertEqual(mem.tier, "long_term")
        meta = mem.metadata or {}
        self.assertEqual(meta.get("summary"), "practice piano scales every other morning")
        self.assertEqual(meta.get("source"), "self_tag")
        self.assertEqual(meta.get("reflection_count"), 0)
        self.assertIsNone(meta.get("last_reflected_at"))
        self.assertIsNone(meta.get("archived_at"))

    def test_add_goal_truncates_long_summary(self) -> None:
        _, _, goals = _store_factory()
        body = "x" * (_MAX_SUMMARY_CHARS + 25)
        mem = goals.add_goal(summary=body)
        self.assertIsNotNone(mem)
        assert mem is not None
        self.assertLessEqual(len(mem.content), _MAX_SUMMARY_CHARS)

    def test_add_goal_rejects_short_body(self) -> None:
        _, _, goals = _store_factory()
        self.assertIsNone(goals.add_goal(summary="hi"))

    def test_add_goal_dedupe_via_memory_store(self) -> None:
        _, _, goals = _store_factory()
        first = goals.add_goal(summary="learn rust language deeply this year")
        self.assertIsNotNone(first)
        second = goals.add_goal(summary="learn rust language deeply this year")
        # MemoryStore returns None on dedupe; GoalStore mirrors that.
        self.assertIsNone(second)
        active = goals.list_active()
        self.assertEqual(len(active), 1)

    def test_add_progress_mirrors_on_goal(self) -> None:
        _, _, goals = _store_factory()
        goal = goals.add_goal(summary="practice listening to jazz harmonies")
        assert goal is not None
        progress = goals.add_progress(
            goal_id=int(goal.id),
            note="picked up the maj7 / dom7 distinction tonight",
            source="worker",
        )
        self.assertIsNotNone(progress)
        assert progress is not None
        self.assertEqual(progress.kind, "goal_progress")
        progress_meta = progress.metadata or {}
        self.assertEqual(int(progress_meta.get("goal_id")), int(goal.id))
        refreshed_goal = goals.list_active()[0]
        gmeta = refreshed_goal.metadata or {}
        self.assertEqual(gmeta.get("reflection_count"), 1)
        self.assertIn("maj7", gmeta.get("last_progress_note") or "")
        self.assertIsNotNone(gmeta.get("last_reflected_at"))
        self.assertEqual(int(gmeta.get("last_reflection_id")), int(progress.id))

    def test_add_progress_rejects_unknown_goal(self) -> None:
        _, _, goals = _store_factory()
        self.assertIsNone(
            goals.add_progress(goal_id=9999, note="this goal does not exist")
        )

    def test_archive_and_unarchive(self) -> None:
        _, mem_store, goals = _store_factory()
        goal = goals.add_goal(summary="explore woodworking next quarter")
        assert goal is not None
        self.assertTrue(goals.archive_goal(int(goal.id)))
        after = mem_store.get(int(goal.id))
        assert after is not None
        self.assertEqual(after.tier, "archive")
        self.assertIsNotNone((after.metadata or {}).get("archived_at"))
        self.assertEqual(goals.list_active(), [])
        # Idempotent re-archive.
        self.assertTrue(goals.archive_goal(int(goal.id)))
        self.assertTrue(goals.unarchive_goal(int(goal.id)))
        revived = mem_store.get(int(goal.id))
        assert revived is not None
        self.assertEqual(revived.tier, "long_term")
        self.assertIsNone((revived.metadata or {}).get("archived_at"))
        self.assertEqual(len(goals.list_active()), 1)

    def test_update_summary_refreshes_content(self) -> None:
        _, mem_store, goals = _store_factory()
        goal = goals.add_goal(summary="learn rust language deeply this year")
        assert goal is not None
        self.assertTrue(goals.update_summary(int(goal.id), summary="learn rust slowly but well"))
        after = mem_store.get(int(goal.id))
        assert after is not None
        self.assertEqual(after.content, "learn rust slowly but well")
        self.assertEqual((after.metadata or {}).get("summary"), "learn rust slowly but well")


class TestGoalStoreMaintenance(unittest.TestCase):
    def test_prune_overflow_archives_oldest_unpinned(self) -> None:
        _, mem_store, goals = _store_factory(max_active=3)
        # Each goal uses distinct tokens so the deterministic embedder
        # doesn't collapse them via dedupe.
        summaries = [
            "learn russian cyrillic alphabet slowly each evening",
            "practice jazz piano sevenths and ninths chord listening",
            "explore woodworking hand chisels and dovetail joints",
            "write short essays about animation art each saturday",
            "study mediterranean cooking techniques and pasta shapes",
        ]
        ids = []
        for summary in summaries:
            mem = goals.add_goal(summary=summary)
            assert mem is not None
            ids.append(int(mem.id))
        active = goals.list_active()
        self.assertEqual(len(active), 3)
        # The two oldest should be archived.
        archived_ids = {
            m.id
            for m in goals.list_all()
            if (m.metadata or {}).get("archived_at")
        }
        self.assertEqual(archived_ids, {ids[0], ids[1]})

    def test_prune_overflow_skips_pinned(self) -> None:
        _, mem_store, goals = _store_factory(max_active=2)
        first = goals.add_goal(summary="pin this jazz piano practice goal forever")
        assert first is not None
        mem_store.set_pinned(int(first.id), True)
        for summary in (
            "learn russian cyrillic alphabet slowly each evening",
            "explore woodworking hand chisels and dovetail joints",
            "study mediterranean cooking techniques and pasta shapes",
        ):
            goals.add_goal(summary=summary)
        active = goals.list_active()
        ids = [m.id for m in active]
        self.assertIn(int(first.id), ids)

    def test_prune_progress_caps_history(self) -> None:
        _, _, goals = _store_factory(max_progress_per_goal=3)
        goal = goals.add_goal(summary="practice jazz piano sevenths and ninths daily")
        assert goal is not None
        # Vary the wording so the deterministic embedder doesn't dedupe.
        notes = [
            "monday session focused on dominant seventh listening",
            "tuesday practiced major ninth chord shapes carefully",
            "wednesday tackled altered dominant voicings on piano",
            "thursday worked on tritone substitutions in standards",
            "friday spent thirty minutes on chord melody arrangement",
        ]
        for note in notes:
            goals.add_progress(goal_id=int(goal.id), note=note)
        history = goals.list_progress(int(goal.id))
        self.assertLessEqual(len(history), 3)


class TestGoalStoreRetrieval(unittest.TestCase):
    def test_pick_for_reflection_picks_oldest_touched(self) -> None:
        _, mem_store, goals = _store_factory()
        first = goals.add_goal(summary="learn russian alphabet slowly")
        second = goals.add_goal(summary="practice jazz piano scales steadily")
        third = goals.add_goal(summary="explore woodworking projects monthly")
        assert first is not None and second is not None and third is not None
        # Reflect on second + third; first stays untouched and oldest.
        goals.add_progress(goal_id=int(second.id), note="picked up sevenths and ninths")
        goals.add_progress(goal_id=int(third.id), note="watched a video about chisels")
        picked = goals.pick_for_reflection()
        self.assertIsNotNone(picked)
        assert picked is not None
        self.assertEqual(int(picked.id), int(first.id))

    def test_pick_relevant_returns_top_match(self) -> None:
        _, _, goals = _store_factory()
        goals.add_goal(summary="practice jazz piano sevenths and ninths daily")
        goals.add_goal(summary="learn russian alphabet slowly each evening")
        # Threshold lowered so the tiny test embedder (16-slot hash)
        # surfaces the right match without needing real semantic
        # similarity.
        match = goals.pick_relevant(
            "practice jazz piano sevenths and ninths daily",
            threshold=0.1,
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertIn("jazz", match.content)

    def test_pick_relevant_below_threshold_returns_none(self) -> None:
        _, _, goals = _store_factory()
        goals.add_goal(summary="practice jazz piano sevenths and ninths daily")
        # Default threshold is 0.5; the query shares no tokens so cosine = 0.
        match = goals.pick_relevant("kubernetes containers nginx orchestration")
        self.assertIsNone(match)

    def test_active_goal_vectors_returns_unit_norm(self) -> None:
        _, _, goals = _store_factory()
        goals.add_goal(summary="practice jazz piano sevenths and ninths daily")
        goals.add_goal(summary="learn russian alphabet slowly each evening")
        vectors = goals.active_goal_vectors()
        self.assertEqual(len(vectors), 2)
        for vec in vectors:
            norm = float(np.linalg.norm(vec))
            self.assertAlmostEqual(norm, 1.0, places=5)


class TestHasAnyActive(unittest.TestCase):
    def test_has_any_active_empty(self) -> None:
        _, _, goals = _store_factory()
        self.assertFalse(goals.has_any_active())

    def test_has_any_active_after_add(self) -> None:
        _, _, goals = _store_factory()
        goals.add_goal(summary="learn russian alphabet slowly")
        self.assertTrue(goals.has_any_active())

    def test_has_any_active_ignores_archived(self) -> None:
        _, _, goals = _store_factory()
        mem = goals.add_goal(summary="learn russian alphabet slowly")
        assert mem is not None
        goals.archive_goal(int(mem.id))
        self.assertFalse(goals.has_any_active())


if __name__ == "__main__":
    unittest.main()
