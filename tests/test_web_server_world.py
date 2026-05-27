"""End-to-end tests for the ``/api/world`` REST surface.

Uses a MagicMock-backed ``SessionController`` mirroring the
``test_web_server_memories.py`` style. The real ``WorldStore`` lives
behind the mock so the endpoint contracts (status codes, payload
shape, WS broadcast triggers) stay independent of the storage path.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


def _make_state(state_overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {
        "location_id": 1,
        "posture": "sitting",
        "activity": "watching_screens",
        "mood_note": "",
        "updated_at": "2026-05-27T00:00:00Z",
    }
    if state_overrides:
        base.update(state_overrides)
    return base


def _make_location(loc_id: int, name: str, slug: str | None = None) -> dict[str, Any]:
    return {
        "id": loc_id,
        "slug": slug or name.replace(" ", "_"),
        "name": name,
        "description": "",
        "position": loc_id,
    }


def _make_item(
    item_id: int,
    name: str,
    *,
    location_id: int | None = 1,
    consumable: bool = False,
    quantity: int = 1,
    given_by: str | None = None,
    kind: str = "other",
) -> dict[str, Any]:
    return {
        "id": item_id,
        "slug": name.replace(" ", "_").lower(),
        "name": name,
        "description": "",
        "kind": kind,
        "consumable": consumable,
        "quantity": quantity,
        "location_id": location_id,
        "state": {},
        "given_by": given_by,
        "created_at": "2026-05-27T00:00:00Z",
        "updated_at": "2026-05-27T00:00:00Z",
    }


class _WorldState:
    def __init__(self) -> None:
        self.locations: list[dict[str, Any]] = [
            _make_location(1, "the desk", slug="desk"),
            _make_location(2, "the kitchenette", slug="kitchenette"),
        ]
        self.items: list[dict[str, Any]] = []
        self.state: dict[str, Any] = _make_state()
        self._next_item_id = 1
        self._next_loc_id = 3
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def world_snapshot(self) -> dict[str, Any]:
        return {
            "state": dict(self.state),
            "locations": [dict(l) for l in self.locations],
            "items": [dict(i) for i in self.items],
            "enabled": True,
        }

    def update_world_state(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(("update_world_state", dict(kwargs)))
        for key, value in kwargs.items():
            if value is None and key in ("location_id",):
                self.state[key] = None
            elif value is not None:
                self.state[key] = value
        return dict(self.state)

    def add_world_location(self, *, name: str, slug: str | None = None,
                            description: str = "", position: int | None = None,
                            ) -> dict[str, Any]:
        loc = _make_location(self._next_loc_id, name, slug=slug)
        loc["description"] = description
        if position is not None:
            loc["position"] = position
        self._next_loc_id += 1
        self.locations.append(loc)
        return dict(loc)

    def update_world_location(self, loc_id: int, **kwargs: Any
                              ) -> dict[str, Any] | None:
        for loc in self.locations:
            if loc["id"] == loc_id:
                for k, v in kwargs.items():
                    loc[k] = v
                return dict(loc)
        return None

    def delete_world_location(self, loc_id: int) -> bool:
        before = len(self.locations)
        self.locations = [l for l in self.locations if l["id"] != loc_id]
        return len(self.locations) < before

    def add_world_item(self, **kwargs: Any) -> dict[str, Any]:
        item = _make_item(
            self._next_item_id,
            kwargs["name"],
            location_id=kwargs.get("location_id"),
            consumable=kwargs.get("consumable", False),
            quantity=kwargs.get("quantity", 1),
            given_by=kwargs.get("given_by"),
            kind=kwargs.get("kind", "other"),
        )
        self._next_item_id += 1
        self.items.append(item)
        return dict(item)

    def update_world_item(self, item_id: int, **kwargs: Any
                          ) -> dict[str, Any] | None:
        for item in self.items:
            if item["id"] == item_id:
                for k, v in kwargs.items():
                    item[k] = v
                return dict(item)
        return None

    def delete_world_item(self, item_id: int) -> bool:
        before = len(self.items)
        self.items = [i for i in self.items if i["id"] != item_id]
        return len(self.items) < before

    def consume_world_item(self, item_id: int, *, amount: int = 1
                           ) -> dict[str, Any] | None:
        for item in self.items:
            if item["id"] == item_id:
                consumed = min(amount, item["quantity"])
                item["quantity"] -= consumed
                if item["consumable"] and item["quantity"] <= 0:
                    self.items = [i for i in self.items if i["id"] != item_id]
                    return {"deleted_item_id": item_id, "consumed": consumed}
                return {"item": dict(item), "consumed": consumed}
        return None

    def reseed_world(self, *, force: bool = True) -> dict[str, Any]:
        self.locations = [
            _make_location(1, "the desk", slug="desk"),
            _make_location(2, "the kitchenette", slug="kitchenette"),
        ]
        self.items = []
        self.state = _make_state()
        return self.world_snapshot()


def _build_client() -> tuple[TestClient, _WorldState]:
    state = _WorldState()
    session = MagicMock()
    session.world_snapshot.side_effect = state.world_snapshot
    session.update_world_state.side_effect = state.update_world_state
    session.add_world_location.side_effect = state.add_world_location
    session.update_world_location.side_effect = state.update_world_location
    session.delete_world_location.side_effect = state.delete_world_location
    session.add_world_item.side_effect = state.add_world_item
    session.update_world_item.side_effect = state.update_world_item
    session.delete_world_item.side_effect = state.delete_world_item
    session.consume_world_item.side_effect = state.consume_world_item
    session.reseed_world.side_effect = state.reseed_world
    app = create_web_app(session)
    return TestClient(app), state


class GetWorldEndpointTests(unittest.TestCase):
    def test_returns_full_snapshot(self) -> None:
        client, state = _build_client()
        state.add_world_item(
            name="cookies", kind="food", consumable=True, quantity=3,
            location_id=2, given_by="user",
        )
        response = client.get("/api/world")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("state", body)
        self.assertIn("locations", body)
        self.assertIn("items", body)
        self.assertTrue(body["enabled"])
        self.assertEqual(len(body["items"]), 1)


class PatchWorldStateTests(unittest.TestCase):
    def test_patch_posture(self) -> None:
        client, state = _build_client()
        response = client.patch(
            "/api/world/state", json={"posture": "lying"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state.state["posture"], "lying")

    def test_patch_with_no_fields_returns_400(self) -> None:
        client, _ = _build_client()
        response = client.patch("/api/world/state", json={})
        self.assertEqual(response.status_code, 400)

    def test_patch_invalid_location_id_rejected(self) -> None:
        client, _ = _build_client()
        response = client.patch(
            "/api/world/state", json={"location_id": "the_bed"},
        )
        self.assertEqual(response.status_code, 400)


class CreateWorldLocationTests(unittest.TestCase):
    def test_creates_location(self) -> None:
        client, state = _build_client()
        response = client.post(
            "/api/world/locations",
            json={"name": "the balcony", "description": "outside"},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["location"]["name"], "the balcony")
        self.assertEqual(len(state.locations), 3)

    def test_empty_name_rejected(self) -> None:
        client, _ = _build_client()
        response = client.post(
            "/api/world/locations", json={"name": "  "},
        )
        self.assertEqual(response.status_code, 400)


class CreateWorldItemTests(unittest.TestCase):
    def test_creates_item_with_given_by_user(self) -> None:
        client, state = _build_client()
        response = client.post(
            "/api/world/items",
            json={
                "name": "cookie",
                "kind": "food",
                "consumable": True,
                "quantity": 3,
                "location_id": 2,
                "given_by": "user",
            },
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["item"]["name"], "cookie")
        self.assertEqual(body["item"]["quantity"], 3)
        self.assertEqual(body["item"]["given_by"], "user")
        self.assertEqual(state.items[0]["consumable"], True)

    def test_invalid_quantity_rejected(self) -> None:
        client, _ = _build_client()
        response = client.post(
            "/api/world/items",
            json={"name": "cookie", "quantity": 0},
        )
        self.assertEqual(response.status_code, 400)


class ConsumeWorldItemTests(unittest.TestCase):
    def test_consume_decrements(self) -> None:
        client, state = _build_client()
        state.add_world_item(
            name="cookie", kind="food", consumable=True, quantity=3,
            location_id=2, given_by="user",
        )
        item_id = state.items[0]["id"]
        response = client.post(
            f"/api/world/items/{item_id}/consume", json={"amount": 1},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["consumed"], 1)
        self.assertEqual(body["item"]["quantity"], 2)

    def test_consume_zero_deletes(self) -> None:
        client, state = _build_client()
        state.add_world_item(
            name="cookie", kind="food", consumable=True, quantity=1,
            location_id=2, given_by="user",
        )
        item_id = state.items[0]["id"]
        response = client.post(
            f"/api/world/items/{item_id}/consume", json={"amount": 5},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["deleted_item_id"], item_id)
        self.assertEqual(state.items, [])

    def test_consume_unknown_id_returns_404(self) -> None:
        client, _ = _build_client()
        response = client.post(
            "/api/world/items/9999/consume", json={"amount": 1},
        )
        self.assertEqual(response.status_code, 404)


class DeleteEndpointsTests(unittest.TestCase):
    def test_delete_item(self) -> None:
        client, state = _build_client()
        state.add_world_item(name="lamp", kind="decor")
        item_id = state.items[0]["id"]
        response = client.delete(f"/api/world/items/{item_id}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state.items, [])

    def test_delete_unknown_returns_404(self) -> None:
        client, _ = _build_client()
        response = client.delete("/api/world/items/9999")
        self.assertEqual(response.status_code, 404)


class ReseedTests(unittest.TestCase):
    def test_reseed_returns_snapshot(self) -> None:
        client, state = _build_client()
        state.add_world_item(name="extra", kind="other")
        response = client.post("/api/world/seed?force=true")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertIn("locations", body)
        self.assertEqual(state.items, [])


if __name__ == "__main__":
    unittest.main()
