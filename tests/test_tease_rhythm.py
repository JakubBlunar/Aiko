"""K48 — tease rhythm (banter as a budget).

Three layers:

1. Pure helpers in ``app.core.conversation.tease_rhythm``
   (``classify_tease`` / ``is_short_reply`` / ``landed_verdict`` /
   ``trailing_tease_streak`` / ``decide_cue`` / ``render_cue``).
2. The post-turn gate ``PostTurnMixin._update_tease_rhythm`` (verdict on
   the prior tease + classify current + arm cue, cooldown-gated) via a
   minimal host.
3. The provider ``_render_tease_rhythm_block`` (one-shot consume + force
   flag).
"""
from __future__ import annotations

import unittest
from collections import deque
from types import SimpleNamespace
from typing import Any

from app.core.conversation.tease_rhythm import (
    CUE_EASE_OFF,
    CUE_GREEN_LIGHT,
    classify_tease,
    decide_cue,
    is_short_reply,
    landed_verdict,
    render_cue,
    trailing_tease_streak,
)
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.session.post_turn_mixin import PostTurnMixin


# ── 1. pure helpers ────────────────────────────────────────────────


class PureHelperTests(unittest.TestCase):
    def test_classify_tease_reaction(self) -> None:
        self.assertTrue(classify_tease("anything at all", "smug"))
        self.assertTrue(classify_tease("hmph, no", "defiant"))
        self.assertFalse(classify_tease("that's lovely", "gentle"))

    def test_classify_tease_text_markers(self) -> None:
        self.assertTrue(classify_tease("oh please, nice try", None))
        self.assertTrue(classify_tease("sure you did, dork", "neutral"))
        self.assertFalse(classify_tease("I hope your day goes well.", None))
        self.assertFalse(classify_tease("", None))

    def test_is_short_reply(self) -> None:
        self.assertTrue(is_short_reply("ok"))
        self.assertTrue(is_short_reply("lol stop it"))
        self.assertFalse(is_short_reply(""))  # empty is not a "reply"
        self.assertFalse(
            is_short_reply("that is actually a really interesting point")
        )

    def test_landed_verdict(self) -> None:
        self.assertIs(landed_verdict(laughed=True, user_reply="whatever"), True)
        self.assertIs(landed_verdict(laughed=False, user_reply="ok"), False)
        self.assertIsNone(
            landed_verdict(
                laughed=False,
                user_reply="haha okay but seriously what do you think",
            )
        )

    def test_trailing_tease_streak(self) -> None:
        self.assertEqual(trailing_tease_streak([False, True, True, True]), 3)
        self.assertEqual(trailing_tease_streak([True, True, False]), 0)
        self.assertEqual(trailing_tease_streak([]), 0)

    def test_decide_cue_priority(self) -> None:
        # miss beats everything else -> ease_off even with high humor.
        self.assertEqual(
            decide_cue(
                last_landed=False, tease_streak=0, humor=0.9,
                consecutive_cap=3, green_light_humor=0.2,
            ),
            CUE_EASE_OFF,
        )
        # streak guard -> ease_off.
        self.assertEqual(
            decide_cue(
                last_landed=None, tease_streak=3, humor=0.0,
                consecutive_cap=3, green_light_humor=0.2,
            ),
            CUE_EASE_OFF,
        )
        # landed + humor over floor -> green_light.
        self.assertEqual(
            decide_cue(
                last_landed=True, tease_streak=1, humor=0.3,
                consecutive_cap=3, green_light_humor=0.2,
            ),
            CUE_GREEN_LIGHT,
        )
        # landed but humor below floor -> gentle, no green light.
        self.assertIsNone(
            decide_cue(
                last_landed=True, tease_streak=1, humor=0.05,
                consecutive_cap=3, green_light_humor=0.2,
            )
        )
        # nothing notable -> None.
        self.assertIsNone(
            decide_cue(
                last_landed=None, tease_streak=1, humor=0.5,
                consecutive_cap=3, green_light_humor=0.2,
            )
        )

    def test_render_cue(self) -> None:
        self.assertIn("ease off", render_cue(CUE_EASE_OFF).lower())
        gl = render_cue(CUE_GREEN_LIGHT, user_name="Jacob")
        self.assertIn("Jacob", gl)
        self.assertIn("landed", gl.lower())
        self.assertEqual(render_cue(None), "")


