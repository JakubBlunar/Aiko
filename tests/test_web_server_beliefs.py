"""End-to-end tests for the ``/api/beliefs`` REST surface (K2)."""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


class _BeliefState:
    """In-memory stand-in for the K2 belief facade."""

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self._next = 1
        self.added_payloads: list[dict[str, Any]] = []
        self.updated_payloads: list[dict[str, Any]] = []
        self.deleted_payloads: list[dict[str, Any]] = []

    def list_beliefs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        kind: str | None = None,
        status: str | None = None,
        include_counts: bool = True,
    ) -> dict[str, Any]:
        rows = list(self.rows.values())
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if status:
            rows = [r for r in rows if r["status"] == status]
        rows.sort(key=lambda r: -r["id"])
        page = rows[offset : offset + limit]
        return {
            "beliefs": page,
            "counts": {
                "active": sum(1 for r in self.rows.values() if r["status"] == "active"),
                "confirmed": sum(1 for r in self.rows.values() if r["status"] == "confirmed"),
                "contradicted": sum(1 for r in self.rows.values() if r["status"] == "contradicted"),
                "stale": sum(1 for r in self.rows.values() if r["status"] == "stale"),
            },
            "enabled": True,
        }

    def add_belief(
        self,
        *,
        kind: str,
        topic: str,
        predicted_state: str,
        confidence: float | None = None,
    ) -> dict[str, Any] | None:
        row = {
            "id": self._next,
            "user_id": "u1",
            "kind": kind,
            "topic": topic.lower().strip(),
            "predicted_state": predicted_state.strip(),
            "confidence": confidence if confidence is not None else 0.6,
            "valence": None,
            "arousal": None,
            "source": "manual",
            "source_message_id": None,
            "observed_at": "2026-01-01T00:00:00Z",
            "last_checked_at": None,
            "status": "active",
            "gap_seen_at": None,
            "metadata": {},
        }
        self.rows[self._next] = row
        self._next += 1
        return row

    def update_belief(
        self,
        belief_id: int,
        *,
        predicted_state: str | None = None,
        confidence: float | None = None,
        status: str | None = None,
    ) -> dict[str, Any] | None:
        row = self.rows.get(int(belief_id))
        if row is None:
            return None
        if predicted_state is not None:
            row["predicted_state"] = predicted_state
        if confidence is not None:
            row["confidence"] = confidence
        if status is not None:
            row["status"] = status
        return row

    def delete_belief(self, belief_id: int) -> bool:
        return self.rows.pop(int(belief_id), None) is not None


def _build_client() -> tuple[TestClient, _BeliefState]:
    state = _BeliefState()
    session = MagicMock()
    session.memory_store = state  # truthy
    session.list_beliefs.side_effect = state.list_beliefs
    session.add_belief.side_effect = state.add_belief
    session.update_belief.side_effect = state.update_belief
    session.delete_belief.side_effect = state.delete_belief
    app = create_web_app(session)
    return TestClient(app), state


class ListBeliefsTests(unittest.TestCase):
    def test_returns_beliefs_and_counts(self) -> None:
        client, state = _build_client()
        state.add_belief(kind="mood", topic="tokyo", predicted_state="excited")
        state.add_belief(kind="opinion", topic="rust", predicted_state="overhyped")
        resp = client.get("/api/beliefs")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data["beliefs"]), 2)
        self.assertEqual(data["counts"]["active"], 2)
        self.assertTrue(data["enabled"])

    def test_filter_by_kind(self) -> None:
        client, state = _build_client()
        state.add_belief(kind="mood", topic="t", predicted_state="x")
        state.add_belief(kind="opinion", topic="r", predicted_state="y")
        resp = client.get("/api/beliefs?kind=mood")
        self.assertEqual(resp.status_code, 200)
        rows = resp.json()["beliefs"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["kind"], "mood")


class CreateBeliefTests(unittest.TestCase):
    def test_create_happy_path(self) -> None:
        client, _ = _build_client()
        resp = client.post(
            "/api/beliefs",
            json={
                "kind": "mood",
                "topic": "tokyo",
                "predicted_state": "excited",
                "confidence": 0.8,
            },
        )
        self.assertEqual(resp.status_code, 200)
        belief = resp.json()["belief"]
        self.assertEqual(belief["topic"], "tokyo")
        self.assertEqual(belief["predicted_state"], "excited")
        self.assertEqual(belief["confidence"], 0.8)

    def test_invalid_kind_rejected(self) -> None:
        client, _ = _build_client()
        resp = client.post(
            "/api/beliefs",
            json={"kind": "bogus", "topic": "x", "predicted_state": "y"},
        )
        self.assertEqual(resp.status_code, 400)

    def test_empty_topic_rejected(self) -> None:
        client, _ = _build_client()
        resp = client.post(
            "/api/beliefs",
            json={"kind": "mood", "topic": "  ", "predicted_state": "y"},
        )
        self.assertEqual(resp.status_code, 400)


class PatchBeliefTests(unittest.TestCase):
    def test_patch_state(self) -> None:
        client, state = _build_client()
        row = state.add_belief(kind="mood", topic="t", predicted_state="excited")
        resp = client.patch(
            f"/api/beliefs/{row['id']}",
            json={"predicted_state": "nervous", "confidence": 0.9},
        )
        self.assertEqual(resp.status_code, 200)
        b = resp.json()["belief"]
        self.assertEqual(b["predicted_state"], "nervous")
        self.assertEqual(b["confidence"], 0.9)

    def test_patch_status_to_contradicted(self) -> None:
        client, state = _build_client()
        row = state.add_belief(kind="mood", topic="t", predicted_state="excited")
        resp = client.patch(
            f"/api/beliefs/{row['id']}",
            json={"status": "contradicted"},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["belief"]["status"], "contradicted")

    def test_patch_unknown_returns_404(self) -> None:
        client, _ = _build_client()
        resp = client.patch("/api/beliefs/9999", json={"confidence": 0.5})
        self.assertEqual(resp.status_code, 404)

    def test_empty_patch_rejected(self) -> None:
        client, state = _build_client()
        row = state.add_belief(kind="mood", topic="t", predicted_state="x")
        resp = client.patch(f"/api/beliefs/{row['id']}", json={})
        self.assertEqual(resp.status_code, 400)


class DeleteBeliefTests(unittest.TestCase):
    def test_delete_existing(self) -> None:
        client, state = _build_client()
        row = state.add_belief(kind="mood", topic="t", predicted_state="x")
        resp = client.delete(f"/api/beliefs/{row['id']}")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["deleted"], row["id"])

    def test_delete_unknown_returns_404(self) -> None:
        client, _ = _build_client()
        resp = client.delete("/api/beliefs/9999")
        self.assertEqual(resp.status_code, 404)


if __name__ == "__main__":
    unittest.main()
