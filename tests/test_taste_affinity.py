"""K81 taste intake: the affinity bar is relative to her own baseline.

The original bar was an absolute engaged rate (0.5) and L28m measured it
unreachable -- across 39 warmed clusters on the live ledger the best rate
was 0.32 and the median 0.20, so the pass could never mint a taste. These
tests pin the corrected reading: a cluster clears when it lands *better
than she generally does*, with the absolute value surviving only as a floor.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from app.core.concepts.concept_synthesis_worker import ConceptSynthesisWorker
from app.core.memory.surfacing_outcome_store import ClusterTaste


class _Ledger:
    def __init__(self, taste: dict[int, ClusterTaste]) -> None:
        self._taste = taste
        self.asked: list[tuple[int | None, int]] = []

    def engaged_rate_by_cluster(
        self, *, window_days: int | None, min_settled: int = 1,
    ) -> dict[int, ClusterTaste]:
        self.asked.append((window_days, min_settled))
        return dict(self._taste)


class _Cluster:
    def __init__(self, cid: int, summary: str) -> None:
        self.cluster_id = cid
        self.representative_id = cid * 10
        self.summary = summary
        self.size = 5


class _TopicGraph:
    def __init__(self, clusters: list[_Cluster]) -> None:
        self._clusters = clusters

    def topic_clusters(self) -> list[_Cluster]:
        return list(self._clusters)


class _Worker:
    """The slice of ConceptSynthesisWorker the taste pass touches."""

    _run_taste_pass = ConceptSynthesisWorker._run_taste_pass
    _taste_affinity_window_days = (
        ConceptSynthesisWorker._taste_affinity_window_days
    )
    _taste_min_settled = ConceptSynthesisWorker._taste_min_settled
    _taste_min_affinity = ConceptSynthesisWorker._taste_min_affinity
    _taste_affinity_baseline_multiple = (
        ConceptSynthesisWorker._taste_affinity_baseline_multiple
    )
    _taste_affinity_bar = ConceptSynthesisWorker._taste_affinity_bar
    _max_taste_clusters = ConceptSynthesisWorker._max_taste_clusters
    # Re-wrapped: reading a staticmethod off the class yields a plain
    # function, which would rebind as an instance method on this double.
    _affinity_phrase = staticmethod(ConceptSynthesisWorker._affinity_phrase)
    _taste_fingerprint = staticmethod(
        ConceptSynthesisWorker._taste_fingerprint
    )

    def __init__(
        self,
        taste: dict[int, ClusterTaste],
        *,
        floor: float = 0.15,
        multiple: float = 1.4,
        enabled: bool = True,
    ) -> None:
        self._agent_settings = SimpleNamespace(taste_synthesis_enabled=enabled)
        self._memory_settings = SimpleNamespace(
            taste_affinity_window_days=90,
            taste_min_settled=4,
            taste_min_affinity=floor,
            taste_affinity_baseline_multiple=multiple,
            concept_synthesis_max_taste_clusters=6,
        )
        self._ledger = _Ledger(taste)
        self._surfacing_outcome_store_provider = lambda: self._ledger
        self._topic_graph = _TopicGraph(
            [_Cluster(cid, f"topic {cid}") for cid in taste]
        )
        self._sigs: dict[str, str] = {}
        self.offered: list[dict[int, str]] = []

    def _load_sigs(self, key: str) -> dict[str, Any]:
        raw = self._sigs.get(key)
        return json.loads(raw) if raw else {}

    def _save_sigs(self, key: str, blob: dict[str, Any]) -> None:
        self._sigs[key] = json.dumps(blob)

    def _existing_for(self, spec: Any, **_kw: Any) -> list:
        return []

    def _memory_content(self, _rep: int) -> str:
        return "a representative memory"

    def _digest_for_rep(self, _rep: int) -> str:
        return ""

    def _propose(self, _ctx: Any, *, affinity_by_rep: Any, **_kw: Any) -> list:
        self.offered.append(dict(affinity_by_rep))
        return [f"proposal-{len(affinity_by_rep)}"]

    def spec(self) -> Any:
        return SimpleNamespace(
            kind="taste",
            subject="aiko",
            sig_key="concept_synth.taste_sig.aiko",
            propose=self._propose,
        )


def _snapshot() -> dict[int, ClusterTaste]:
    """A ledger shaped like the live one: nothing near 0.5, one standout."""
    return {
        1: ClusterTaste(1, surfaced=200, settled=180, engaged=58),   # 0.322
        2: ClusterTaste(2, surfaced=260, settled=240, engaged=48),   # 0.200
        3: ClusterTaste(3, surfaced=150, settled=140, engaged=21),   # 0.150
        4: ClusterTaste(4, surfaced=100, settled=90, engaged=9),     # 0.100
    }


class BarTests(unittest.TestCase):
    def test_the_bar_sits_above_the_pooled_baseline(self) -> None:
        worker = _Worker(_snapshot())
        bar, baseline = worker._taste_affinity_bar(_snapshot())
        # 136 engaged / 650 settled = 0.209; x 1.4 = 0.293.
        self.assertAlmostEqual(baseline, 136 / 650, places=4)
        self.assertAlmostEqual(bar, baseline * 1.4, places=4)

    def test_only_the_standout_clears_it(self) -> None:
        worker = _Worker(_snapshot())
        bar, _baseline = worker._taste_affinity_bar(_snapshot())
        cleared = [
            cid
            for cid, ct in _snapshot().items()
            if (ct.engaged_rate or 0.0) >= bar
        ]
        self.assertEqual(cleared, [1])

    def test_the_absolute_floor_binds_when_nothing_lands(self) -> None:
        # A relationship where engagement is uniformly near zero must not
        # mint taste from noise: the floor, not the multiple, decides.
        flat = {
            1: ClusterTaste(1, surfaced=100, settled=100, engaged=3),
            2: ClusterTaste(2, surfaced=100, settled=100, engaged=1),
        }
        worker = _Worker(flat)
        bar, baseline = worker._taste_affinity_bar(flat)
        self.assertLess(baseline * 1.4, 0.15)
        self.assertAlmostEqual(bar, 0.15, places=4)

    def test_the_bar_never_exceeds_one(self) -> None:
        perfect = {1: ClusterTaste(1, surfaced=10, settled=10, engaged=10)}
        worker = _Worker(perfect)
        bar, _baseline = worker._taste_affinity_bar(perfect)
        self.assertLessEqual(bar, 1.0)


class PhraseTests(unittest.TestCase):
    def test_the_phrase_carries_the_baseline_for_comparison(self) -> None:
        phrase = _Worker(_snapshot())._affinity_phrase(
            ClusterTaste(1, surfaced=200, settled=180, engaged=58), 0.209,
        )
        self.assertIn("32% engaged over 180 turns", phrase)
        self.assertIn("21% typical", phrase)

    def test_an_unknown_baseline_is_simply_omitted(self) -> None:
        phrase = _Worker(_snapshot())._affinity_phrase(
            ClusterTaste(1, surfaced=200, settled=180, engaged=58),
        )
        self.assertNotIn("typical", phrase)


class PassTests(unittest.TestCase):
    def test_a_live_shaped_ledger_now_offers_its_standout(self) -> None:
        worker = _Worker(_snapshot())
        stats: dict[str, Any] = {}
        out = worker._run_taste_pass(None, worker.spec(), stats)
        self.assertTrue(out)
        self.assertTrue(stats["taste_dirty"])
        # Cluster 1's representative id, and only it.
        self.assertEqual(list(worker.offered[0]), [10])

    def test_the_old_absolute_bar_would_have_refused_everything(self) -> None:
        # The regression this retune exists for: with the bar read as an
        # absolute 0.5 the same ledger yields nothing at all.
        worker = _Worker(_snapshot(), floor=0.5, multiple=1.0)
        stats: dict[str, Any] = {}
        self.assertEqual(
            worker._run_taste_pass(None, worker.spec(), stats), []
        )
        self.assertFalse(stats["taste_dirty"])
        self.assertEqual(worker.offered, [])

    def test_the_offered_annotation_names_the_baseline(self) -> None:
        worker = _Worker(_snapshot())
        worker._run_taste_pass(None, worker.spec(), {})
        self.assertIn("typical", worker.offered[0][10])

    def test_the_switch_still_skips_the_pass(self) -> None:
        worker = _Worker(_snapshot(), enabled=False)
        self.assertEqual(worker._run_taste_pass(None, worker.spec(), {}), [])
        self.assertEqual(worker.offered, [])


if __name__ == "__main__":
    unittest.main()
