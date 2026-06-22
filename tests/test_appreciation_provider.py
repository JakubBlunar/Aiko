"""J10 — appreciation beats: provider plumbing."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from app.core.session.inner_life_providers_mixin import (
    InnerLifeProvidersMixin,
    _KV_APPRECIATION_AT,
    _KV_APPRECIATION_ANCHOR,
)


def _iso(days_ago: float) -> str:
    return (
        datetime.now(timezone.utc) - timedelta(days=days_ago)
    ).isoformat()


def _row(rid: int, summary: str, vibe: str, days_ago: float):
    return SimpleNamespace(
        id=rid, summary=summary, vibe=vibe, when=_iso(days_ago),
    )


class _AxesStore:
    def __init__(self, closeness: float) -> None:
        self._c = closeness

    def get(self, _uid: str) -> SimpleNamespace:
        return SimpleNamespace(closeness=self._c)


class _Store:
    def __init__(self, rows) -> None:
        self._rows = list(rows)

    def list(self, *, offset: int = 0, limit: int = 20, vibe=None):
        return list(self._rows), len(self._rows)


class _KvDb:
    def __init__(self, seed=None) -> None:
        self._kv = dict(seed or {})

    def kv_get(self, k):
        return self._kv.get(k)

    def kv_set(self, k, v):
        self._kv[k] = v


class _Host(InnerLifeProvidersMixin):
    def __init__(
        self,
        *,
        enabled: bool = True,
        rows=(),
        closeness: float = 0.5,
        stage: str = "familiar",
        kv=None,
        store: bool = True,
        chat_db: bool = True,
    ) -> None:
        self._settings = SimpleNamespace(agent=SimpleNamespace(
            appreciation_beats_enabled=enabled,
            appreciation_min_closeness=0.25,
            appreciation_cooldown_hours=72.0,
            appreciation_max_anchor_age_days=21.0,
        ))
        self._user_id = "jacob"
        self.user_display_name = "Jacob"
        self._relationship_axes_store = _AxesStore(closeness)
        self._shared_moments_store = _Store(rows) if store else None
        self._chat_db = _KvDb(kv) if chat_db else None
        self._appreciation_force_next = False
        self.relationship_stage_now = lambda: stage  # type: ignore[assignment]


_GOOD = (_row(7, "we built that playlist together", "warm", 2.0),)


class AppreciationProviderTests(unittest.TestCase):
    def test_disabled(self) -> None:
        self.assertEqual(
            _Host(enabled=False, rows=_GOOD)._render_appreciation_block(), "",
        )

    def test_no_store(self) -> None:
        self.assertEqual(
            _Host(store=False, rows=_GOOD)._render_appreciation_block(), "",
        )

    def test_no_chat_db(self) -> None:
        self.assertEqual(
            _Host(chat_db=False, rows=_GOOD)._render_appreciation_block(), "",
        )

    def test_low_closeness_silent(self) -> None:
        self.assertEqual(
            _Host(rows=_GOOD, closeness=0.0)._render_appreciation_block(), "",
        )

    def test_no_positive_anchor(self) -> None:
        rows = (_row(1, "you were hurting", "comfort", 1.0),
                _row(2, "vague chat", "general", 1.0))
        self.assertEqual(_Host(rows=rows)._render_appreciation_block(), "")

    def test_anchor_too_old(self) -> None:
        rows = (_row(3, "old joke", "playful", 60.0),)
        self.assertEqual(_Host(rows=rows)._render_appreciation_block(), "")

    def test_happy_path_fires_and_stamps(self) -> None:
        host = _Host(rows=_GOOD)
        block = host._render_appreciation_block()
        self.assertIn("Jacob", block)
        self.assertIn("playlist", block)
        self.assertIn("appreciat", block.lower())
        # Watermarks stamped.
        self.assertTrue(host._chat_db.kv_get(_KV_APPRECIATION_AT))
        self.assertEqual(host._chat_db.kv_get(_KV_APPRECIATION_ANCHOR), "7")

    def test_cooldown_suppresses_second_call(self) -> None:
        host = _Host(rows=_GOOD)
        self.assertTrue(host._render_appreciation_block())
        self.assertEqual(host._render_appreciation_block(), "")

    def test_anti_repeat_same_anchor_after_cooldown(self) -> None:
        # Cooldown long elapsed, but the only anchor was already used.
        kv = {
            _KV_APPRECIATION_AT: _iso(10.0),  # 10 days ago > 72h
            _KV_APPRECIATION_ANCHOR: "7",
        }
        host = _Host(rows=_GOOD, kv=kv)
        self.assertEqual(host._render_appreciation_block(), "")

    def test_fires_after_cooldown_with_new_anchor(self) -> None:
        kv = {
            _KV_APPRECIATION_AT: _iso(10.0),
            _KV_APPRECIATION_ANCHOR: "99",  # different from anchor id 7
        }
        host = _Host(rows=_GOOD, kv=kv)
        self.assertTrue(host._render_appreciation_block())

    def test_force_bypasses_gates(self) -> None:
        # Low closeness + fresh cooldown would normally suppress.
        host = _Host(rows=_GOOD, closeness=-1.0,
                     kv={_KV_APPRECIATION_AT: _iso(0.0)})
        host._appreciation_force_next = True
        self.assertTrue(host._render_appreciation_block())
        self.assertFalse(host._appreciation_force_next)

    def test_close_stage_softer_tone(self) -> None:
        block = _Host(rows=_GOOD, stage="close")._render_appreciation_block()
        self.assertIn("soft", block.lower())

    def test_new_stage_light_tone(self) -> None:
        block = _Host(rows=_GOOD, stage="new")._render_appreciation_block()
        self.assertIn("light and unforced", block.lower())


if __name__ == "__main__":
    unittest.main()
