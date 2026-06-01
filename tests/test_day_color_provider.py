"""Controller-level tests for the K27 day-color provider.

Exercises ``InnerLifeProvidersMixin._render_day_color_block`` via a
minimal stub host that simulates the controller surface the provider
reads from (``_settings`` / ``_chat_db`` / the two K27 force flags).
Avoids spinning up the full :class:`SessionController` which would
import half the world.

The pure helpers (palette, roll_for_today, is_stale,
render_inner_life_block, get_color_by_name) are covered exhaustively
in ``tests/test_day_color.py``; this module focuses on the provider
plumbing: master switch, lazy-roll write path, stable-read no-write
path, force-flag bypass, reroll-flag override, exception swallow.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest import mock

from app.core.affect import day_color
from app.core.affect.day_color_worker import (
    KV_DAY_COLOR,
    KV_DAY_COLOR_SET_AT,
)
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin


# ── fixtures ────────────────────────────────────────────────────────


class _FakeChatDb:
    """Minimal kv_meta surface the provider depends on. Counters so
    the tests can pin "wrote / didn't write" without re-reading the
    store."""

    def __init__(self, initial: dict[str, str] | None = None) -> None:
        self._store: dict[str, str] = dict(initial or {})
        self.kv_get_calls = 0
        self.kv_set_calls = 0
        self.raise_on_get: Exception | None = None
        self.raise_on_set: Exception | None = None

    def kv_get(self, key: str) -> str | None:
        self.kv_get_calls += 1
        if self.raise_on_get is not None:
            raise self.raise_on_get
        return self._store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.kv_set_calls += 1
        if self.raise_on_set is not None:
            raise self.raise_on_set
        self._store[key] = value


def _make_agent_settings(**overrides: Any) -> SimpleNamespace:
    base = dict(
        day_color_enabled=True,
        day_color_check_interval_seconds=3600,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@dataclass
class _FakeSettings:
    agent: SimpleNamespace


class _Host(InnerLifeProvidersMixin):
    """Minimal mixin host with only the attributes the K27 provider
    reads. Bypasses :meth:`InnerLifeProvidersMixin.__init__` because
    the mixin doesn't define one -- the real controller sets these
    attributes inline in its own ``__init__``.
    """

    def __init__(
        self,
        *,
        chat_db: _FakeChatDb,
        agent_settings: SimpleNamespace | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._settings = _FakeSettings(
            agent=agent_settings or _make_agent_settings(),
        )
        self._day_color_force_next: str | None = None
        self._day_color_force_reroll: bool = False


# ── master switch ────────────────────────────────────────────────────


class MasterSwitchTests(unittest.TestCase):
    def test_disabled_returns_empty(self) -> None:
        chat_db = _FakeChatDb()
        host = _Host(
            chat_db=chat_db,
            agent_settings=_make_agent_settings(day_color_enabled=False),
        )
        self.assertEqual(host._render_day_color_block(), "")
        # Master switch must short-circuit BEFORE any kv_get.
        self.assertEqual(chat_db.kv_get_calls, 0)


# ── lazy fallback path ───────────────────────────────────────────────


class LazyFallbackTests(unittest.TestCase):
    def test_missing_kv_triggers_roll_and_writes(self) -> None:
        chat_db = _FakeChatDb()  # empty
        host = _Host(chat_db=chat_db)
        rendered = host._render_day_color_block()
        # Roll happened: both kv_meta keys are now populated.
        self.assertIn(KV_DAY_COLOR, chat_db._store)
        self.assertIn(KV_DAY_COLOR_SET_AT, chat_db._store)
        new_name = chat_db._store[KV_DAY_COLOR]
        self.assertIn(new_name, {c.name for c in day_color.PALETTE})
        # Rendered line mentions the chosen name.
        self.assertIn(new_name, rendered)
        self.assertTrue(
            rendered.startswith("Your day's colour today:"),
        )

    def test_stale_kv_triggers_roll_and_overwrites(self) -> None:
        # Pretend yesterday's roll is still in kv_meta. The lazy
        # fallback should detect ``is_stale=True`` and overwrite.
        stale_at = (
            datetime.now(timezone.utc) - timedelta(days=2)
        ).isoformat()
        chat_db = _FakeChatDb(
            initial={
                KV_DAY_COLOR: "low_key",
                KV_DAY_COLOR_SET_AT: stale_at,
            }
        )
        host = _Host(chat_db=chat_db)
        rendered = host._render_day_color_block()
        self.assertNotEqual(chat_db._store[KV_DAY_COLOR_SET_AT], stale_at)
        self.assertTrue(rendered)

    def test_lazy_roll_swallows_kv_set_failure(self) -> None:
        chat_db = _FakeChatDb()
        chat_db.raise_on_set = RuntimeError("disk full")
        host = _Host(chat_db=chat_db)
        # The provider should not raise -- it returns "" so the rest
        # of the prompt assembly proceeds.
        self.assertEqual(host._render_day_color_block(), "")


# ── stable-read path ────────────────────────────────────────────────


class StableReadTests(unittest.TestCase):
    def _seed_today(
        self, chat_db: _FakeChatDb, *, name: str = "cozy",
    ) -> None:
        now = datetime.now().astimezone()
        chat_db._store[KV_DAY_COLOR] = name
        chat_db._store[KV_DAY_COLOR_SET_AT] = now.isoformat()

    def test_fresh_kv_returns_existing_without_write(self) -> None:
        chat_db = _FakeChatDb()
        self._seed_today(chat_db, name="cozy")
        before_writes = chat_db.kv_set_calls
        host = _Host(chat_db=chat_db)
        rendered = host._render_day_color_block()
        self.assertIn("cozy", rendered)
        # Stable-read path is read-only.
        self.assertEqual(chat_db.kv_set_calls, before_writes)

    def test_unknown_name_in_kv_returns_empty(self) -> None:
        # If the kv_meta value no longer matches the palette (eg.
        # an old roll from a previous palette version that's since
        # been removed), the provider returns "" rather than
        # crashing.
        chat_db = _FakeChatDb()
        now = datetime.now().astimezone()
        chat_db._store[KV_DAY_COLOR] = "deprecated_name"
        chat_db._store[KV_DAY_COLOR_SET_AT] = now.isoformat()
        host = _Host(chat_db=chat_db)
        self.assertEqual(host._render_day_color_block(), "")


# ── MCP force flags ─────────────────────────────────────────────────


class ForceNextTests(unittest.TestCase):
    def test_force_next_overrides_kv_without_write(self) -> None:
        chat_db = _FakeChatDb()
        # Seed today's colour so the normal path would render "cozy".
        now = datetime.now().astimezone()
        chat_db._store[KV_DAY_COLOR] = "cozy"
        chat_db._store[KV_DAY_COLOR_SET_AT] = now.isoformat()
        host = _Host(chat_db=chat_db)
        host._day_color_force_next = "mischievous"

        rendered = host._render_day_color_block()
        self.assertIn("mischievous", rendered)
        # kv_meta must NOT have been touched (test the persisted
        # roll survives the one-shot override).
        self.assertEqual(chat_db._store[KV_DAY_COLOR], "cozy")
        # And the force flag is one-shot -- next call falls back to
        # the stable-read path.
        self.assertIsNone(host._day_color_force_next)
        next_rendered = host._render_day_color_block()
        self.assertIn("cozy", next_rendered)

    def test_force_next_unknown_falls_through_to_normal_path(self) -> None:
        chat_db = _FakeChatDb()
        now = datetime.now().astimezone()
        chat_db._store[KV_DAY_COLOR] = "cozy"
        chat_db._store[KV_DAY_COLOR_SET_AT] = now.isoformat()
        host = _Host(chat_db=chat_db)
        host._day_color_force_next = "not_a_real_colour"

        rendered = host._render_day_color_block()
        # Unknown name -> fall through to the normal read path.
        self.assertIn("cozy", rendered)
        # Flag was consumed regardless.
        self.assertIsNone(host._day_color_force_next)


class ForceRerollTests(unittest.TestCase):
    def test_force_reroll_writes_new_colour(self) -> None:
        chat_db = _FakeChatDb()
        now = datetime.now().astimezone()
        chat_db._store[KV_DAY_COLOR] = "cozy"
        chat_db._store[KV_DAY_COLOR_SET_AT] = now.isoformat()
        host = _Host(chat_db=chat_db)
        host._day_color_force_reroll = True

        rendered = host._render_day_color_block()
        # Reroll writes a fresh colour to kv_meta.
        self.assertTrue(rendered)
        # The chosen name is in the palette (could be the same as
        # "cozy" if the rng lands there, that's a valid outcome).
        self.assertIn(
            chat_db._store[KV_DAY_COLOR],
            {c.name for c in day_color.PALETTE},
        )
        # Force flag consumed.
        self.assertFalse(host._day_color_force_reroll)


# ── exception swallow ───────────────────────────────────────────────


class ExceptionSafetyTests(unittest.TestCase):
    def test_kv_get_failure_falls_through_to_lazy_roll(self) -> None:
        # When kv_get fails reading ``set_at``, the provider treats
        # the value as missing and falls through to the lazy-roll
        # path. The lazy roll's writes (kv_set) succeed in this
        # case, so a fresh cue lands.
        chat_db = _FakeChatDb()
        chat_db.raise_on_get = RuntimeError("db locked")
        host = _Host(chat_db=chat_db)
        rendered = host._render_day_color_block()
        # Provider must NOT raise.
        self.assertTrue(rendered.startswith("Your day's colour today:"))

    def test_kv_get_and_kv_set_failure_returns_empty(self) -> None:
        # When both reads and writes fail, the provider has no
        # working path and must swallow the cascade to "".
        chat_db = _FakeChatDb()
        chat_db.raise_on_get = RuntimeError("db locked")
        chat_db.raise_on_set = RuntimeError("disk full")
        host = _Host(chat_db=chat_db)
        self.assertEqual(host._render_day_color_block(), "")

    def test_missing_chat_db_returns_empty(self) -> None:
        # The provider defensively reads ``self._chat_db`` via
        # getattr because a partially-built controller may not have
        # one yet. Simulate that by deleting the attribute.
        chat_db = _FakeChatDb()
        host = _Host(chat_db=chat_db)
        host._chat_db = None  # type: ignore[assignment]
        self.assertEqual(host._render_day_color_block(), "")

    def test_roll_failure_returns_empty(self) -> None:
        chat_db = _FakeChatDb()  # empty -> lazy path
        host = _Host(chat_db=chat_db)
        with mock.patch(
            "app.core.affect.day_color.roll_for_today",
            side_effect=RuntimeError("rng exploded"),
        ):
            self.assertEqual(host._render_day_color_block(), "")
        # And no half-written kv_meta on failure.
        self.assertNotIn(KV_DAY_COLOR, chat_db._store)


if __name__ == "__main__":
    unittest.main()
