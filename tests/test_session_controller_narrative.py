"""Tests for ``SessionController._render_narrative_block``.

The narrative block is the inner-monologue cue surfaced from the
prepared-nudge store on typed-mode turns (live-voice has its own
``ProactiveDirector`` consumer). These tests exercise the per-source-kind
labelling, the empty / missing-store / no-nudge paths, and confirm the
non-consuming read pattern (typed turns must not pre-empt the nudge so
``ProactiveDirector`` can still speak it later).
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any

from app.core.session_controller import SessionController


@dataclass(slots=True, frozen=True)
class _StubNudge:
    """Minimal stand-in for ``PreparedNudge`` (only the fields the
    render block reads). Real one is frozen + slots; we mirror that
    so a regression where the controller mutates the nudge stays loud.
    """
    user_id: str
    text: str
    source_kind: str


class _StubStore:
    """In-memory ``PreparedNudgeStore`` stand-in that records calls.

    Crucially exposes both ``get_fresh`` (called by the narrative path)
    and ``consume`` (NOT called by the narrative path — only by the
    proactive director). Tests assert the controller never crosses
    those wires.
    """

    def __init__(self, nudge: _StubNudge | None = None) -> None:
        self._nudge = nudge
        self.get_fresh_calls: list[str] = []
        self.consume_calls: list[str] = []

    def get_fresh(self, user_id: str) -> _StubNudge | None:
        self.get_fresh_calls.append(user_id)
        return self._nudge

    def consume(self, user_id: str) -> _StubNudge | None:
        self.consume_calls.append(user_id)
        nudge = self._nudge
        self._nudge = None
        return nudge


def _make_controller(
    *,
    store: _StubStore | None,
    user_id: str = "jacob",
) -> SessionController:
    """Bypass ``__init__`` and wire only the slice the narrative path
    actually touches (the nudge store + user_id)."""
    controller = SessionController.__new__(SessionController)
    controller._prepared_nudge_store = store  # type: ignore[attr-defined]
    controller._user_id = user_id  # type: ignore[attr-defined]
    return controller


class NarrativeBlockTests(unittest.TestCase):
    def test_no_store_returns_empty(self) -> None:
        controller = _make_controller(store=None)
        self.assertEqual(controller._render_narrative_block(), "")

    def test_no_fresh_nudge_returns_empty(self) -> None:
        store = _StubStore(nudge=None)
        controller = _make_controller(store=store)
        self.assertEqual(controller._render_narrative_block(), "")
        self.assertEqual(store.get_fresh_calls, ["jacob"])
        self.assertEqual(store.consume_calls, [], "narrative path must not consume")

    def test_empty_text_returns_empty(self) -> None:
        store = _StubStore(
            nudge=_StubNudge(user_id="jacob", text="   ", source_kind="callback"),
        )
        controller = _make_controller(store=store)
        self.assertEqual(controller._render_narrative_block(), "")

    def test_callback_kind_uses_loose_thread_label(self) -> None:
        store = _StubStore(
            nudge=_StubNudge(
                user_id="jacob",
                text="that fish-shaped cookie idea",
                source_kind="callback",
            ),
        )
        controller = _make_controller(store=store)
        block = controller._render_narrative_block()
        self.assertIn("loose thread", block.lower())
        self.assertIn("fish-shaped cookie idea", block)

    def test_per_source_kind_labels(self) -> None:
        cases: list[tuple[str, str]] = [
            ("open_question", "wanting to ask"),
            ("callback", "loose thread"),
            ("promise", "said you'd do"),
            ("reflection", "on your mind"),
            ("agenda", "goal you're tracking"),
            ("resume", "where you left off"),
        ]
        for kind, fragment in cases:
            with self.subTest(kind=kind):
                store = _StubStore(
                    nudge=_StubNudge(
                        user_id="jacob",
                        text="placeholder text",
                        source_kind=kind,
                    ),
                )
                controller = _make_controller(store=store)
                block = controller._render_narrative_block().lower()
                self.assertIn(fragment, block)

    def test_unknown_source_kind_falls_back(self) -> None:
        store = _StubStore(
            nudge=_StubNudge(
                user_id="jacob",
                text="some thought",
                source_kind="totally_made_up",
            ),
        )
        controller = _make_controller(store=store)
        block = controller._render_narrative_block()
        # Falls back to the generic "On your mind" label.
        self.assertTrue(block.startswith("On your mind:"))
        self.assertIn("some thought", block)

    def test_non_consuming_read_allows_repeat_calls(self) -> None:
        """Two successive renders must return the same line — the
        narrative path is read-only; only ProactiveDirector consumes.
        """
        store = _StubStore(
            nudge=_StubNudge(
                user_id="jacob",
                text="ping about the fish-cookie",
                source_kind="callback",
            ),
        )
        controller = _make_controller(store=store)
        first = controller._render_narrative_block()
        second = controller._render_narrative_block()
        self.assertEqual(first, second)
        self.assertNotEqual(first, "")
        self.assertEqual(store.consume_calls, [])
        self.assertEqual(store.get_fresh_calls, ["jacob", "jacob"])

    def test_get_fresh_exception_returns_empty(self) -> None:
        """A broken store must not kill the prompt build (the assembler
        also has ``_safe_provider`` belt-and-braces)."""
        class _BoomStore:
            def get_fresh(self, _user_id: str) -> Any:
                raise RuntimeError("db closed")

        controller = _make_controller(store=_BoomStore())  # type: ignore[arg-type]
        self.assertEqual(controller._render_narrative_block(), "")


if __name__ == "__main__":
    unittest.main()
