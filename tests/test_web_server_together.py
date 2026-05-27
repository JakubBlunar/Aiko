"""End-to-end tests for the Together / Shared-moments REST surface."""
from __future__ import annotations

import unittest
from typing import Any, Callable
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


class _TogetherState:
    """In-memory stand-in for the shared-moments + axes section of
    ``SessionController``. Persists rows in a plain dict so the REST
    handlers exercise the happy + error paths."""

    def __init__(self) -> None:
        self.moments: dict[int, dict[str, Any]] = {}
        self._next = 1
        self._axes = {
            "user_id": "jacob",
            "closeness": 0.4,
            "humor": 0.2,
            "trust": 0.3,
            "comfort": 0.2,
            "updated_at": "2026-05-27T12:00:00+00:00",
        }
        self._moment_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._axes_listeners: list[Callable[[dict[str, Any]], None]] = []

    # listeners
    def add_shared_moment_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> None:
        self._moment_listeners.append(listener)

    def add_relationship_axes_listener(
        self, listener: Callable[[dict[str, Any]], None]
    ) -> None:
        self._axes_listeners.append(listener)

    def _broadcast_moment(self, patch: dict[str, Any]) -> None:
        for listener in list(self._moment_listeners):
            listener(patch)

    # together summary
    def get_together_summary(self) -> dict[str, Any]:
        return {
            "phase": "anchored",
            "days_known": 42,
            "total_turns": 999,
            "total_sessions": 17,
            "axes": dict(self._axes),
            "milestones": [{"id": "first_week", "label": "first week", "reached_at": "2026-04-01T00:00:00Z"}],
            "anniversary_today": None,
            "recent_moments_count": len(self.moments),
        }

    # moments CRUD
    def list_shared_moments(
        self, *, offset: int = 0, limit: int = 20, vibe: str | None = None,
    ) -> dict[str, Any]:
        rows = list(self.moments.values())
        if vibe:
            rows = [r for r in rows if r["vibe"] == vibe]
        rows.sort(key=lambda r: -r["id"])
        return {"moments": rows[offset : offset + limit], "total": len(rows)}

    def add_shared_moment(
        self, *, summary: str, vibe: str = "general", when: str | None = None,
    ) -> dict[str, Any] | None:
        cleaned = summary.strip()
        if not cleaned:
            return None
        row = {
            "id": self._next,
            "summary": cleaned,
            "vibe": vibe,
            "when": when or "2026-05-27T12:00:00+00:00",
            "created_at": "2026-05-27T12:00:00+00:00",
            "salience": 0.7,
            "pinned": True,
            "source": "manual",
            "confidence": 1.0,
            "source_message_ids": [],
            "last_anniversaried_at": None,
        }
        self.moments[self._next] = row
        self._next += 1
        self._broadcast_moment({"action": "created", "moment": row})
        return row

    def update_shared_moment(self, moment_id: int, **fields: Any) -> dict[str, Any] | None:
        row = self.moments.get(int(moment_id))
        if row is None:
            return None
        for key, value in fields.items():
            if key in row:
                row[key] = value
        self._broadcast_moment({"action": "updated", "moment": row})
        return dict(row)

    def delete_shared_moment(self, moment_id: int) -> bool:
        row = self.moments.pop(int(moment_id), None)
        if row is None:
            return False
        self._broadcast_moment({"action": "deleted", "moment_id": int(moment_id)})
        return True

    def mark_message_as_moment(
        self, message_id: int, *, vibe: str = "general",
    ) -> dict[str, Any] | None:
        if message_id == 0:
            return None
        return self.add_shared_moment(
            summary=f"message {message_id} marked",
            vibe=vibe,
        )


def _build_client() -> tuple[TestClient, _TogetherState]:
    state = _TogetherState()
    session = MagicMock()
    session.add_shared_moment_listener.side_effect = state.add_shared_moment_listener
    session.add_relationship_axes_listener.side_effect = (
        state.add_relationship_axes_listener
    )
    session.get_together_summary.side_effect = state.get_together_summary
    session.list_shared_moments.side_effect = state.list_shared_moments
    session.add_shared_moment.side_effect = state.add_shared_moment
    session.update_shared_moment.side_effect = state.update_shared_moment
    session.delete_shared_moment.side_effect = state.delete_shared_moment
    session.mark_message_as_moment.side_effect = state.mark_message_as_moment
    app = create_web_app(session)
    return TestClient(app), state


class TogetherEndpointTests(unittest.TestCase):
    def test_get_together_returns_summary(self) -> None:
        client, _ = _build_client()
        response = client.get("/api/together")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["phase"], "anchored")
        self.assertEqual(body["days_known"], 42)
        self.assertIn("axes", body)
        self.assertIn("milestones", body)


