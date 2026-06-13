"""Controller-level tests for the follow-up cue provider.

Exercises :meth:`InnerLifeProvidersMixin._render_follow_up_block` via a
minimal mixin host stub (the same approach as
``tests/test_forward_curiosity_provider.py``). Focuses on the provider
plumbing: master-switch gate, the surfacing watermark (one-shot),
empty-ring silence, the optional LLM question suffix, and the force-next
bypass.

Unlike the K34 gap-return cues, the follow-up cue is time-anchored and
independent of the ``_gap_cue_surfaced`` family — it surfaces on the
very next turn after a plan's event time passes, regardless of any other
gap cue.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from app.core.proactive.follow_up_worker import FOLLOW_UP_JOURNAL_KEY
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


class _FakeChatDb:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(follow_up_enabled=True)
    base.update(overrides)
    return SimpleNamespace(**base)


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        cues: list[dict[str, Any]] | None = None,
        force_next: bool = False,
        agent_settings: SimpleNamespace | None = None,
    ) -> None:
        self._settings = SimpleNamespace(
            agent=agent_settings or _make_agent_settings(),
        )
        self._chat_db = _FakeChatDb()
        if cues:
            self._chat_db.store[FOLLOW_UP_JOURNAL_KEY] = json.dumps(cues)
        self._follow_up_force_next = force_next
        self.user_display_name = "Jacob"


def _cue(
    at: str = "2026-06-13T18:55:00+00:00",
    question: str = "",
) -> dict[str, Any]:
    return {
        "at": at,
        "plan": "you were planning to take a bath and watch anime later "
        "this evening",
        "clock": "20:55",
        "question": question,
        "source_id": "7",
        "event_time": "2026-06-13T18:55:00+00:00",
    }


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        host = _Host(
            cues=[_cue()],
            agent_settings=_make_agent_settings(follow_up_enabled=False),
        )
        self.assertEqual(host._render_follow_up_block(), "")


class SurfacingTests(unittest.TestCase):
    def test_fires_and_advances_watermark(self) -> None:
        host = _Host(cues=[_cue()])
        out = host._render_follow_up_block()
        self.assertTrue(out.startswith("Earlier"))
        self.assertIn("you were planning to take a bath", out)
        self.assertIn("ask how it went", out)
        self.assertIn("20:55", out)
        # Watermark advanced to the cue timestamp.
        self.assertEqual(
            host._chat_db.store.get("follow_up.last_surfaced_at"),
            _cue()["at"],
        )

    def test_empty_ring_silent(self) -> None:
        host = _Host(cues=[])
        self.assertEqual(host._render_follow_up_block(), "")

    def test_already_surfaced_is_silent(self) -> None:
        host = _Host(cues=[_cue()])
        host._chat_db.store["follow_up.last_surfaced_at"] = _cue()["at"]
        self.assertEqual(host._render_follow_up_block(), "")

    def test_optional_llm_question_appended(self) -> None:
        host = _Host(cues=[_cue(question="How was the bath and the anime?")])
        out = host._render_follow_up_block()
        self.assertIn("How was the bath and the anime?", out)

    def test_independent_of_gap_cue_flag(self) -> None:
        # Even when a gap cue already surfaced, the follow-up still fires
        # (it does not read or set _gap_cue_surfaced).
        host = _Host(cues=[_cue()])
        host._gap_cue_surfaced = True
        out = host._render_follow_up_block()
        self.assertTrue(out.startswith("Earlier"))
        # It must NOT clobber the gap-cue flag either way.
        self.assertTrue(host._gap_cue_surfaced)


class ForceNextTests(unittest.TestCase):
    def test_force_next_bypasses_watermark(self) -> None:
        host = _Host(cues=[_cue()], force_next=True)
        host._chat_db.store["follow_up.last_surfaced_at"] = _cue()["at"]
        out = host._render_follow_up_block()
        self.assertTrue(out.startswith("Earlier"))
        # Force flag consumed.
        self.assertFalse(host._follow_up_force_next)


if __name__ == "__main__":
    unittest.main()
