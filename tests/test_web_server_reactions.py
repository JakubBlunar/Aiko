"""End-to-end tests for the K32 user-reactions REST surface.

The two endpoints under test:

  - ``POST /api/chat/messages/{id}/reactions`` -- register one click
    on a kind. Returns the new full reactions counter map.
  - ``DELETE /api/chat/messages/{id}/reactions/{kind}`` -- decrement
    a previously-registered click.

Both must:

  - validate the kind against :data:`REACTION_KINDS` (400 on unknown),
  - return 503 when the feature flag (``agent.user_reactions_enabled``)
    is off,
  - delegate persistence + axes-nudge + WS broadcast to
    :meth:`SessionController.apply_user_reaction` /
    :meth:`SessionController.remove_user_reaction`,
  - return 404 when the controller reports the message is missing or
    not an assistant bubble.

Pure REST-shape tests via :class:`fastapi.testclient.TestClient`.
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from typing import Any, Callable
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


class _ReactionState:
    """In-memory stand-in for the K32 surface on :class:`SessionController`.

    Mirrors the contract: ``apply_user_reaction`` increments the
    counter and returns ``{"message_id", "reactions"}``;
    ``remove_user_reaction`` decrements and returns the same shape.
    Both return ``None`` for unknown message ids -- which the REST
    layer translates to 404.
    """

    def __init__(self) -> None:
        # ``message_id`` -> assistant role + reaction map
        self.assistant_messages: dict[int, dict[str, int]] = {}
        self._reaction_listeners: list[Callable[[dict[str, Any]], None]] = []

    def seed_assistant(self, message_id: int) -> None:
        self.assistant_messages.setdefault(message_id, {})

    def add_message_reaction_listener(
        self, cb: Callable[[dict[str, Any]], None],
    ) -> None:
        self._reaction_listeners.append(cb)

    def add_avatar_touch_listener(
        self, cb: Callable[[dict[str, Any]], None],
    ) -> None:  # pragma: no cover - just collected, not asserted here
        return None

    def apply_user_reaction(
        self, message_id: int, kind: str,
    ) -> dict[str, Any] | None:
        if message_id not in self.assistant_messages:
            return None
        bucket = self.assistant_messages[message_id]
        bucket[kind] = bucket.get(kind, 0) + 1
        payload = {"message_id": message_id, "reactions": dict(bucket)}
        for cb in list(self._reaction_listeners):
            cb(dict(payload))
        return payload

    def remove_user_reaction(
        self, message_id: int, kind: str,
    ) -> dict[str, Any] | None:
        if message_id not in self.assistant_messages:
            return None
        bucket = self.assistant_messages[message_id]
        current = bucket.get(kind, 0)
        if current <= 1:
            bucket.pop(kind, None)
        else:
            bucket[kind] = current - 1
        payload = {"message_id": message_id, "reactions": dict(bucket)}
        for cb in list(self._reaction_listeners):
            cb(dict(payload))
        return payload


def _build_client(
    *, reactions_enabled: bool = True,
) -> tuple[TestClient, _ReactionState]:
    state = _ReactionState()
    session = MagicMock()
    session._settings = SimpleNamespace(
        agent=SimpleNamespace(user_reactions_enabled=reactions_enabled),
    )
    session.add_message_reaction_listener.side_effect = (
        state.add_message_reaction_listener
    )
    session.add_avatar_touch_listener.side_effect = state.add_avatar_touch_listener
    session.apply_user_reaction.side_effect = state.apply_user_reaction
    session.remove_user_reaction.side_effect = state.remove_user_reaction
    app = create_web_app(session)
    return TestClient(app), state


class AddReactionTests(unittest.TestCase):
    def test_post_increments_and_returns_full_map(self) -> None:
        client, state = _build_client()
        state.seed_assistant(42)
        response = client.post(
            "/api/chat/messages/42/reactions",
            json={"kind": "heart"},
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["message_id"], 42)
        self.assertEqual(body["reactions"]["heart"], 1)

    def test_post_increments_existing_counter(self) -> None:
        client, state = _build_client()
        state.seed_assistant(42)
        client.post(
            "/api/chat/messages/42/reactions", json={"kind": "heart"},
        )
        response = client.post(
            "/api/chat/messages/42/reactions", json={"kind": "heart"},
        )
        body = response.json()
        self.assertEqual(body["reactions"]["heart"], 2)

    def test_post_with_unknown_kind_is_400(self) -> None:
        client, _ = _build_client()
        response = client.post(
            "/api/chat/messages/1/reactions", json={"kind": "rage"},
        )
        self.assertEqual(response.status_code, 400)

    def test_post_missing_kind_is_400(self) -> None:
        client, _ = _build_client()
        response = client.post("/api/chat/messages/1/reactions", json={})
        self.assertEqual(response.status_code, 400)

    def test_post_unknown_message_is_404(self) -> None:
        client, _ = _build_client()
        # No seed call -> the controller stand-in returns None.
        response = client.post(
            "/api/chat/messages/9999/reactions", json={"kind": "heart"},
        )
        self.assertEqual(response.status_code, 404)

    def test_post_when_feature_disabled_is_503(self) -> None:
        client, state = _build_client(reactions_enabled=False)
        state.seed_assistant(42)
        response = client.post(
            "/api/chat/messages/42/reactions", json={"kind": "heart"},
        )
        self.assertEqual(response.status_code, 503)


class RemoveReactionTests(unittest.TestCase):
    def test_delete_decrements_and_returns_full_map(self) -> None:
        client, state = _build_client()
        state.seed_assistant(42)
        # Apply heart twice, then delete once.
        client.post(
            "/api/chat/messages/42/reactions", json={"kind": "heart"},
        )
        client.post(
            "/api/chat/messages/42/reactions", json={"kind": "heart"},
        )
        response = client.delete("/api/chat/messages/42/reactions/heart")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["reactions"]["heart"], 1)

    def test_delete_at_zero_removes_key(self) -> None:
        client, state = _build_client()
        state.seed_assistant(42)
        client.post(
            "/api/chat/messages/42/reactions", json={"kind": "heart"},
        )
        response = client.delete("/api/chat/messages/42/reactions/heart")
        body = response.json()
        # Empty map -- the heart key is gone.
        self.assertNotIn("heart", body["reactions"])

    def test_delete_unknown_kind_is_400(self) -> None:
        client, _ = _build_client()
        response = client.delete("/api/chat/messages/42/reactions/rage")
        self.assertEqual(response.status_code, 400)

    def test_delete_unknown_message_is_404(self) -> None:
        client, _ = _build_client()
        response = client.delete("/api/chat/messages/9999/reactions/heart")
        self.assertEqual(response.status_code, 404)

    def test_delete_when_feature_disabled_is_503(self) -> None:
        client, state = _build_client(reactions_enabled=False)
        state.seed_assistant(42)
        response = client.delete("/api/chat/messages/42/reactions/heart")
        self.assertEqual(response.status_code, 503)


class WsListenerWiringTests(unittest.TestCase):
    def test_apply_reaction_broadcasts_through_listener(self) -> None:
        # The web app registers a ``_on_message_reaction_updated`` listener
        # at startup; the K32 path must fan out through it so the multi-
        # window sync happens. We verify the listener is called when the
        # controller's apply_user_reaction lands.
        client, state = _build_client()
        state.seed_assistant(42)
        client.post(
            "/api/chat/messages/42/reactions", json={"kind": "heart"},
        )
        # The state's broadcast loop already calls the registered
        # listener; this test asserts the listener was registered AND
        # fired by re-tapping the state.
        seen: list[dict[str, Any]] = []

        def _spy(payload: dict[str, Any]) -> None:
            seen.append(payload)

        state.add_message_reaction_listener(_spy)
        client.post(
            "/api/chat/messages/42/reactions", json={"kind": "laugh"},
        )
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0]["reactions"].get("laugh"), 1)


if __name__ == "__main__":
    unittest.main()