# ── 2. post-turn gate ──────────────────────────────────────────────


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(
        tease_rhythm_enabled=True,
        tease_rhythm_window=6,
        tease_rhythm_consecutive_cap=3,
        tease_rhythm_green_light_humor=0.2,
        tease_rhythm_cooldown_turns=3,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _FakeAxesStore:
    def __init__(self, humor: float = 0.0) -> None:
        self._humor = humor

    def get(self, _user_id: str) -> SimpleNamespace:  # noqa: ARG002
        return SimpleNamespace(humor=self._humor)


class _GateHost(PostTurnMixin):
    def __init__(
        self,
        *,
        agent: SimpleNamespace | None = None,
        humor: float = 0.0,
        reactions: dict[int, dict[str, int]] | None = None,
    ) -> None:
        self._settings = SimpleNamespace(agent=agent or _agent())
        window = self._settings.agent.tease_rhythm_window
        self._tease_flags: deque[bool] = deque(maxlen=window)
        self._last_tease_message_id: int | None = None
        self._pending_tease_cue: str | None = None
        self._tease_cue_cooldown = 0
        self._tease_rhythm_force: str | None = None
        self._user_id = "u1"
        self._relationship_axes_store = _FakeAxesStore(humor)
        self._reactions = reactions or {}

    def _load_message_reactions(self, message_id: int) -> dict[str, int]:
        return self._reactions.get(message_id, {})


class PostTurnGateTests(unittest.TestCase):
    def test_green_light_when_prior_tease_laughed(self) -> None:
        host = _GateHost(humor=0.5, reactions={10: {"laugh": 1}})
        # Turn 1: Aiko teases (msg id 10).
        host._update_tease_rhythm(
            user_text="so what's the plan",
            assistant_text="oh please, nice try",
            reaction="smug",
            assistant_message_id=10,
        )
        self.assertEqual(host._last_tease_message_id, 10)
        self.assertIsNone(host._pending_tease_cue)
        # Turn 2: user responds; the laugh on msg 10 marks it landed.
        host._update_tease_rhythm(
            user_text="hahaha okay that was good",
            assistant_text="anyway, here's a thought.",
            reaction="cheerful",
            assistant_message_id=11,
        )
        self.assertEqual(host._pending_tease_cue, CUE_GREEN_LIGHT)

    def test_ease_off_when_prior_tease_fell_flat(self) -> None:
        host = _GateHost(humor=0.9, reactions={})  # no laugh
        host._update_tease_rhythm(
            user_text="hi",
            assistant_text="sure you did, showoff",
            reaction="smug",
            assistant_message_id=20,
        )
        # short/curt reply -> missed.
        host._update_tease_rhythm(
            user_text="ok",
            assistant_text="fair enough.",
            reaction="neutral",
            assistant_message_id=21,
        )
        self.assertEqual(host._pending_tease_cue, CUE_EASE_OFF)

    def test_cooldown_blocks_back_to_back(self) -> None:
        host = _GateHost(humor=0.9, reactions={30: {"laugh": 1}})
        host._update_tease_rhythm(
            user_text="x", assistant_text="nice try", reaction="smug",
            assistant_message_id=30,
        )
        host._update_tease_rhythm(
            user_text="lol", assistant_text="another nice try", reaction="smug",
            assistant_message_id=31,
        )
        self.assertEqual(host._pending_tease_cue, CUE_GREEN_LIGHT)
        self.assertGreater(host._tease_cue_cooldown, 0)
        # consume the pending cue (as the provider would) and run again;
        # cooldown should suppress a fresh arm even with another laugh.
        host._pending_tease_cue = None
        host._reactions[31] = {"laugh": 1}
        host._update_tease_rhythm(
            user_text="haha", assistant_text="yeah right", reaction="smug",
            assistant_message_id=32,
        )
        self.assertIsNone(host._pending_tease_cue)

    def test_disabled_is_noop(self) -> None:
        host = _GateHost(agent=_agent(tease_rhythm_enabled=False))
        # The post-turn hook gates on the flag before calling, but the
        # method itself is still safe to call and should arm nothing
        # beyond rolling the ring (decide still runs but enabled-gate is
        # the caller's job) -- verify it doesn't explode.
        host._update_tease_rhythm(
            user_text="hi", assistant_text="hello", reaction="neutral",
            assistant_message_id=1,
        )
        self.assertEqual(host._last_tease_message_id, None)


# ── 3. provider ────────────────────────────────────────────────────


class _ProviderHost(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        pending: str | None = None,
        force: str | None = None,
        agent: SimpleNamespace | None = None,
    ) -> None:
        self._settings = SimpleNamespace(agent=agent or _agent())
        self._pending_tease_cue = pending
        self._tease_rhythm_force = force
        self.user_display_name = "Jacob"


class ProviderTests(unittest.TestCase):
    def test_empty_when_no_cue(self) -> None:
        self.assertEqual(_ProviderHost()._render_tease_rhythm_block(), "")

    def test_renders_pending_cue_once(self) -> None:
        host = _ProviderHost(pending=CUE_GREEN_LIGHT)
        out = host._render_tease_rhythm_block()
        self.assertIn("landed", out.lower())
        self.assertIn("Jacob", out)
        # one-shot: consumed.
        self.assertIsNone(host._pending_tease_cue)
        self.assertEqual(host._render_tease_rhythm_block(), "")

    def test_force_flag_bypasses_and_clears(self) -> None:
        host = _ProviderHost(force=CUE_EASE_OFF)
        out = host._render_tease_rhythm_block()
        self.assertIn("ease off", out.lower())
        self.assertIsNone(host._tease_rhythm_force)

    def test_master_switch_off(self) -> None:
        host = _ProviderHost(
            pending=CUE_GREEN_LIGHT,
            agent=_agent(tease_rhythm_enabled=False),
        )
        self.assertEqual(host._render_tease_rhythm_block(), "")


if __name__ == "__main__":
    unittest.main()
