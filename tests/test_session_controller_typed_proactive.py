"""Tests for typed-mode proactive timer + presence gate.

Bypasses ``SessionController.__init__`` and wires only the slice of
state that the typed-silence machinery actually touches:

  - the four timer-state fields (``_typed_silence_*``)
  - the eligibility predicate inputs (``_settings.agent`` flags,
    ``_user_present``, ``_live_voice_session_active``,
    ``_turn_in_progress``)
  - the ``_proactive`` reference so we can stub
    :meth:`ProactiveDirector.notify_typed_silence` and verify it
    was (or wasn't) invoked when the timer fires.

The real ``threading.Timer`` is used end-to-end so the re-arm-with-
remainder logic is exercised in earnest.
"""
from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass
from typing import Any

from app.core.session_controller import SessionController


@dataclass
class _AgentStub:
    proactive_typed_enabled: bool = True
    proactive_silence_seconds_typed: float = 0.05
    activity_awareness_enabled: bool = False
    proactive_typed_when_away: bool = False


@dataclass
class _SettingsStub:
    agent: _AgentStub


class _DirectorStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def notify_typed_silence(self, session_key: str) -> None:
        self.calls.append(session_key)


def _make_controller(
    *,
    silence: float = 0.05,
    typed_enabled: bool = True,
    user_present: bool = True,
    live_active: bool = False,
    turn_in_progress: bool = False,
    proactive_typed_when_away: bool = False,
) -> tuple[SessionController, _DirectorStub]:
    controller = SessionController.__new__(SessionController)
    controller._settings = _SettingsStub(  # type: ignore[attr-defined]
        agent=_AgentStub(
            proactive_typed_enabled=typed_enabled,
            proactive_silence_seconds_typed=silence,
            proactive_typed_when_away=proactive_typed_when_away,
        ),
    )
    controller._typed_silence_timer = None  # type: ignore[attr-defined]
    controller._typed_silence_lock = threading.Lock()  # type: ignore[attr-defined]
    controller._user_present = user_present  # type: ignore[attr-defined]
    controller._typed_silence_armed_at = None  # type: ignore[attr-defined]
    controller._typed_silence_armed_budget = None  # type: ignore[attr-defined]
    controller._live_voice_session_active = live_active  # type: ignore[attr-defined]
    controller._turn_in_progress = turn_in_progress  # type: ignore[attr-defined]
    controller._user_active_app = None  # type: ignore[attr-defined]
    controller._state = type("S", (), {"session_type": "chat"})()  # type: ignore[attr-defined]
    director = _DirectorStub()
    controller._proactive = director  # type: ignore[attr-defined]
    # ``session_key`` is a property reading ``_user_id`` + ``_session_id``;
    # set the inputs rather than the computed value.
    controller._user_id = "u1"  # type: ignore[attr-defined]
    controller._session_id = "s1"  # type: ignore[attr-defined]
    return controller, director


def _wait_for(predicate, *, timeout: float = 2.0) -> bool:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class TypedSilenceTimerTests(unittest.TestCase):
    def test_arm_then_fire_calls_director(self) -> None:
        controller, director = _make_controller(silence=0.05)
        controller._arm_typed_silence_timer()
        self.assertTrue(_wait_for(lambda: bool(director.calls)))
        self.assertEqual(director.calls, ["u1:s1"])

    def test_disarm_cancels_pending_timer(self) -> None:
        controller, director = _make_controller(silence=0.20)
        controller._arm_typed_silence_timer()
        controller._disarm_typed_silence_timer()
        time.sleep(0.30)
        self.assertEqual(director.calls, [])
        self.assertIsNone(controller._typed_silence_timer)
        self.assertIsNone(controller._typed_silence_armed_at)

    def test_arm_replaces_previous_timer(self) -> None:
        controller, director = _make_controller(silence=0.20)
        controller._arm_typed_silence_timer()
        first_timer = controller._typed_silence_timer
        controller._arm_typed_silence_timer()
        second_timer = controller._typed_silence_timer
        self.assertIsNot(first_timer, second_timer)
        self.assertTrue(_wait_for(lambda: bool(director.calls), timeout=1.0))
        # Only one notify regardless of the two arms.
        self.assertEqual(len(director.calls), 1)

    def test_typed_disabled_skips_arm(self) -> None:
        controller, director = _make_controller(typed_enabled=False)
        controller._arm_typed_silence_timer()
        self.assertIsNone(controller._typed_silence_timer)
        time.sleep(0.10)
        self.assertEqual(director.calls, [])


