"""Tests for the L14 aspiration-momentum producer/consumer pair.

* :class:`AspirationMomentumWorker` (producer): drafts a cue for a stale active
  aspiration, per-concept cooldown + signature suppression, the enabled switch,
  empty when nothing is active, and the force-next bypass.
* :meth:`InnerLifeProvidersMixin._render_aspiration_momentum_block` (consumer):
  renders the newest cue once and advances the watermark; silent when disabled /
  empty / already surfaced; force-next bypasses the watermark.
* the L14 settings parse (synthesis + momentum knobs).
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.proactive import aspiration_momentum as _am
from app.core.proactive.aspiration_momentum_worker import (
    AspirationMomentumWorker,
)
from app.core.session.inner_life_providers_mixin import InnerLifeProvidersMixin

_UTC = timezone.utc


class _FakeKv:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value


def _concept(cid, *, subject="user", label="Toward self-hosting",
             confidence=0.8, last_reinforced_at=None, created_at=""):
    return SimpleNamespace(
        concept_id=cid, subject=subject, label=label, confidence=confidence,
        last_reinforced_at=last_reinforced_at, created_at=created_at,
    )


class _FakeView:
    def __init__(self, concepts, *, enabled=True) -> None:
        self._concepts = concepts
        self.enabled = enabled

    def core(self, *, kind=None, subject=None, min_confidence=0.0, limit=None):
        return [
            c for c in self._concepts
            if getattr(c, "subject", None) == subject
            and float(getattr(c, "confidence", 0.0)) >= float(min_confidence)
        ]


def _worker(view, kv, **overrides):
    kwargs: dict[str, Any] = dict(
        kv_get=kv.get,
        kv_set=kv.set,
        view_provider=lambda: view,
        user_display_name_provider=lambda: "Jacob",
        min_confidence=0.6,
        staleness_min_days=7.0,
        cooldown_days=10.0,
    )
    kwargs.update(overrides)
    return AspirationMomentumWorker(**kwargs)


def _stale_iso(days: float) -> str:
    return (datetime.now(_UTC) - timedelta(days=days)).isoformat()


class ProducerTests(unittest.TestCase):
    def test_drafts_cue_for_stale_active_aspiration(self) -> None:
        kv = _FakeKv()
        view = _FakeView([
            _concept(1, last_reinforced_at=_stale_iso(30)),
        ])
        out = _worker(view, kv).run()
        self.assertEqual(out["drafted"], 1)
        ring = _am.load_cues(kv.get)
        self.assertEqual(len(ring), 1)
        self.assertEqual(ring[-1]["concept_id"], 1)
        self.assertEqual(ring[-1]["subject"], "user")
        # per-concept cooldown + signature watermarks written.
        self.assertIn(_am.per_concept_cooldown_key(1), kv.store)
        self.assertEqual(
            kv.store.get("aspiration_momentum.last_signature"),
            _am.signature(1),
        )

    def test_fresh_aspiration_not_stale_enough(self) -> None:
        kv = _FakeKv()
        view = _FakeView([_concept(1, last_reinforced_at=_stale_iso(2))])
        out = _worker(view, kv).run()
        self.assertEqual(out["drafted"], 0)
        self.assertEqual(_am.load_cues(kv.get), [])

    def test_per_concept_cooldown_suppresses(self) -> None:
        kv = _FakeKv()
        # Concept 1 fired 2 days ago (< 10d cooldown) -> skipped even though
        # stale. No other candidate -> nothing drafted.
        kv.store[_am.per_concept_cooldown_key(1)] = _stale_iso(2)
        view = _FakeView([_concept(1, last_reinforced_at=_stale_iso(30))])
        out = _worker(view, kv).run()
        self.assertEqual(out["drafted"], 0)

    def test_same_signature_suppressed(self) -> None:
        kv = _FakeKv()
        kv.store["aspiration_momentum.last_signature"] = _am.signature(1)
        view = _FakeView([_concept(1, last_reinforced_at=_stale_iso(30))])
        out = _worker(view, kv).run()
        self.assertEqual(out["drafted"], 0)
        self.assertEqual(out.get("same_signature"), _am.signature(1))

    def test_disabled_switch(self) -> None:
        kv = _FakeKv()
        view = _FakeView([_concept(1, last_reinforced_at=_stale_iso(30))])
        out = _worker(view, kv, enabled_provider=lambda: False).run()
        self.assertTrue(out.get("disabled"))
        self.assertEqual(_am.load_cues(kv.get), [])

    def test_empty_when_none_active(self) -> None:
        kv = _FakeKv()
        out = _worker(_FakeView([]), kv).run()
        self.assertEqual(out["drafted"], 0)
        self.assertTrue(out.get("no_active"))

    def test_no_view_silent(self) -> None:
        kv = _FakeKv()
        out = _worker(_FakeView([], enabled=False), kv).run()
        self.assertEqual(out["drafted"], 0)
        self.assertTrue(out.get("no_view"))

    def test_force_next_bypasses_gates(self) -> None:
        kv = _FakeKv()
        # Fresh (not stale) + on cooldown; force should still draft.
        kv.store[_am.per_concept_cooldown_key(1)] = _stale_iso(0)
        kv.store["aspiration_momentum.last_signature"] = _am.signature(1)
        view = _FakeView([_concept(1, last_reinforced_at=_stale_iso(1))])
        w = _worker(view, kv)
        w.force_next()
        out = w.run()
        self.assertEqual(out["drafted"], 1)

    def test_stalest_wins_and_rotates(self) -> None:
        kv = _FakeKv()
        view = _FakeView([
            _concept(1, confidence=0.9, last_reinforced_at=_stale_iso(8)),
            _concept(2, confidence=0.7, last_reinforced_at=_stale_iso(40)),
        ])
        out = _worker(view, kv).run()
        # Concept 2 is far staler -> it wins despite lower confidence.
        self.assertEqual(out["concept_id"], 2)


class SelectCandidateTests(unittest.TestCase):
    def test_aiko_subject_carried(self) -> None:
        now = datetime.now(_UTC)
        kv = _FakeKv()
        cand = _am.select_candidate(
            [_concept(5, subject="aiko", last_reinforced_at=_stale_iso(30))],
            now=now, kv_get=kv.get, min_confidence=0.6,
            staleness_min_days=7.0, cooldown_days=10.0,
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.subject, "aiko")
        self.assertEqual(cand.signature, "aspiration:5")

    def test_undated_concept_is_maximally_stale(self) -> None:
        now = datetime.now(_UTC)
        kv = _FakeKv()
        cand = _am.select_candidate(
            [_concept(9, last_reinforced_at=None, created_at="")],
            now=now, kv_get=kv.get, min_confidence=0.6,
            staleness_min_days=7.0, cooldown_days=10.0,
        )
        self.assertIsNotNone(cand)
        self.assertEqual(cand.concept_id, 9)


# ── consumer ─────────────────────────────────────────────────────────────


class _FakeChatDb:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


class _Host(InnerLifeProvidersMixin):
    def __init__(self, *, cues=None, force_next=False, enabled=True) -> None:
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(aspiration_momentum_enabled=enabled),
        )
        self._chat_db = _FakeChatDb()
        if cues is not None:
            self._chat_db.store[_am.MOMENTUM_JOURNAL_KEY] = json.dumps(cues)
        self._aspiration_momentum_force_next = force_next
        self.user_display_name = "Jacob"


def _cue(at="2026-06-13T18:55:00+00:00", subject="user",
         label="Building toward a self-hosted life"):
    return {"at": at, "concept_id": 1, "subject": subject, "label": label}


class ConsumerTests(unittest.TestCase):
    def test_fires_and_advances_watermark(self) -> None:
        host = _Host(cues=[_cue()])
        out = host._render_aspiration_momentum_block()
        self.assertIn("Building toward a self-hosted life", out)
        self.assertIn("Jacob", out)
        self.assertEqual(
            host._chat_db.store.get("aspiration_momentum.last_surfaced_at"),
            _cue()["at"],
        )

    def test_aiko_cue_first_person(self) -> None:
        host = _Host(cues=[_cue(subject="aiko", label="Growing steadier")])
        out = host._render_aspiration_momentum_block()
        self.assertIn("Growing steadier", out)
        self.assertIn("yourself", out)

    def test_disabled_returns_empty(self) -> None:
        host = _Host(cues=[_cue()], enabled=False)
        self.assertEqual(host._render_aspiration_momentum_block(), "")

    def test_empty_ring_silent(self) -> None:
        host = _Host(cues=[])
        self.assertEqual(host._render_aspiration_momentum_block(), "")

    def test_already_surfaced_is_silent(self) -> None:
        host = _Host(cues=[_cue()])
        host._chat_db.store["aspiration_momentum.last_surfaced_at"] = (
            _cue()["at"]
        )
        self.assertEqual(host._render_aspiration_momentum_block(), "")

    def test_force_next_bypasses_watermark(self) -> None:
        host = _Host(cues=[_cue()], force_next=True)
        host._chat_db.store["aspiration_momentum.last_surfaced_at"] = (
            _cue()["at"]
        )
        out = host._render_aspiration_momentum_block()
        self.assertIn("Building toward", out)
        self.assertFalse(host._aspiration_momentum_force_next)


# ── settings parse ───────────────────────────────────────────────────────


class SettingsParseTests(unittest.TestCase):
    def test_memory_settings_defaults_and_override(self) -> None:
        from app.core.infra.memory_settings import parse_memory_settings

        s = parse_memory_settings({})
        self.assertEqual(s.concept_synthesis_aspiration_min_chain, 3)
        self.assertAlmostEqual(
            s.concept_synthesis_aspiration_min_span_days, 14.0
        )
        self.assertAlmostEqual(s.aspiration_momentum_cooldown_days, 10.0)
        self.assertAlmostEqual(s.aspiration_momentum_min_confidence, 0.6)

        s2 = parse_memory_settings({
            "concept_synthesis_aspiration_min_span_days": 30,
            "aspiration_momentum_cooldown_days": 5,
        })
        self.assertAlmostEqual(
            s2.concept_synthesis_aspiration_min_span_days, 30.0
        )
        self.assertAlmostEqual(s2.aspiration_momentum_cooldown_days, 5.0)

    def test_agent_settings_flags(self) -> None:
        from app.core.infra.agent_settings_parse import parse_agent_settings

        a = parse_agent_settings({})
        self.assertTrue(a.aspiration_synthesis_enabled)
        self.assertTrue(a.aspiration_momentum_enabled)
        a2 = parse_agent_settings({"aspiration_momentum_enabled": False})
        self.assertFalse(a2.aspiration_momentum_enabled)


if __name__ == "__main__":
    unittest.main()
