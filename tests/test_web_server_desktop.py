"""End-to-end tests for the ``/api/desktop`` REST surface.

Exercises just the persona-window endpoints introduced for the Tauri
shell. Uses a MagicMock-backed ``SessionController`` so we don't pay the
full ``create_web_app`` startup cost (LLM threads, MCP listeners, audio
devices, etc.) — every other method on the session is auto-stubbed.
"""
from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from app.web.server import create_web_app


class _DesktopState:
    """Tiny stand-in for the session controller's desktop runtime cache.

    Mirrors :meth:`SessionController.update_desktop_settings` semantics
    closely enough for the endpoint tests: clamps via the real helpers,
    persists into a dict, and lets the tests assert the resulting state.
    """

    def __init__(self) -> None:
        self.persona_window = {
            "width": 320,
            "height": 480,
            "always_on_top": True,
        }

    def snapshot(self) -> dict[str, Any]:
        return {"persona_window": dict(self.persona_window)}

    def update(
        self,
        *,
        persona_window_width: int | None = None,
        persona_window_height: int | None = None,
        persona_window_always_on_top: bool | None = None,
    ) -> dict[str, Any]:
        from app.core.settings import (
            clamp_persona_window_height,
            clamp_persona_window_width,
        )

        if persona_window_width is not None:
            self.persona_window["width"] = clamp_persona_window_width(
                persona_window_width
            )
        if persona_window_height is not None:
            self.persona_window["height"] = clamp_persona_window_height(
                persona_window_height
            )
        if persona_window_always_on_top is not None:
            self.persona_window["always_on_top"] = bool(
                persona_window_always_on_top
            )
        return self.snapshot()


def _build_client() -> tuple[TestClient, _DesktopState]:
    """Build a FastAPI test client with a MagicMock session.

    Only the bits the desktop endpoint needs are wired explicitly; the
    rest of the surface is whatever MagicMock auto-stubs."""
    state = _DesktopState()
    session = MagicMock()
    session.desktop_settings.side_effect = state.snapshot
    session.update_desktop_settings.side_effect = state.update
    # ``create_web_app`` reaches into a few attrs at startup; MagicMock
    # auto-resolves listener registration calls to no-ops. The endpoint
    # surface we exercise here is unaffected by those.
    app = create_web_app(session)
    return TestClient(app), state


class GetDesktopEndpointTests(unittest.TestCase):
    def test_returns_runtime_snapshot(self) -> None:
        client, state = _build_client()
        response = client.get("/api/desktop")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), state.snapshot())


class PatchPersonaWindowEndpointTests(unittest.TestCase):
    def test_valid_payload_updates_state(self) -> None:
        client, state = _build_client()
        response = client.patch(
            "/api/desktop/persona-window",
            json={"width": 400, "height": 600, "always_on_top": False},
        )
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["persona_window"]["width"], 400)
        self.assertEqual(body["persona_window"]["height"], 600)
        self.assertFalse(body["persona_window"]["always_on_top"])
        # The mutation lands on the underlying state object too.
        self.assertEqual(state.persona_window["width"], 400)

    def test_partial_payload_only_touches_provided_keys(self) -> None:
        client, state = _build_client()
        response = client.patch(
            "/api/desktop/persona-window",
            json={"width": 360},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(state.persona_window["width"], 360)
        # Untouched keys remain at the default.
        self.assertEqual(state.persona_window["height"], 480)
        self.assertTrue(state.persona_window["always_on_top"])

    def test_out_of_range_value_clamps_silently(self) -> None:
        client, state = _build_client()
        response = client.patch(
            "/api/desktop/persona-window",
            json={"width": 99_999},
        )
        # We don't reject out-of-range integers — the same clamp the
        # loader uses kicks in. This matches the SessionController's
        # forgiveness contract documented in ``settings.py``.
        self.assertEqual(response.status_code, 200)
        self.assertLessEqual(state.persona_window["width"], 800)

    def test_non_integer_width_is_rejected(self) -> None:
        client, _ = _build_client()
        response = client.patch(
            "/api/desktop/persona-window",
            json={"width": "wide"},
        )
        self.assertEqual(response.status_code, 400)

    def test_non_boolean_always_on_top_is_rejected(self) -> None:
        client, _ = _build_client()
        response = client.patch(
            "/api/desktop/persona-window",
            json={"always_on_top": "yes"},
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
