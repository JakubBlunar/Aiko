"""Tests for the activity-awareness slice of :class:`SessionController`.

Privacy posture is the load-bearing concern: even if a buggy client
emits ``user_activity`` while the toggle is off, the setter must drop
the value. The render block must always return ``""`` whenever the
feature is disabled OR no app is set.

Bypasses ``SessionController.__init__`` and wires only the slice of
state the activity API needs.
"""
from __future__ import annotations

import threading
import unittest
from dataclasses import dataclass

from app.core.session.session_controller import SessionController


@dataclass
class _AgentStub:
    activity_awareness_enabled: bool = False


@dataclass
class _AssistantStub:
    user_display_name: str = "Jacob"


@dataclass
class _SettingsStub:
    agent: _AgentStub
    assistant: _AssistantStub


def _make_controller(*, enabled: bool = False) -> SessionController:
    controller = SessionController.__new__(SessionController)
    controller._settings = _SettingsStub(  # type: ignore[attr-defined]
        agent=_AgentStub(activity_awareness_enabled=enabled),
        assistant=_AssistantStub(),
    )
    controller._user_active_app = None  # type: ignore[attr-defined]
    controller._typed_silence_lock = threading.Lock()  # type: ignore[attr-defined]
    return controller


class SetUserActiveAppTests(unittest.TestCase):
    def test_set_dropped_when_feature_disabled(self) -> None:
        controller = _make_controller(enabled=False)
        controller.set_user_active_app("Code")
        # The setter coerced to None — server-side privacy gate.
        self.assertIsNone(controller._user_active_app)

    def test_set_accepted_when_feature_enabled(self) -> None:
        controller = _make_controller(enabled=True)
        controller.set_user_active_app("Code")
        self.assertEqual(controller._user_active_app, "Code")

    def test_set_blank_clears_value(self) -> None:
        controller = _make_controller(enabled=True)
        controller.set_user_active_app("Code")
        controller.set_user_active_app("")
        self.assertIsNone(controller._user_active_app)
        controller.set_user_active_app("Code")
        controller.set_user_active_app(None)
        self.assertIsNone(controller._user_active_app)

    def test_set_strips_whitespace(self) -> None:
        controller = _make_controller(enabled=True)
        controller.set_user_active_app("  Firefox  ")
        self.assertEqual(controller._user_active_app, "Firefox")


class RenderActivityBlockTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        controller = _make_controller(enabled=False)
        # Even if a stale value somehow got through, the disabled gate
        # in the render path keeps it out of the prompt.
        controller._user_active_app = "Code"
        self.assertEqual(controller._render_activity_block(), "")

    def test_no_app_returns_empty(self) -> None:
        controller = _make_controller(enabled=True)
        self.assertEqual(controller._render_activity_block(), "")

    def test_enabled_with_app_renders_block(self) -> None:
        controller = _make_controller(enabled=True)
        controller._user_active_app = "Cursor"
        block = controller._render_activity_block()
        self.assertIn("Jacob is currently working in Cursor", block)
        # The trailing tonal nudge must be present so the LLM doesn't
        # turn ambient awareness into surveillance theatre.
        self.assertIn("only mention", block.lower())

    def test_disable_clears_via_setter(self) -> None:
        # Simulates the PATCH /api/settings flow that drops the cache
        # by calling ``set_user_active_app(None)`` after flipping the
        # toggle off. The next render is empty.
        controller = _make_controller(enabled=True)
        controller.set_user_active_app("Discord")
        controller._settings.agent.activity_awareness_enabled = False
        controller.set_user_active_app(None)
        self.assertEqual(controller._render_activity_block(), "")


if __name__ == "__main__":
    unittest.main()
