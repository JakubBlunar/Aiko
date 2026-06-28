"""End-to-end tests for the ``/api/agenda`` REST surface (I3)."""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


class _AgendaState:
    """In-memory stand-in for the Phase 4a agenda facade."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self._next = 1

    def _to_dict(self, row: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": row["id"],
            "goal": row["goal"],
            "status": row["status"],
            "importance": round(float(row["importance"]), 3),
            "created_at": row["created_at"],
            "due_at": row.get("due_at"),
            "last_groomed_at": row.get("last_groomed_at"),
        }

    def list_agenda(
        self, *, status: str | None = None, limit: int = 50,
    ) -> dict[str, Any]:
        rows = list(self.rows.values())
        status_norm = (status or "").strip().lower() or None
        if status_norm and status_norm != "all":
            rows = [r for r in rows if r["status"] == status_norm]
        rows.sort(key=lambda r: (r["status"] != "open", -r["importance"]))
        return {"items": [self._to_dict(r) for r in rows[:limit]], "enabled": True}

    def add_agenda(
        self, *, goal: str, importance: float = 0.5, due_at: str | None = None,
    ) -> dict[str, Any] | None:
        row = {
            "id": self._next,
            "goal": goal.strip()[:240],
            "status": "open",
            "importance": max(0.0, min(1.0, float(importance))),
            "created_at": "2026-01-01T00:00:00Z",
            "due_at": due_at,
            "last_groomed_at": None,
        }
        self.rows[self._next] = row
        self._next += 1
        return self._to_dict(row)

    def update_agenda(
        self,
        agenda_id: int,
        *,
        status: str | None = None,
        importance: float | None = None,
        goal: str | None = None,
        due_at: str | None = None,
    ) -> dict[str, Any] | None:
        row = self.rows.get(int(agenda_id))
        if row is None:
            return None
        if status is not None:
            row["status"] = status
        if importance is not None:
            row["importance"] = max(0.0, min(1.0, float(importance)))
        if goal is not None:
            row["goal"] = goal.strip()[:240]
        if due_at is not None:
            row["due_at"] = due_at
        return self._to_dict(row)

    def agenda_stats(self) -> dict[str, Any]:
        return {"enabled": True, "adds": len(self.rows)}


def _build_client() -> tuple[TestClient, _AgendaState]:
    state = _AgendaState()
    session = MagicMock()
    session.list_agenda.side_effect = state.list_agenda
    session.add_agenda.side_effect = state.add_agenda
    session.update_agenda.side_effect = state.update_agenda
    session.agenda_stats.side_effect = state.agenda_stats
    app = create_web_app(session)
    return TestClient(app), state


class ListAgendaTests(unittest.TestCase):
    def test_returns_items(self) -> None:
        client, state = _build_client()
        state.add_agenda(goal="learn rust", importance=0.7)
        state.add_agenda(goal="plan the trip", importance=0.4)
        resp = client.get("/api/agenda")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["items"]), 2)
        self.assertTrue(data["enabled"])
        # Open-first / importance-desc ordering preserved.
        self.assertEqual(data["items"][0]["goal"], "learn rust")

    def test_filter_by_status(self) -> None:
        client, state = _build_client()
        row = state.add_agenda(goal="ship it", importance=0.5)
        state.update_agenda(row["id"], status="done")
        state.add_agenda(goal="still open", importance=0.5)
        resp = client.get("/api/agenda?status=done")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["items"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "done")


class CreateAgendaTests(unittest.TestCase):
    def test_create_happy_path(self) -> None:
        client, _ = _build_client()
        resp = client.post("/api/agenda", json={"goal": "write the docs"})
        self.assertEqual(resp.status_code, 200)
        item = resp.json()["item"]
        self.assertEqual(item["goal"], "write the docs")
        self.assertEqual(item["status"], "open")

    def test_short_goal_rejected(self) -> None:
        client, _ = _build_client()
        resp = client.post("/api/agenda", json={"goal": "no"})
        self.assertEqual(resp.status_code, 400)

    def test_bad_importance_rejected(self) -> None:
        client, _ = _build_client()
        resp = client.post(
            "/api/agenda", json={"goal": "a real goal", "importance": "high"},
        )
        self.assertEqual(resp.status_code, 400)


class PatchAgendaTests(unittest.TestCase):
    def test_complete(self) -> None:
        client, state = _build_client()
        row = state.add_agenda(goal="finish the feature", importance=0.5)
        resp = client.patch(f"/api/agenda/{row['id']}", json={"status": "done"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["item"]["status"], "done")

    def test_invalid_status_rejected(self) -> None:
        client, state = _build_client()
        row = state.add_agenda(goal="finish the feature", importance=0.5)
        resp = client.patch(f"/api/agenda/{row['id']}", json={"status": "bogus"})
        self.assertEqual(resp.status_code, 400)

    def test_empty_patch_rejected(self) -> None:
        client, state = _build_client()
        row = state.add_agenda(goal="finish the feature", importance=0.5)
        resp = client.patch(f"/api/agenda/{row['id']}", json={})
        self.assertEqual(resp.status_code, 400)

    def test_unknown_returns_404(self) -> None:
        client, _ = _build_client()
        resp = client.patch("/api/agenda/9999", json={"status": "done"})
        self.assertEqual(resp.status_code, 404)


class AgendaStatsTests(unittest.TestCase):
    def test_stats(self) -> None:
        client, state = _build_client()
        state.add_agenda(goal="one goal here", importance=0.5)
        resp = client.get("/api/agenda/stats")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertTrue(data["enabled"])
        self.assertEqual(data["adds"], 1)


if __name__ == "__main__":
    unittest.main()
