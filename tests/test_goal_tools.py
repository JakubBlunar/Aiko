"""K1 personality backlog tests for the goal agent tools."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

import numpy as np

from app.core.chat_database import ChatDatabase
from app.core.goal_store import GoalStore
from app.core.memory_store import MemoryStore
from app.llm.tools.base import ToolError
from app.llm.tools.goals import (
    AddGoalTool,
    ArchiveGoalTool,
    ListGoalsTool,
    UpdateGoalProgressTool,
    build_goal_tools,
)


class _DeterministicEmbedder:
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


class _FakeSession:
    """Just enough of SessionController for the goal tools to run."""

    def __init__(self, *, goal_store: GoalStore, memory_store: MemoryStore) -> None:
        self._goal_store = goal_store
        self._memory_store = memory_store
        self.session_key = "test_session"
        self.notify_added: list[dict] = []
        self.notify_updated: list[dict] = []

    def _notify_memory_added(self, payload: dict) -> None:
        self.notify_added.append(payload)

    def _notify_memory_updated(self, payload: dict) -> None:
        self.notify_updated.append(payload)


def _make_session() -> _FakeSession:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    ChatDatabase(path)
    memory_store = MemoryStore(path)
    goal_store = GoalStore(
        memory_store=memory_store,
        embedder=_DeterministicEmbedder(),
    )
    return _FakeSession(goal_store=goal_store, memory_store=memory_store)


class TestAddGoalTool(unittest.TestCase):
    def test_add_goal_persists_and_notifies(self) -> None:
        sess = _make_session()
        tool = AddGoalTool(sess)
        result = tool.run({"summary": "practice listening for sevenths and ninths daily"})
        payload = json.loads(result)
        self.assertTrue(payload["added"])
        self.assertIn("goal", payload)
        self.assertEqual(payload["goal"]["source"], "tool")
        self.assertEqual(len(sess.notify_added), 1)

    def test_add_goal_rejects_empty_summary(self) -> None:
        sess = _make_session()
        tool = AddGoalTool(sess)
        with self.assertRaises(ToolError):
            tool.run({"summary": ""})

    def test_add_goal_duplicate_returns_clean_no_op(self) -> None:
        sess = _make_session()
        tool = AddGoalTool(sess)
        first = json.loads(tool.run({"summary": "practice jazz piano sevenths and ninths daily"}))
        self.assertTrue(first["added"])
        second = json.loads(tool.run({"summary": "practice jazz piano sevenths and ninths daily"}))
        self.assertFalse(second["added"])
        self.assertEqual(second["reason"], "duplicate_or_invalid")


class TestUpdateGoalProgressTool(unittest.TestCase):
    def test_update_progress_writes_and_mirrors(self) -> None:
        sess = _make_session()
        goal = sess._goal_store.add_goal(
            summary="practice jazz piano sevenths and ninths daily",
        )
        assert goal is not None
        tool = UpdateGoalProgressTool(sess)
        result = json.loads(
            tool.run({
                "goal_id": int(goal.id),
                "note": "noticed I keep reaching for maj7 shapes tonight",
            })
        )
        self.assertTrue(result["updated"])
        self.assertIn("goal", result)
        self.assertEqual(result["goal"]["reflection_count"], 1)
        self.assertIn("maj7", result["goal"]["last_progress_note"])
        self.assertEqual(len(sess.notify_added), 1)
        self.assertEqual(len(sess.notify_updated), 1)

    def test_update_progress_rejects_unknown_goal(self) -> None:
        sess = _make_session()
        tool = UpdateGoalProgressTool(sess)
        result = json.loads(
            tool.run({"goal_id": 9999, "note": "a fresh note about something"})
        )
        self.assertFalse(result["updated"])
        self.assertEqual(result["reason"], "unknown_goal_or_invalid_note")

    def test_update_progress_invalid_id_raises(self) -> None:
        sess = _make_session()
        tool = UpdateGoalProgressTool(sess)
        with self.assertRaises(ToolError):
            tool.run({"goal_id": "not-an-int", "note": "anything"})


class TestArchiveGoalTool(unittest.TestCase):
    def test_archive_goal_succeeds(self) -> None:
        sess = _make_session()
        goal = sess._goal_store.add_goal(
            summary="learn russian cyrillic alphabet slowly each evening",
        )
        assert goal is not None
        tool = ArchiveGoalTool(sess)
        result = json.loads(tool.run({"goal_id": int(goal.id)}))
        self.assertTrue(result["archived"])
        self.assertEqual(result["goal"]["tier"], "archive")
        self.assertEqual(sess._goal_store.list_active(), [])

    def test_archive_unknown_goal_returns_clean_no_op(self) -> None:
        sess = _make_session()
        tool = ArchiveGoalTool(sess)
        result = json.loads(tool.run({"goal_id": 999}))
        self.assertFalse(result["archived"])
        self.assertEqual(result["reason"], "unknown_goal")


class TestListGoalsTool(unittest.TestCase):
    def test_list_goals_returns_active_only(self) -> None:
        sess = _make_session()
        a = sess._goal_store.add_goal(
            summary="learn russian cyrillic alphabet slowly each evening",
        )
        b = sess._goal_store.add_goal(
            summary="practice jazz piano sevenths and ninths daily",
        )
        assert a is not None and b is not None
        sess._goal_store.archive_goal(int(a.id))
        tool = ListGoalsTool(sess)
        payload = json.loads(tool.run({}))
        ids = {g["id"] for g in payload["goals"]}
        self.assertEqual(ids, {int(b.id)})


class TestBuildGoalTools(unittest.TestCase):
    def test_build_factory_returns_full_set(self) -> None:
        sess = _make_session()
        names = [t.schema().name for t in build_goal_tools(sess)]
        self.assertEqual(
            names,
            ["list_goals", "add_goal", "update_goal_progress", "archive_goal"],
        )


if __name__ == "__main__":
    unittest.main()
