"""Tests for K26 -- Aiko-side voice evolution.

Two layers: the pure promotion rule
(:mod:`app.core.relationship.voice_adoption`) and the worker that drives
it off the catchphrase registry
(:class:`~app.core.proactive.voice_adoption_worker.VoiceAdoptionWorker`).

The mechanic is deliberately measured in weeks, so almost every test here
is really about a *refusal*: too young, too soon after the last one, too
many already, or provenance unknown. The one thing K26 must never do is
have Aiko "adopt" a phrase that was hers to begin with.
"""
from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

from app.core.proactive.voice_adoption_worker import VoiceAdoptionWorker
from app.core.relationship import voice_adoption as va


_NOW = datetime(2026, 7, 1, 12, 0, tzinfo=timezone.utc)


def _cand(phrase: str, *, age_days: float, salience: float = 0.5):
    return va.AdoptionCandidate(
        phrase=phrase,
        first_seen=_NOW - timedelta(days=age_days),
        salience=salience,
    )


# ── the pure rule ──────────────────────────────────────────────────────


class EligibleTests(unittest.TestCase):
    def test_young_phrases_are_not_eligible(self) -> None:
        """A phrase from one intense evening is a mood, not a habit."""
        out = va.eligible_candidates(
            [_cand("that's cursed", age_days=3)], now=_NOW, min_age_days=14,
        )
        self.assertEqual(out, [])

    def test_old_enough_is_eligible(self) -> None:
        out = va.eligible_candidates(
            [_cand("that's cursed", age_days=30)], now=_NOW, min_age_days=14,
        )
        self.assertEqual([c.phrase for c in out], ["that's cursed"])

    def test_already_adopted_is_skipped(self) -> None:
        out = va.eligible_candidates(
            [_cand("that's cursed", age_days=30)],
            adopted=[{"phrase": "That's Cursed"}],
            now=_NOW,
            min_age_days=14,
        )
        self.assertEqual(out, [])

    def test_oldest_ranks_first(self) -> None:
        out = va.eligible_candidates(
            [
                _cand("newer thing", age_days=20, salience=0.9),
                _cand("older thing", age_days=60, salience=0.2),
            ],
            now=_NOW,
            min_age_days=14,
        )
        self.assertEqual([c.phrase for c in out][0], "older thing")