class PresenceGateTests(unittest.TestCase):
    def test_eligibility_requires_present_and_typed_enabled(self) -> None:
        controller, _ = _make_controller()
        self.assertTrue(controller._is_typed_proactive_eligible())
        controller._user_present = False
        self.assertFalse(controller._is_typed_proactive_eligible())
        controller._user_present = True
        controller._settings.agent.proactive_typed_enabled = False
        self.assertFalse(controller._is_typed_proactive_eligible())

    def test_voice_mode_dominates_eligibility(self) -> None:
        controller, _ = _make_controller(user_present=False)
        controller._live_voice_session_active = True
        # Even with user_present=False, voice-mode active means the
        # predicate short-circuits to False (voice path doesn't share
        # this gate at all). The important regression: presence flips
        # don't affect voice's own proactive nudge.
        self.assertFalse(controller._is_typed_proactive_eligible())

    def test_set_present_false_cancels_pending_timer(self) -> None:
        controller, director = _make_controller(silence=0.50)
        controller._arm_typed_silence_timer()
        controller.set_user_present(False)
        time.sleep(0.60)
        self.assertEqual(director.calls, [])
        self.assertIsNone(controller._typed_silence_timer)
        # Stashed remainder should be > 0 so the next True flip has
        # something to re-arm with.
        self.assertIsNotNone(controller._typed_silence_armed_budget)
        assert controller._typed_silence_armed_budget is not None
        self.assertGreater(controller._typed_silence_armed_budget, 0.0)

    def test_set_present_true_rearms_with_remainder(self) -> None:
        controller, director = _make_controller(silence=0.30)
        controller._arm_typed_silence_timer()
        # Wait a bit so some of the budget elapses, then go away.
        time.sleep(0.10)
        controller.set_user_present(False)
        # Sleep longer than the original budget — the timer is gone,
        # so nothing fires.
        time.sleep(0.40)
        self.assertEqual(director.calls, [])
        # Now come back. The remaining budget should be ~0.20, so the
        # next fire happens within ~0.30 s.
        controller.set_user_present(True)
        self.assertTrue(_wait_for(lambda: bool(director.calls), timeout=1.0))

    def test_voice_start_disarms_typed_timer(self) -> None:
        controller, director = _make_controller(silence=0.30)
        controller._arm_typed_silence_timer()
        controller.set_live_voice_session_active(True)
        time.sleep(0.40)
        self.assertEqual(director.calls, [])
        self.assertIsNone(controller._typed_silence_timer)

    def test_set_present_idempotent(self) -> None:
        controller, _ = _make_controller(silence=0.20)
        controller._arm_typed_silence_timer()
        first_timer = controller._typed_silence_timer
        # Same value as default — should be a no-op (timer untouched).
        controller.set_user_present(True)
        self.assertIs(controller._typed_silence_timer, first_timer)


class TypedWhenAwayFlagTests(unittest.TestCase):
    """``proactive_typed_when_away`` opts out of the presence gate.

    Default (``False``) keeps the historical behavior: hidden windows
    -> no autonomous chime. Setting it to ``True`` lets the typed
    proactive timer fire even when ``_user_present == False``, which
    is the user's "let her talk while I'm away" opt-in.
    """

    def test_flag_off_respects_presence(self) -> None:
        # All other gates passing, but the user is absent and the flag
        # is off -> ineligible. This is the regression that the user
        # originally hit (Aiko spoke while every window was hidden).
        controller, _ = _make_controller(
            user_present=False, proactive_typed_when_away=False,
        )
        self.assertFalse(controller._is_typed_proactive_eligible())

    def test_flag_on_bypasses_presence(self) -> None:
        # Flag on -> eligibility no longer depends on presence.
        controller, _ = _make_controller(
            user_present=False, proactive_typed_when_away=True,
        )
        self.assertTrue(controller._is_typed_proactive_eligible())

    def test_flag_on_still_blocked_by_other_gates(self) -> None:
        # The flag opens the *presence* gate only — the master toggle,
        # voice mode, and turn-in-progress checks still apply. Otherwise
        # flipping it would silently override the "Aiko shouldn't speak
        # while I'm typing" guard.
        controller, _ = _make_controller(
            user_present=False, proactive_typed_when_away=True,
        )
        controller._settings.agent.proactive_typed_enabled = False
        self.assertFalse(controller._is_typed_proactive_eligible())

        controller._settings.agent.proactive_typed_enabled = True
        controller._live_voice_session_active = True
        self.assertFalse(controller._is_typed_proactive_eligible())

        controller._live_voice_session_active = False
        controller._turn_in_progress = True
        self.assertFalse(controller._is_typed_proactive_eligible())


if __name__ == "__main__":
    unittest.main()
