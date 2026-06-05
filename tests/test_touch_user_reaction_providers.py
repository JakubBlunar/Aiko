"""Tests for the K31 + K32 inner-life providers.

The providers are bound methods on :class:`InnerLifeProvidersMixin`.
We exercise them by binding the unbound methods onto a tiny harness
object that supplies the handful of attributes they actually read
(``_settings``, ``_pending_user_reactions``, ``_chat_db``,
``user_display_name``). This keeps the tests fast (<50ms) and free
of the full SessionController construction cost.

Covers:

  - K32 ``_render_user_reactions_block`` enabled / disabled gating,
    empty-queue silent path, queue drained after a successful
    render, mixed-kind cue shape.
  - K31 ``_render_touch_state_block`` enabled / disabled gating,
    silent path when no daily touch budget is consumed, warning
    cue when the intimate-touch counter is high.
"""
from __future__ import annotations

import unittest
from collections import deque
from datetime import datetime, timezone
from types import SimpleNamespace

from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.touch.touch_gestures import (
    KV_TOUCH_STATE,
    TouchServiceState,
    serialize_state,
)


class _MemoryChatDb:
    """Minimal kv_meta-only stand-in for :class:`ChatDatabase`."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value

    def kv_delete(self, key: str) -> None:
        self.store.pop(key, None)


def _make_harness(
    *,
    user_reactions_enabled: bool = True,
    touch_enabled: bool = True,
) -> SimpleNamespace:
    """Build an object that satisfies the providers' reads."""
    return SimpleNamespace(
        _settings=SimpleNamespace(
            agent=SimpleNamespace(
                user_reactions_enabled=user_reactions_enabled,
                touch_enabled=touch_enabled,
            ),
        ),
        _pending_user_reactions=deque(),
        _chat_db=_MemoryChatDb(),
        user_display_name="Jacob",
    )


def _render_reactions(harness: SimpleNamespace) -> str:
    return InnerLifeProvidersMixin._render_user_reactions_block(harness)  # type: ignore[arg-type]


def _render_touch_state(harness: SimpleNamespace) -> str:
    return InnerLifeProvidersMixin._render_touch_state_block(harness)  # type: ignore[arg-type]


class UserReactionsProviderTests(unittest.TestCase):
    def test_empty_queue_returns_blank(self) -> None:
        harness = _make_harness()
        self.assertEqual(_render_reactions(harness), "")

    def test_single_reaction_renders_cue(self) -> None:
        harness = _make_harness()
        harness._pending_user_reactions.append((42, "heart"))
        block = _render_reactions(harness)
        self.assertIn("Jacob", block)
        self.assertIn("hearted", block)

    def test_queue_drained_after_render(self) -> None:
        harness = _make_harness()
        harness._pending_user_reactions.append((42, "heart"))
        _render_reactions(harness)
        self.assertEqual(len(harness._pending_user_reactions), 0)
        # Second call -- nothing to render.
        self.assertEqual(_render_reactions(harness), "")

    def test_master_switch_off_returns_blank(self) -> None:
        harness = _make_harness(user_reactions_enabled=False)
        harness._pending_user_reactions.append((42, "heart"))
        self.assertEqual(_render_reactions(harness), "")
        # Queue NOT drained when the master switch is off so a later
        # config flip can pick the cue up.
        self.assertEqual(len(harness._pending_user_reactions), 1)

    def test_mixed_kinds_get_summarised(self) -> None:
        harness = _make_harness()
        harness._pending_user_reactions.extend(
            [(1, "heart"), (2, "laugh"), (3, "hug")],
        )
        block = _render_reactions(harness)
        for kind in ("heart", "laugh", "hug"):
            self.assertIn(kind, block)


class TouchStateProviderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def test_blank_kv_returns_silent(self) -> None:
        harness = _make_harness()
        self.assertEqual(_render_touch_state(harness), "")

    def test_warn_when_intimate_count_high(self) -> None:
        harness = _make_harness()
        state = TouchServiceState(
            last_fired={},
            daily_counts={"hug": 2, "cuddle": 1},
            daily_date=self.today,
        )
        harness._chat_db.kv_set(KV_TOUCH_STATE, serialize_state(state))
        block = _render_touch_state(harness)
        self.assertIn("Jacob", block)
        self.assertIn("physical", block)

    def test_silent_when_yesterdays_counts(self) -> None:
        harness = _make_harness()
        # Stale date -> the provider should silently skip the cue
        # because the daily counts no longer apply.
        state = TouchServiceState(
            last_fired={},
            daily_counts={"hug": 5},
            daily_date="2020-01-01",
        )
        harness._chat_db.kv_set(KV_TOUCH_STATE, serialize_state(state))
        self.assertEqual(_render_touch_state(harness), "")

    def test_master_switch_off_returns_blank(self) -> None:
        harness = _make_harness(touch_enabled=False)
        state = TouchServiceState(
            last_fired={},
            daily_counts={"hug": 5},
            daily_date=self.today,
        )
        harness._chat_db.kv_set(KV_TOUCH_STATE, serialize_state(state))
        self.assertEqual(_render_touch_state(harness), "")


if __name__ == "__main__":
    unittest.main()