class PromoteTests(unittest.TestCase):
    def test_adopts_the_top_candidate(self) -> None:
        rows, new = va.promote(
            [], [_cand("fair enough", age_days=30)], now=_NOW,
        )
        self.assertEqual(new, "fair enough")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["adopted_at"], _NOW.isoformat())

    def test_only_one_per_run(self) -> None:
        """Takes the head of the already-ranked list, and only that."""
        rows, new = va.promote(
            [],
            [_cand("first in line", age_days=40), _cand("also old", age_days=30)],
            now=_NOW,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(new, "first in line")

    def test_recent_adoption_blocks_the_next(self) -> None:
        recent = [
            {
                "phrase": "fair enough",
                "adopted_at": (_NOW - timedelta(days=2)).isoformat(),
            }
        ]
        rows, new = va.promote(
            recent,
            [_cand("that's cursed", age_days=30)],
            now=_NOW,
            min_days_between=10,
        )
        self.assertIsNone(new)
        self.assertEqual(len(rows), 1)

    def test_spacing_satisfied_allows_the_next(self) -> None:
        old = [
            {
                "phrase": "fair enough",
                "adopted_at": (_NOW - timedelta(days=30)).isoformat(),
            }
        ]
        _rows, new = va.promote(
            old,
            [_cand("that's cursed", age_days=30)],
            now=_NOW,
            min_days_between=10,
        )
        self.assertEqual(new, "that's cursed")

    def test_full_set_refuses(self) -> None:
        full = [
            {"phrase": f"p{i}", "adopted_at": (_NOW - timedelta(days=99 + i)).isoformat()}
            for i in range(3)
        ]
        _rows, new = va.promote(
            full, [_cand("that's cursed", age_days=30)], now=_NOW,
            max_adopted=3,
        )
        self.assertIsNone(new)

    def test_no_candidates_is_a_no_op(self) -> None:
        rows, new = va.promote([], [], now=_NOW)
        self.assertEqual((rows, new), ([], None))


class RetireTests(unittest.TestCase):
    def test_phrase_gone_from_the_registry_is_dropped(self) -> None:
        kept, gone = va.retire(
            [{"phrase": "fair enough"}, {"phrase": "that's cursed"}],
            ["Fair Enough"],
        )
        self.assertEqual([r["phrase"] for r in kept], ["fair enough"])
        self.assertEqual(gone, ["that's cursed"])

    def test_empty_registry_retires_everything(self) -> None:
        kept, gone = va.retire([{"phrase": "fair enough"}], [])
        self.assertEqual(kept, [])
        self.assertEqual(gone, ["fair enough"])


class RenderTests(unittest.TestCase):
    def test_nothing_adopted_renders_nothing(self) -> None:
        self.assertEqual(va.render_block([]), "")

    def test_names_the_newest_first_and_caps(self) -> None:
        adopted = [
            {"phrase": "oldest", "adopted_at": "2026-01-01T00:00:00+00:00"},
            {"phrase": "middle", "adopted_at": "2026-03-01T00:00:00+00:00"},
            {"phrase": "newest", "adopted_at": "2026-06-01T00:00:00+00:00"},
        ]
        block = va.render_block(
            adopted, user_display_name="Sam", max_phrases=2,
        )
        self.assertIn('"newest"', block)
        self.assertIn('"middle"', block)
        self.assertNotIn("oldest", block)
        self.assertIn("Sam", block)

    def test_copy_forbids_forcing_and_pointing_it_out(self) -> None:
        block = va.render_block([{"phrase": "fair enough"}])
        self.assertIn("never force one in", block)
        self.assertIn("never point out", block)


class StateTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        store: dict[str, str] = {}
        va.save_state(store.__setitem__, [{"phrase": "fair enough"}])
        self.assertEqual(
            va.load_state(store.get), [{"phrase": "fair enough"}],
        )

    def test_garbage_reads_as_empty(self) -> None:
        self.assertEqual(va.load_state(lambda k: "{{not json"), [])
        self.assertEqual(va.load_state(lambda k: json.dumps({"a": 1})), [])


# ── the worker ─────────────────────────────────────────────────────────


class _Kv:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def kv_get(self, key: str) -> str | None:
        return self.store.get(key)

    def kv_set(self, key: str, value: str) -> None:
        self.store[key] = value


class _Store:
    def __init__(self, mems: list[Any]) -> None:
        self.mems = mems

    def list_recent(self, *, limit: int = 128, kind: str | None = None):
        return [
            m for m in self.mems if kind is None or m.kind == kind
        ][:limit]


def _mem(
    phrase: str,
    *,
    age_days: float,
    origin: str | None = "user",
    salience: float = 0.5,
) -> SimpleNamespace:
    return SimpleNamespace(
        content=phrase,
        kind="catchphrase",
        salience=salience,
        created_at=(_NOW - timedelta(days=age_days)).isoformat(),
        metadata={"origin": origin} if origin else {},
    )


def _worker(
    mems: list[Any],
    *,
    kv: _Kv | None = None,
    enabled: bool = True,
    origin_resolver: Any = None,
    **over: Any,
) -> tuple[VoiceAdoptionWorker, _Kv]:
    kv = kv or _Kv()
    worker = VoiceAdoptionWorker(
        chat_db=kv,
        memory_store=_Store(mems),
        enabled_provider=lambda: enabled,
        origin_resolver=origin_resolver,
        clock=lambda: _NOW,
        **over,
    )
    return worker, kv


class WorkerTests(unittest.TestCase):
    def test_adopts_an_old_phrase_of_his(self) -> None:
        worker, kv = _worker([_mem("fair enough", age_days=40)])
        result = worker.run()
        self.assertEqual(result["adopted"], "fair enough")
        self.assertEqual(
            [r["phrase"] for r in va.load_state(kv.kv_get)], ["fair enough"],
        )

    def test_never_adopts_her_own_phrase(self) -> None:
        """The one failure mode that reads as broken."""
        worker, kv = _worker(
            [_mem("fair enough", age_days=40, origin="assistant")],
        )
        self.assertIsNone(worker.run()["adopted"])
        self.assertEqual(va.load_state(kv.kv_get), [])

    def test_unknown_provenance_is_not_a_guess(self) -> None:
        worker, _kv = _worker([_mem("fair enough", age_days=40, origin=None)])
        self.assertIsNone(worker.run()["adopted"])

    def test_origin_resolver_backfills_legacy_rows(self) -> None:
        worker, _kv = _worker(
            [_mem("fair enough", age_days=40, origin=None)],
            origin_resolver=lambda phrase: "user",
        )
        self.assertEqual(worker.run()["adopted"], "fair enough")

    def test_resolver_failure_is_survivable(self) -> None:
        def boom(phrase: str) -> str:
            raise RuntimeError("db gone")

        worker, _kv = _worker(
            [_mem("fair enough", age_days=40, origin=None)],
            origin_resolver=boom,
        )
        self.assertIsNone(worker.run()["adopted"])

    def test_young_phrase_waits(self) -> None:
        worker, _kv = _worker([_mem("fair enough", age_days=2)])
        self.assertIsNone(worker.run()["adopted"])

    def test_force_next_drops_the_time_gates_only(self) -> None:
        worker, _kv = _worker([_mem("fair enough", age_days=2)])
        worker.force_next()
        self.assertEqual(worker.run()["adopted"], "fair enough")

        worker2, _kv2 = _worker(
            [_mem("fair enough", age_days=2, origin="assistant")],
        )
        worker2.force_next()
        self.assertIsNone(worker2.run()["adopted"])

    def test_retires_a_phrase_that_left_the_registry(self) -> None:
        kv = _Kv()
        va.save_state(
            kv.kv_set,
            [{"phrase": "gone phrase", "adopted_at": _NOW.isoformat()}],
        )
        worker, kv = _worker([], kv=kv)
        result = worker.run()
        self.assertEqual(result["retired"], ["gone phrase"])
        self.assertEqual(va.load_state(kv.kv_get), [])

    def test_disabled_is_a_no_op(self) -> None:
        worker, kv = _worker(
            [_mem("fair enough", age_days=40)], enabled=False,
        )
        self.assertTrue(worker.run()["disabled"])
        self.assertEqual(kv.store, {})

    def test_quiet_run_does_not_rewrite_the_store(self) -> None:
        worker, kv = _worker([_mem("fair enough", age_days=2)])
        worker.run()
        self.assertEqual(kv.store, {})

    def test_interval_has_a_floor(self) -> None:
        worker, _kv = _worker([], interval_seconds=1)
        self.assertGreaterEqual(worker.interval_seconds, 60.0)

    def test_is_ready_follows_the_switch(self) -> None:
        worker, _kv = _worker([], enabled=False)
        self.assertFalse(worker.is_ready(now=_NOW, last_run_at=None))
        worker2, _kv2 = _worker([])
        self.assertTrue(worker2.is_ready(now=_NOW, last_run_at=None))

    def test_timing_is_no_longer_a_veto(self) -> None:
        worker, _kv = _worker([])
        self.assertTrue(
            worker.is_ready(
                now=_NOW, last_run_at=_NOW - timedelta(seconds=30),
            )
        )


class PromotionBlockedTests(unittest.TestCase):
    """The state-only half of ``promote``, split out for the probe."""

    def test_an_empty_state_is_never_blocked(self) -> None:
        self.assertIsNone(va.promotion_blocked([], now=_NOW))

    def test_a_full_set_is_blocked_by_the_ceiling(self) -> None:
        rows = [
            {"phrase": f"p{i}", "adopted_at": (
                _NOW - timedelta(days=400)
            ).isoformat()}
            for i in range(3)
        ]
        self.assertEqual(
            va.promotion_blocked(rows, now=_NOW, max_adopted=3), "at ceiling",
        )

    def test_a_recent_adoption_is_blocked_by_spacing(self) -> None:
        rows = [{
            "phrase": "p", "adopted_at": (_NOW - timedelta(days=2)).isoformat(),
        }]
        self.assertEqual(
            va.promotion_blocked(rows, now=_NOW, min_days_between=10.0),
            "spacing",
        )

    def test_an_elapsed_gap_clears(self) -> None:
        rows = [{
            "phrase": "p",
            "adopted_at": (_NOW - timedelta(days=30)).isoformat(),
        }]
        self.assertIsNone(
            va.promotion_blocked(rows, now=_NOW, min_days_between=10.0)
        )


class WorkerDemandTests(unittest.TestCase):
    """Cheap spacing gate first; registry scan only once it clears."""

    def _probe(self, worker: VoiceAdoptionWorker):
        return worker.demand(now=_NOW, last_run_at=None)

    def test_an_adoptable_phrase_is_full_pressure(self) -> None:
        worker, _kv = _worker([_mem("fair enough", age_days=40)])
        signal = self._probe(worker)
        self.assertEqual(signal.pressure, 1.0)
        self.assertEqual(signal.reason, "1 adoptable")
        self.assertFalse(signal.needs_llm)

    def test_an_empty_registry_asks_for_nothing(self) -> None:
        worker, _kv = _worker([])
        signal = self._probe(worker)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "nothing adoptable")

    def test_her_own_phrases_are_not_adoptable(self) -> None:
        worker, _kv = _worker(
            [_mem("fair enough", age_days=40, origin="assistant")],
        )
        self.assertEqual(self._probe(worker).pressure, 0.0)

    def test_a_young_phrase_is_not_adoptable(self) -> None:
        worker, _kv = _worker([_mem("fair enough", age_days=2)])
        self.assertEqual(self._probe(worker).pressure, 0.0)

    def test_the_spacing_gate_short_circuits_the_scan(self) -> None:
        # The whole point of the cheap gate: for the ten days after an
        # adoption the probe must not touch the registry at all.
        kv = _Kv()
        va.save_state(
            kv.kv_set,
            [{
                "phrase": "already mine",
                "adopted_at": (_NOW - timedelta(days=1)).isoformat(),
            }],
        )
        scans: list[int] = []

        class _CountingStore(_Store):
            def list_recent(self, *, limit: int = 128, kind=None):
                scans.append(1)
                return super().list_recent(limit=limit, kind=kind)

        worker = VoiceAdoptionWorker(
            chat_db=kv,
            memory_store=_CountingStore([_mem("fair enough", age_days=40)]),
            clock=lambda: _NOW,
        )
        signal = self._probe(worker)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "spacing")
        self.assertEqual(scans, [])

    def test_the_probe_skips_the_origin_resolver(self) -> None:
        # Resolving a legacy row's provenance is a message-history
        # lookup per phrase -- run-shaped work. The probe undercounts
        # those rather than paying for them; the daily heartbeat run
        # still adopts them.
        calls: list[str] = []

        def resolver(phrase: str) -> str:
            calls.append(phrase)
            return "user"

        worker, _kv = _worker(
            [_mem("fair enough", age_days=40, origin=None)],
            origin_resolver=resolver,
        )
        signal = self._probe(worker)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(calls, [])
        # ...but the run still adopts it.
        self.assertEqual(worker.run()["adopted"], "fair enough")
        self.assertEqual(calls, ["fair enough"])

    def test_disabled_and_storeless_report_no_pressure(self) -> None:
        off, _kv = _worker([_mem("fair enough", age_days=40)], enabled=False)
        self.assertEqual(self._probe(off).reason, "disabled")
        storeless = VoiceAdoptionWorker(
            chat_db=_Kv(), memory_store=None, clock=lambda: _NOW,
        )
        self.assertEqual(self._probe(storeless).reason, "no store")

    def test_probing_never_adopts(self) -> None:
        worker, kv = _worker([_mem("fair enough", age_days=40)])
        self._probe(worker)
        self._probe(worker)
        self.assertEqual(kv.store, {})

    def test_probing_does_not_eat_the_force_flag(self) -> None:
        worker, _kv = _worker([_mem("fair enough", age_days=2)])
        worker.force_next()
        self.assertEqual(self._probe(worker).pressure, 0.0)
        self.assertEqual(worker.run()["adopted"], "fair enough")


if __name__ == "__main__":
    unittest.main()
