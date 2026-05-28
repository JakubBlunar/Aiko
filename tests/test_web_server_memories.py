"""End-to-end tests for the ``/api/memories`` REST surface.

Covers the editor endpoints introduced for the Memory tab:

* ``GET /api/memories`` with ``offset`` / ``kind`` query params and the new
  ``total`` / ``cap`` response fields.
* ``PATCH /api/memories/{id}`` for content / kind / salience updates.
* ``POST /api/memories`` create + dedupe semantics.
* ``POST /api/memories/{id}/pin`` toggle.

Uses a MagicMock-backed ``SessionController`` so we don't pay the full
``create_web_app`` startup cost. Only the memory-editor-relevant attrs are
wired explicitly; everything else is auto-stubbed.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


class _MemoryState:
    """In-memory stand-in for ``SessionController``'s memory ops.

    Persists rows in a plain dict keyed by id. Mirrors the real surface
    (``list_memories``, ``memory_count``, ``memory_cap``, ``update_memory``,
    ``add_memory``, ``set_memory_pinned``, ``delete_memory``,
    ``add_memory_listener``, ``add_memory_updated_listener``, ``memory_store``)
    closely enough for endpoint tests to exercise both the happy paths and
    the error branches.
    """

    def __init__(self) -> None:
        self.rows: dict[int, dict[str, Any]] = {}
        self._next = 1
        self._dedupe_targets: set[str] = set()
        self._cap = 5000

    def memory_store(self) -> object:
        # Truthy stand-in -- the endpoint's enabled check is just
        # ``session.memory_store is not None``.
        return self

    def list_memories(
        self,
        *,
        limit: int = 50,
        order: str = "recent",
        offset: int = 0,
        kind: str | None = None,
        tier: str | None = None,
    ) -> list[dict[str, Any]]:
        rows = list(self.rows.values())
        if kind:
            rows = [r for r in rows if r["kind"] == kind]
        if tier:
            rows = [r for r in rows if r.get("tier") == tier]
        if order == "top":
            rows.sort(key=lambda r: -r["salience"])
        else:
            rows.sort(key=lambda r: -r["id"])  # recent: highest id first
        return rows[offset : offset + limit]

    def memory_count(
        self,
        kind: str | None = None,
        *,
        tier: str | None = None,
    ) -> int:
        rows = self.rows.values()
        if kind is not None:
            rows = [r for r in rows if r["kind"] == kind]
        if tier is not None:
            rows = [r for r in rows if r.get("tier") == tier]
        return len(list(rows))

    def memory_cap(self) -> int:
        return self._cap

    def add_memory(
        self,
        content: str,
        *,
        kind: str = "fact",
        salience: float = 0.6,
        tier: str = "long_term",
        confidence: float | None = None,
    ) -> dict[str, Any] | None:
        cleaned = content.strip()
        if cleaned in self._dedupe_targets:
            existing = next(
                r for r in self.rows.values() if r["content"] == cleaned
            )
            return {"deduped_into": existing}
        if not cleaned or len(cleaned) < 4:
            return None
        row = {
            "id": self._next,
            "content": cleaned,
            "kind": kind,
            "salience": float(salience),
            "source_session": None,
            "source_message_id": None,
            "created_at": "2026-01-01T00:00:00Z",
            "last_used_at": None,
            "use_count": 0,
            "pinned": False,
            "tier": tier,
            "revival_score": 0.0,
        }
        self.rows[self._next] = row
        self._next += 1
        return {"memory": row}

    def update_memory(
        self,
        memory_id: int,
        *,
        content: str | None = None,
        kind: str | None = None,
        salience: float | None = None,
        tier: str | None = None,
        confidence: float | None = None,
    ) -> dict[str, Any] | None:
        row = self.rows.get(int(memory_id))
        if row is None:
            return None
        if content is not None:
            row["content"] = content.strip()
        if kind is not None:
            row["kind"] = kind
        if salience is not None:
            row["salience"] = float(salience)
        if tier is not None:
            row["tier"] = tier
        return dict(row)

    def set_memory_pinned(
        self,
        memory_id: int,
        pinned: bool,
    ) -> dict[str, Any] | None:
        row = self.rows.get(int(memory_id))
        if row is None:
            return None
        row["pinned"] = bool(pinned)
        if pinned:
            row["salience"] = max(row["salience"], 1.0)
        return dict(row)

    def delete_memory(self, memory_id: int) -> bool:
        return self.rows.pop(int(memory_id), None) is not None


def _build_client() -> tuple[TestClient, _MemoryState]:
    state = _MemoryState()
    session = MagicMock()
    session.memory_store = state  # truthy
    session.list_memories.side_effect = state.list_memories
    session.memory_count.side_effect = state.memory_count
    session.memory_cap.side_effect = state.memory_cap
    session.add_memory.side_effect = state.add_memory
    session.update_memory.side_effect = state.update_memory
    session.set_memory_pinned.side_effect = state.set_memory_pinned
    session.delete_memory.side_effect = state.delete_memory
    app = create_web_app(session)
    return TestClient(app), state


class GetMemoriesEndpointTests(unittest.TestCase):
    def test_returns_total_and_cap(self) -> None:
        client, state = _build_client()
        for i in range(3):
            state.add_memory(f"memory number {i}")
        response = client.get("/api/memories")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(body["cap"], 5000)
        self.assertEqual(body["count"], 3)
        self.assertTrue(body["enabled"])

    def test_kind_filter_narrows_total(self) -> None:
        client, state = _build_client()
        state.add_memory("apples and pears", kind="fact")
        state.add_memory("user is a runner", kind="preference")
        state.add_memory("oranges are okay", kind="fact")
        response = client.get("/api/memories?kind=fact")
        body = response.json()
        self.assertEqual(body["total"], 2)
        self.assertEqual(len(body["memories"]), 2)
        for row in body["memories"]:
            self.assertEqual(row["kind"], "fact")

    def test_offset_pagination(self) -> None:
        client, state = _build_client()
        for i in range(5):
            state.add_memory(f"row number {i:02d}")
        response = client.get("/api/memories?limit=2&offset=2")
        body = response.json()
        self.assertEqual(len(body["memories"]), 2)
        self.assertEqual(body["total"], 5)


class PatchMemoryEndpointTests(unittest.TestCase):
    def test_patch_content_updates_row(self) -> None:
        client, state = _build_client()
        created = state.add_memory("original content here")
        assert created and created.get("memory")
        memory_id = created["memory"]["id"]
        response = client.patch(
            f"/api/memories/{memory_id}",
            json={"content": "freshly edited content"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["memory"]["content"], "freshly edited content")
        self.assertEqual(state.rows[memory_id]["content"], "freshly edited content")

    def test_patch_with_no_fields_returns_400(self) -> None:
        client, state = _build_client()
        created = state.add_memory("anything goes here")
        assert created and created.get("memory")
        memory_id = created["memory"]["id"]
        response = client.patch(f"/api/memories/{memory_id}", json={})
        self.assertEqual(response.status_code, 400)

    def test_patch_unknown_id_returns_404(self) -> None:
        client, _ = _build_client()
        response = client.patch(
            "/api/memories/99999",
            json={"content": "no row here"},
        )
        self.assertEqual(response.status_code, 404)

    def test_patch_invalid_types_rejected(self) -> None:
        client, state = _build_client()
        created = state.add_memory("anything goes here")
        assert created and created.get("memory")
        memory_id = created["memory"]["id"]
        response = client.patch(
            f"/api/memories/{memory_id}",
            json={"salience": "high"},
        )
        self.assertEqual(response.status_code, 400)


class CreateMemoryEndpointTests(unittest.TestCase):
    def test_create_inserts_row(self) -> None:
        client, state = _build_client()
        response = client.post(
            "/api/memories",
            json={"content": "Aiko likes lavender", "kind": "preference"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("memory", body)
        self.assertEqual(body["memory"]["kind"], "preference")
        self.assertEqual(state.memory_count(), 1)

    def test_create_dedupe_returns_deduped_into(self) -> None:
        client, state = _build_client()
        state.add_memory("Aiko likes lavender")
        state._dedupe_targets.add("Aiko likes lavender")
        response = client.post(
            "/api/memories",
            json={"content": "Aiko likes lavender"},
        )
        body = response.json()
        self.assertNotIn("memory", body)
        self.assertIn("deduped_into", body)

    def test_create_empty_content_rejected(self) -> None:
        client, _ = _build_client()
        response = client.post("/api/memories", json={"content": "  "})
        self.assertEqual(response.status_code, 400)


class _ConflictState:
    """In-memory stand-in for the F5 memory-conflict facade."""

    def __init__(self) -> None:
        self.pairs: dict[int, dict[str, Any]] = {}
        self._next = 1
        self.last_resolve_args: dict[str, Any] | None = None
        self.last_dismiss_id: int | None = None

    def record(
        self,
        *,
        a: int,
        b: int,
        similarity: float = 0.85,
        confidence_delta: float = 0.2,
        status: str = "open",
        winner_id: int | None = None,
        loser_id: int | None = None,
    ) -> int:
        pid = self._next
        self._next += 1
        self.pairs[pid] = {
            "id": pid,
            "memory_a_id": a,
            "memory_b_id": b,
            "memory_a": {
                "id": a,
                "content": f"memory {a}",
                "kind": "fact",
                "confidence": 0.7,
            },
            "memory_b": {
                "id": b,
                "content": f"memory {b}",
                "kind": "fact",
                "confidence": 0.5,
            },
            "similarity": similarity,
            "confidence_delta": confidence_delta,
            "heuristic_label": "definite",
            "heuristic_signals": ["antonym:loves/hates"],
            "llm_verdict": None,
            "llm_reason": None,
            "status": status,
            "winner_id": winner_id,
            "loser_id": loser_id,
            "resolution_action": None,
            "flagged_by": "auto",
            "detected_at": "2026-05-28T00:00:00Z",
            "resolved_at": None,
        }
        return pid

    def list_memory_conflicts(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        include_recently_resolved: bool = True,
    ) -> dict[str, Any]:
        open_pairs = [
            p for p in self.pairs.values() if p["status"] == "open"
        ]
        if status is not None:
            open_pairs = [
                p for p in self.pairs.values() if p["status"] == status
            ]
        recently = [
            p for p in self.pairs.values() if p["status"] == "auto_resolved"
        ] if include_recently_resolved else []
        counts = {
            "open": sum(1 for p in self.pairs.values() if p["status"] == "open"),
            "auto_resolved": sum(
                1 for p in self.pairs.values() if p["status"] == "auto_resolved"
            ),
            "user_resolved": sum(
                1 for p in self.pairs.values() if p["status"] == "user_resolved"
            ),
            "dismissed": sum(
                1 for p in self.pairs.values() if p["status"] == "dismissed"
            ),
        }
        return {
            "open": open_pairs[offset : offset + limit],
            "recently_auto_resolved": recently,
            "counts": counts,
        }

    def resolve_memory_conflict(
        self,
        pair_id: int,
        *,
        winner_id: int,
        action: str = "demote",
    ) -> dict[str, Any] | None:
        pair = self.pairs.get(int(pair_id))
        if pair is None:
            return None
        if winner_id not in (pair["memory_a_id"], pair["memory_b_id"]):
            raise ValueError("winner_id must equal memory_a_id or memory_b_id")
        loser_id = (
            pair["memory_b_id"]
            if winner_id == pair["memory_a_id"]
            else pair["memory_a_id"]
        )
        self.last_resolve_args = {
            "pair_id": int(pair_id),
            "winner_id": int(winner_id),
            "action": action,
        }
        pair["status"] = "user_resolved"
        pair["winner_id"] = int(winner_id)
        pair["loser_id"] = int(loser_id)
        pair["resolution_action"] = action
        return {
            "pair_id": int(pair_id),
            "winner_id": int(winner_id),
            "loser_id": int(loser_id),
            "action": action,
            "status": "user_resolved",
        }

    def dismiss_memory_conflict(self, pair_id: int) -> bool:
        pair = self.pairs.get(int(pair_id))
        if pair is None:
            return False
        self.last_dismiss_id = int(pair_id)
        pair["status"] = "dismissed"
        return True


def _build_client_with_conflicts() -> tuple[TestClient, _ConflictState]:
    state = _ConflictState()
    session = MagicMock()
    session.memory_store = state  # truthy
    session.list_memory_conflicts.side_effect = state.list_memory_conflicts
    session.resolve_memory_conflict.side_effect = state.resolve_memory_conflict
    session.dismiss_memory_conflict.side_effect = state.dismiss_memory_conflict
    app = create_web_app(session)
    return TestClient(app), state


class ListMemoryConflictsTests(unittest.TestCase):
    def test_returns_open_and_counts(self) -> None:
        client, state = _build_client_with_conflicts()
        state.record(a=1, b=2)
        state.record(a=3, b=4)
        state.record(
            a=5, b=6,
            status="auto_resolved",
            winner_id=5, loser_id=6,
        )
        response = client.get("/api/memory-conflicts")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["open"]), 2)
        self.assertEqual(len(body["recently_auto_resolved"]), 1)
        self.assertEqual(body["counts"]["open"], 2)
        self.assertEqual(body["counts"]["auto_resolved"], 1)

    def test_status_filter(self) -> None:
        client, state = _build_client_with_conflicts()
        state.record(a=1, b=2)
        state.record(a=3, b=4, status="dismissed")
        response = client.get("/api/memory-conflicts?status=dismissed")
        body = response.json()
        self.assertEqual(len(body["open"]), 1)
        self.assertEqual(body["open"][0]["status"], "dismissed")

    def test_empty_when_no_conflicts(self) -> None:
        client, _ = _build_client_with_conflicts()
        response = client.get("/api/memory-conflicts")
        body = response.json()
        self.assertEqual(body["open"], [])
        self.assertEqual(body["counts"]["open"], 0)


class ResolveMemoryConflictTests(unittest.TestCase):
    def test_resolve_demote_applies(self) -> None:
        client, state = _build_client_with_conflicts()
        pid = state.record(a=1, b=2)
        response = client.post(
            f"/api/memory-conflicts/{pid}/resolve",
            json={"winner_id": 1, "action": "demote"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["pair_id"], pid)
        self.assertEqual(body["winner_id"], 1)
        self.assertEqual(body["loser_id"], 2)
        self.assertEqual(body["action"], "demote")
        self.assertEqual(state.pairs[pid]["status"], "user_resolved")

    def test_resolve_delete_applies(self) -> None:
        client, state = _build_client_with_conflicts()
        pid = state.record(a=1, b=2)
        response = client.post(
            f"/api/memory-conflicts/{pid}/resolve",
            json={"winner_id": 2, "action": "delete"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["winner_id"], 2)
        self.assertEqual(body["loser_id"], 1)
        self.assertEqual(body["action"], "delete")

    def test_resolve_unknown_pair_returns_404(self) -> None:
        client, _ = _build_client_with_conflicts()
        response = client.post(
            "/api/memory-conflicts/9999/resolve",
            json={"winner_id": 1, "action": "demote"},
        )
        self.assertEqual(response.status_code, 404)

    def test_resolve_missing_winner_id_returns_400(self) -> None:
        client, state = _build_client_with_conflicts()
        pid = state.record(a=1, b=2)
        response = client.post(
            f"/api/memory-conflicts/{pid}/resolve",
            json={"action": "demote"},
        )
        self.assertEqual(response.status_code, 400)

    def test_resolve_invalid_winner_id_returns_400(self) -> None:
        client, state = _build_client_with_conflicts()
        pid = state.record(a=1, b=2)
        # 99 is not a, b of the pair.
        response = client.post(
            f"/api/memory-conflicts/{pid}/resolve",
            json={"winner_id": 99, "action": "demote"},
        )
        self.assertEqual(response.status_code, 400)


class DismissMemoryConflictTests(unittest.TestCase):
    def test_dismiss_marks_pair(self) -> None:
        client, state = _build_client_with_conflicts()
        pid = state.record(a=1, b=2)
        response = client.post(f"/api/memory-conflicts/{pid}/dismiss")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["dismissed"], pid)
        self.assertEqual(state.pairs[pid]["status"], "dismissed")

    def test_dismiss_unknown_pair_returns_404(self) -> None:
        client, _ = _build_client_with_conflicts()
        response = client.post("/api/memory-conflicts/9999/dismiss")
        self.assertEqual(response.status_code, 404)


class PinMemoryEndpointTests(unittest.TestCase):
    def test_pin_default_true(self) -> None:
        client, state = _build_client()
        created = state.add_memory("pin me forever")
        assert created and created.get("memory")
        memory_id = created["memory"]["id"]
        response = client.post(f"/api/memories/{memory_id}/pin", json={})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(state.rows[memory_id]["pinned"])

    def test_unpin_explicit_false(self) -> None:
        client, state = _build_client()
        created = state.add_memory("temporarily pinned")
        assert created and created.get("memory")
        memory_id = created["memory"]["id"]
        state.set_memory_pinned(memory_id, True)
        response = client.post(
            f"/api/memories/{memory_id}/pin",
            json={"pinned": False},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(state.rows[memory_id]["pinned"])

    def test_pin_unknown_id_returns_404(self) -> None:
        client, _ = _build_client()
        response = client.post("/api/memories/9999/pin", json={})
        self.assertEqual(response.status_code, 404)

    def test_pin_non_boolean_rejected(self) -> None:
        client, state = _build_client()
        created = state.add_memory("rejected pin payload")
        assert created and created.get("memory")
        memory_id = created["memory"]["id"]
        response = client.post(
            f"/api/memories/{memory_id}/pin",
            json={"pinned": "yes"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
