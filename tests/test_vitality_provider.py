"""Controller-plumbing tests for K68 embodied vitality.

Two halves, each via a minimal stub host (no full SessionController):

* The **provider** ``InnerLifeProvidersMixin._render_vitality_block`` --
  master switch, lazy-recovery write path, force-energy bypass, band
  rendering, exception swallow.
* The **post-turn hook** ``PostTurnHelpersMixin._apply_vitality_turn`` --
  the liven-up (engaged + arousal + novelty boost), length cost, kv
  persist, and the embodiment broadcast.

The math itself is covered in ``tests/test_vitality.py``.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.affect import vitality as v
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


# ── fixtures ────────────────────────────────────────────────────────


class _FakeChatDb:
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


def _mem(**overrides: Any) -> SimpleNamespace:
    base = dict(
        vitality_recover_half_life_hours=2.0,
        vitality_low_threshold=0.30,
        vitality_high_threshold=0.70,
        vitality_expressiveness_floor=0.7,
        vitality_expressiveness_ceil=1.2,
        vitality_cost_chars_per_unit=1200.0,
        vitality_cost_length_unit=0.04,
        vitality_cost_emotion_gain=0.06,
        vitality_cost_max=0.12,
        vitality_boost_engaged=0.05,
        vitality_boost_arousal_threshold=0.55,
        vitality_boost_arousal_gain=0.22,
        vitality_boost_strong_novelty=0.04,
        vitality_boost_mild_novelty=0.02,
        vitality_boost_max=0.15,
        vitality_proactive_factor=0.4,
        # Rhythm off in these plumbing fixtures so the baseline stays the
        # plain circadian curve (deterministic); the off-rhythm roll has
        # its own dedicated suite in tests/test_vitality_rhythm.py.
        vitality_rhythm_exception_chance=0.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _agent(**overrides: Any) -> SimpleNamespace:
    base = dict(
        vitality_enabled=True,
        vitality_rhythm_enabled=False,
        emotion_episodes_enabled=False,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@dataclass
class _FakeSettings:
    agent: SimpleNamespace


class _ProviderHost(InnerLifeProvidersMixin):
    def __init__(self, *, chat_db: _FakeChatDb, agent=None, mem=None) -> None:
        self._chat_db = chat_db
        self._settings = _FakeSettings(agent=agent or _agent())
        self._memory_settings = mem or _mem()
        self.user_display_name = "Jacob"
        self._vitality_force_energy: float | None = None


def _seed(chat_db: _FakeChatDb, energy: float, *, at: datetime | None = None) -> None:
    now = at or datetime.now().astimezone()
    chat_db._store[v.KV_VITALITY] = v.serialize(
        v.VitalityState(energy=energy, last_update_at=now.isoformat()),
    )


# ── provider: master switch ─────────────────────────────────────────


class ProviderMasterSwitchTests(unittest.TestCase):
    def test_disabled_returns_empty_no_io(self) -> None:
        chat_db = _FakeChatDb()
        host = _ProviderHost(chat_db=chat_db, agent=_agent(vitality_enabled=False))
        self.assertEqual(host._render_vitality_block(), "")
        self.assertEqual(chat_db.kv_get_calls, 0)


# ── provider: lazy recovery + render ────────────────────────────────


class ProviderRenderTests(unittest.TestCase):
    def test_low_energy_renders_low_cue_and_persists(self) -> None:
        chat_db = _FakeChatDb()
        _seed(chat_db, 0.12)
        host = _ProviderHost(chat_db=chat_db)
        out = host._render_vitality_block()
        self.assertIn("running low", out)
        # State was recovered + re-persisted.
        self.assertGreaterEqual(chat_db.kv_set_calls, 1)

    def test_normal_energy_is_silent(self) -> None:
        chat_db = _FakeChatDb()
        _seed(chat_db, 0.5)
        host = _ProviderHost(chat_db=chat_db)
        self.assertEqual(host._render_vitality_block(), "")

    def test_recovery_pulls_stale_high_toward_night_baseline(self) -> None:
        # Energy was high 12h ago at 3am; by now it should have relaxed
        # toward the (low) night baseline -> low cue.
        chat_db = _FakeChatDb()
        past = datetime(2026, 6, 30, 3, 0, 0).astimezone() - timedelta(hours=0)
        _seed(chat_db, 0.95, at=past - timedelta(hours=12))
        host = _ProviderHost(chat_db=chat_db)
        # Provider uses real "now"; just assert it doesn't crash and
        # persists a recovered (lower) value than the stored 0.95.
        host._render_vitality_block()
        stored = v.deserialize(
            chat_db._store[v.KV_VITALITY], baseline=0.5,
            now=datetime.now(timezone.utc),
        )
        self.assertLess(stored.energy, 0.95)

    def test_missing_kv_seeds_baseline(self) -> None:
        chat_db = _FakeChatDb()
        host = _ProviderHost(chat_db=chat_db)
        host._render_vitality_block()
        self.assertIn(v.KV_VITALITY, chat_db._store)


# ── provider: force energy ──────────────────────────────────────────


class ProviderForceTests(unittest.TestCase):
    def test_force_low_renders_low_and_consumes_flag(self) -> None:
        chat_db = _FakeChatDb()
        _seed(chat_db, 0.9)
        host = _ProviderHost(chat_db=chat_db)
        host._vitality_force_energy = 0.05
        out = host._render_vitality_block()
        self.assertIn("running low", out)
        self.assertIsNone(host._vitality_force_energy)
        # Forced energy is persisted.
        stored = v.deserialize(
            chat_db._store[v.KV_VITALITY], baseline=0.5,
            now=datetime.now(timezone.utc),
        )
        self.assertAlmostEqual(stored.energy, 0.05, places=4)

    def test_force_high_renders_high(self) -> None:
        chat_db = _FakeChatDb()
        host = _ProviderHost(chat_db=chat_db)
        host._vitality_force_energy = 0.95
        out = host._render_vitality_block()
        self.assertIn("lit up", out)


# ── provider: exception safety ──────────────────────────────────────


class ProviderExceptionTests(unittest.TestCase):
    def test_missing_chat_db_returns_empty(self) -> None:
        host = _ProviderHost(chat_db=_FakeChatDb())
        host._chat_db = None  # type: ignore[assignment]
        self.assertEqual(host._render_vitality_block(), "")


# ── post-turn hook ──────────────────────────────────────────────────


class _PostHost(PostTurnHelpersMixin):
    def __init__(
        self,
        *,
        chat_db: _FakeChatDb,
        arousal: float = 0.4,
        novelty_band: str | None = None,
        engagement_label: str | None = None,
        mem=None,
        agent=None,
    ) -> None:
        self._chat_db = chat_db
        self._memory_settings = mem or _mem()
        self._settings = _FakeSettings(agent=agent or _agent())
        self._user_id = "u"
        self._affect_store = SimpleNamespace(
            get=lambda _uid: SimpleNamespace(arousal=arousal),
        )
        self._novelty_detector = SimpleNamespace(last_band=novelty_band)
        self._last_engagement_label = engagement_label
        self.broadcasts: list[float] = []

    def _notify_vitality(self, energy: float, *, force: bool = False) -> None:
        self.broadcasts.append(float(energy))


class PostTurnTests(unittest.TestCase):
    def test_engaged_exciting_novel_turn_perks_up(self) -> None:
        chat_db = _FakeChatDb()
        _seed(chat_db, 0.30)
        host = _PostHost(
            chat_db=chat_db,
            arousal=0.85,
            novelty_band="strong_novelty",
            engagement_label="engaged",
        )
        host._apply_vitality_turn("a short, lively reply")
        stored = v.deserialize(
            chat_db._store[v.KV_VITALITY], baseline=0.5,
            now=datetime.now(timezone.utc),
        )
        self.assertGreater(stored.energy, 0.30)
        self.assertTrue(host.broadcasts)

    def test_flat_long_turn_drains(self) -> None:
        chat_db = _FakeChatDb()
        _seed(chat_db, 0.50)
        host = _PostHost(
            chat_db=chat_db,
            arousal=0.40,
            novelty_band=None,
            engagement_label="neutral",
        )
        long_reply = "x" * 3000
        host._apply_vitality_turn(long_reply)
        stored = v.deserialize(
            chat_db._store[v.KV_VITALITY], baseline=0.5,
            now=datetime.now(timezone.utc),
        )
        self.assertLess(stored.energy, 0.50)

    def test_disabled_emotion_intensity_zero(self) -> None:
        host = _PostHost(chat_db=_FakeChatDb())
        self.assertEqual(host._peak_emotion_intensity(), 0.0)

    def test_broadcast_carries_new_energy(self) -> None:
        chat_db = _FakeChatDb()
        _seed(chat_db, 0.20)
        host = _PostHost(
            chat_db=chat_db,
            arousal=0.9,
            engagement_label="engaged",
        )
        host._apply_vitality_turn("reply")
        self.assertEqual(len(host.broadcasts), 1)
        self.assertGreater(host.broadcasts[0], 0.20)


if __name__ == "__main__":
    unittest.main()