class SharedMomentsListTests(unittest.TestCase):
    def test_list_paginates(self) -> None:
        client, state = _build_client()
        for i in range(5):
            state.add_shared_moment(summary=f"moment {i}", vibe="warm")
        response = client.get("/api/shared-moments?limit=2&offset=1")
        body = response.json()
        self.assertEqual(body["total"], 5)
        self.assertEqual(len(body["moments"]), 2)

    def test_vibe_filter(self) -> None:
        client, state = _build_client()
        state.add_shared_moment(summary="a warm one", vibe="warm")
        state.add_shared_moment(summary="a playful one", vibe="playful")
        state.add_shared_moment(summary="another warm one", vibe="warm")
        response = client.get("/api/shared-moments?vibe=warm")
        body = response.json()
        self.assertEqual(body["total"], 2)
        for row in body["moments"]:
            self.assertEqual(row["vibe"], "warm")


class SharedMomentsCreateTests(unittest.TestCase):
    def test_post_creates_moment(self) -> None:
        client, state = _build_client()
        response = client.post(
            "/api/shared-moments",
            json={"summary": "we cooked dinner together", "vibe": "warm"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["moment"]["summary"], "we cooked dinner together")
        self.assertEqual(body["moment"]["vibe"], "warm")
        self.assertEqual(len(state.moments), 1)

    def test_post_empty_summary_rejected(self) -> None:
        client, _ = _build_client()
        response = client.post("/api/shared-moments", json={"summary": "   "})
        self.assertEqual(response.status_code, 400)

    def test_post_missing_summary_rejected(self) -> None:
        client, _ = _build_client()
        response = client.post("/api/shared-moments", json={})
        self.assertEqual(response.status_code, 400)


class SharedMomentsPatchTests(unittest.TestCase):
    def test_patch_updates_summary(self) -> None:
        client, state = _build_client()
        row = state.add_shared_moment(summary="first", vibe="warm")
        response = client.patch(
            f"/api/shared-moments/{row['id']}",
            json={"summary": "edited", "vibe": "tender"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["moment"]["summary"], "edited")
        self.assertEqual(body["moment"]["vibe"], "tender")

    def test_patch_empty_body_rejected(self) -> None:
        client, state = _build_client()
        row = state.add_shared_moment(summary="x", vibe="warm")
        response = client.patch(f"/api/shared-moments/{row['id']}", json={})
        self.assertEqual(response.status_code, 400)

    def test_patch_unknown_id_returns_404(self) -> None:
        client, _ = _build_client()
        response = client.patch("/api/shared-moments/99999", json={"summary": "ok"})
        self.assertEqual(response.status_code, 404)

    def test_patch_pinned_must_be_bool(self) -> None:
        client, state = _build_client()
        row = state.add_shared_moment(summary="x", vibe="warm")
        response = client.patch(
            f"/api/shared-moments/{row['id']}", json={"pinned": "yes"},
        )
        self.assertEqual(response.status_code, 400)


class SharedMomentsDeleteTests(unittest.TestCase):
    def test_delete_removes_row(self) -> None:
        client, state = _build_client()
        row = state.add_shared_moment(summary="x", vibe="warm")
        response = client.delete(f"/api/shared-moments/{row['id']}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["deleted_moment_id"], row["id"])
        self.assertNotIn(row["id"], state.moments)

    def test_delete_unknown_id_returns_404(self) -> None:
        client, _ = _build_client()
        response = client.delete("/api/shared-moments/99999")
        self.assertEqual(response.status_code, 404)


class MarkMomentEndpointTests(unittest.TestCase):
    def test_mark_creates_moment(self) -> None:
        client, state = _build_client()
        response = client.post(
            "/api/chat/messages/42/mark-moment",
            json={"vibe": "tender"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("moment", body)
        self.assertEqual(body["moment"]["vibe"], "tender")
        self.assertEqual(len(state.moments), 1)

    def test_mark_unknown_message_returns_404(self) -> None:
        client, _ = _build_client()
        response = client.post(
            "/api/chat/messages/0/mark-moment",
            json={"vibe": "warm"},
        )
        self.assertEqual(response.status_code, 404)

    def test_mark_defaults_to_general_vibe(self) -> None:
        client, _ = _build_client()
        response = client.post("/api/chat/messages/7/mark-moment")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["moment"]["vibe"], "general")


class WSBroadcastTests(unittest.TestCase):
    """The web app registers a shared-moments listener on boot. We
    verify a CRUD round-trip drives a broadcast through the registered
    callback."""

    def test_create_triggers_broadcast(self) -> None:
        client, state = _build_client()
        # ``_build_client`` already called ``create_web_app`` which in
        # turn called ``add_shared_moment_listener`` with the broadcaster.
        # Verify the listener list has at least one entry.
        self.assertGreaterEqual(len(state._moment_listeners), 1)

        received: list[dict] = []
        state._moment_listeners.append(received.append)

        # POST creates a row -> state._broadcast_moment fires.
        client.post("/api/shared-moments", json={"summary": "hi", "vibe": "warm"})
        self.assertTrue(received)
        self.assertEqual(received[-1]["action"], "created")


if __name__ == "__main__":
    unittest.main()
