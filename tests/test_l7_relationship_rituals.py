"""Tests for L7 relationship (ritual) concepts.

Covers the full vertical slice:

* the pure ``ritual_grouping`` helper (single-link cosine grouping, min-size
  floor, dominant-vibe + weekday annotation, ``moment_from_memory``),
* the ``ritual_evidence_gate`` floors,
* the ``ritual`` kind registration + relevance-only routing,
* the ``_run_ritual_pass`` worker pass (creation, min-moments floor,
  dirty-tracking, the ``ritual_synthesis_enabled`` switch),
* the ``relationship_ritual`` proposer (group annotation in the prompt,
  min-sources, cold-start, reinforce-by-id),
* the rendering header.
"""
from __future__ import annotations

import types
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.concepts import ritual_grouping as rg
from app.core.concepts.concept_kinds import (
    core_lane_kinds,
    get_kind,
    kinds_for_target,
)
from app.core.concepts.concept_lifecycle import ritual_evidence_gate
from app.core.concepts.proposers import ProposerContext
from app.core.concepts.proposers.relationship_ritual import (
    propose_relationship_ritual,
)

from tests.test_concept_synthesis_worker import (
    MemStub,
    WorkerHarness,
)


_UTC = timezone.utc


def _vec(*xs) -> np.ndarray:
    return np.asarray(xs, dtype=np.float32)


# ── ritual_grouping (pure) ──────────────────────────────────────────────


class RitualGroupingTests(unittest.TestCase):
    def test_groups_similar_and_drops_small_components(self) -> None:
        base = datetime(2026, 1, 2, 20, 0, tzinfo=_UTC)  # a fixed weekday
        friday = base.strftime("%A")
        moments = [
            rg.MomentInput(
                id=10 + i, embedding=_vec(1.0, 0.0, 0.0),
                text=f"late debugging session {i}", vibe="playful",
                when=(base + timedelta(days=7 * i)).isoformat(),
                salience=0.5 + i * 0.1,
            )
            for i in range(3)
        ]
        # Two unrelated moments (orthogonal) -> a size-2 component, excluded.
        moments += [
            rg.MomentInput(
                id=90 + i, embedding=_vec(0.0, 1.0, 0.0),
                text="unrelated cozy evening", vibe="warm",
                when=base.isoformat(), salience=0.4,
            )
            for i in range(2)
        ]
        groups = rg.group_moments(moments, min_size=3, similarity=0.6)
        self.assertEqual(len(groups), 1)
        g = groups[0]
        self.assertEqual(g.size, 3)
        self.assertEqual(set(g.member_ids), {10, 11, 12})
        self.assertEqual(g.dominant_vibe, "playful")
        self.assertEqual(g.weekday_hint, friday)

    def test_min_size_floor(self) -> None:
        moments = [
            rg.MomentInput(id=i, embedding=_vec(1.0, 0.0), text="x",
                           vibe="warm", when="")
            for i in range(2)
        ]
        self.assertEqual(rg.group_moments(moments, min_size=3), [])

    def test_no_weekday_hint_when_scattered(self) -> None:
        base = datetime(2026, 1, 1, 12, 0, tzinfo=_UTC)
        moments = [
            rg.MomentInput(
                id=i, embedding=_vec(1.0, 0.0, 0.0), text="t", vibe="general",
                # consecutive days -> different weekdays, no majority
                when=(base + timedelta(days=i)).isoformat(),
            )
            for i in range(3)
        ]
        groups = rg.group_moments(moments, min_size=3, similarity=0.6)
        self.assertEqual(len(groups), 1)
        self.assertIsNone(groups[0].weekday_hint)
        # No meaningful vibe -> falls back to "general".
        self.assertEqual(groups[0].dominant_vibe, "general")

    def test_moment_from_memory_reads_metadata(self) -> None:
        mem = MemStub(
            5, "Shared moment (playful): cracked the bug at 2am",
            "shared_moment", 0.8,
            metadata={"vibe": "playful", "when": "2026-01-02T20:00:00+00:00",
                      "what": "cracked the bug at 2am"},
            embedding=_vec(1.0, 0.0),
        )
        mi = rg.moment_from_memory(mem)
        self.assertIsNotNone(mi)
        self.assertEqual(mi.id, 5)
        self.assertEqual(mi.vibe, "playful")
        self.assertEqual(mi.text, "cracked the bug at 2am")
        self.assertEqual(mi.when, "2026-01-02T20:00:00+00:00")

    def test_moment_from_memory_none_without_embedding(self) -> None:
        mem = MemStub(6, "text", "shared_moment", 0.5,
                      metadata={"what": "text"}, embedding=None)
        self.assertIsNone(rg.moment_from_memory(mem))


# ── gate + registry ─────────────────────────────────────────────────────


class RitualGateAndRegistryTests(unittest.TestCase):
    def test_gate_floors(self) -> None:
        self.assertFalse(
            ritual_evidence_gate(
                distinct_source_count=2, age_days=10.0, confidence=0.99,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )  # only 2 sources < floor 3
        self.assertFalse(
            ritual_evidence_gate(
                distinct_source_count=3, age_days=0.5, confidence=0.99,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )  # age below 1.0 floor
        self.assertFalse(
            ritual_evidence_gate(
                distinct_source_count=3, age_days=2.0, confidence=0.5,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )  # confidence below 0.65 floor
        self.assertTrue(
            ritual_evidence_gate(
                distinct_source_count=3, age_days=1.0, confidence=0.65,
                min_sources=1, min_age_days=0.0, min_confidence=0.0,
            )
        )

    def test_registry(self) -> None:
        kind = get_kind("ritual")
        self.assertIsNotNone(kind)
        self.assertEqual(kind.subject, "relationship")
        self.assertEqual(kind.evidence_model, "set")
        self.assertAlmostEqual(kind.plasticity_default, 0.4)
        self.assertFalse(kind.core_always_on)
        self.assertEqual(kind.surfacing_targets, {})
        self.assertIs(kind.promotion_gate, ritual_evidence_gate)

    def test_relevance_only_routing(self) -> None:
        self.assertNotIn("ritual", kinds_for_target("profile_block"))
        self.assertNotIn("ritual", {k.name for k in core_lane_kinds()})


# ── worker ritual pass ──────────────────────────────────────────────────


def _ritual_responder(system, user):
    if "RELATIONSHIP RITUALS between" in system:
        return {"concepts": [{
            "label": "Friday late-night debugging sessions",
            "group_index": 0,
            "rationale": "recurring late-Friday bug hunts",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


def _moment_rows(n: int, *, start_id: int = 500) -> list[MemStub]:
    base = datetime(2026, 1, 2, 20, 0, tzinfo=_UTC)
    return [
        MemStub(
            start_id + i,
            f"Shared moment (playful): debugging night {i}",
            "shared_moment",
            0.5 + i * 0.05,
            metadata={
                "vibe": "playful",
                "when": (base + timedelta(days=7 * i)).isoformat(),
                "what": f"debugging night {i}",
            },
            embedding=_vec(1.0, 0.0, 0.0),
        )
        for i in range(n)
    ]


class RitualPassTests(unittest.TestCase):
    def test_creates_ritual_concept_with_memory_edges(self) -> None:
        rows = _moment_rows(6)
        h = WorkerHarness(
            _ritual_responder, clusters=[], self_memories=[],
            shared_moments=rows,
        )
        stats = h.worker.run()
        self.assertTrue(stats["ritual_dirty"])
        out = h.store.list_by(subject="relationship", kind="ritual")
        self.assertEqual(len(out), 1)
        c = out[0]
        self.assertEqual(c.label, "Friday late-night debugging sessions")
        self.assertEqual(c.evidence_model, "set")
        ev = h.store.evidence_of(c.concept_id)
        self.assertTrue(all(e.src_type == "memory" for e in ev))
        self.assertEqual(
            {e.src_id for e in ev}, {str(m.id) for m in rows}
        )

    def test_min_moments_floor_skips_pass(self) -> None:
        rows = _moment_rows(3)  # below the default floor (6)
        h = WorkerHarness(
            _ritual_responder, clusters=[], self_memories=[],
            shared_moments=rows,
        )
        stats = h.worker.run()
        self.assertFalse(stats["ritual_dirty"])
        self.assertEqual(
            h.store.list_by(subject="relationship", kind="ritual"), []
        )

    def test_clean_rerun_is_noop(self) -> None:
        rows = _moment_rows(6)
        h = WorkerHarness(
            _ritual_responder, clusters=[], self_memories=[],
            shared_moments=rows,
        )
        h.worker.run()
        calls = h.ollama.calls
        before = h.store.count()
        stats = h.worker.run()
        self.assertFalse(stats["ritual_dirty"])
        self.assertEqual(h.ollama.calls, calls)  # ritual pass short-circuits
        self.assertEqual(h.store.count(), before)

    def test_new_moment_refires_pass(self) -> None:
        rows = _moment_rows(6)
        h = WorkerHarness(
            _ritual_responder, clusters=[], self_memories=[],
            shared_moments=rows,
        )
        h.worker.run()
        self.assertFalse(h.worker.run()["ritual_dirty"])  # clean
        extra = _moment_rows(1, start_id=700)
        h.mem._extra.extend(extra)
        h.mem._by_id.update({m.id: m for m in extra})
        self.assertTrue(h.worker.run()["ritual_dirty"])

    def test_disabled_switch_skips_pass(self) -> None:
        rows = _moment_rows(6)
        agent = types.SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            ritual_synthesis_enabled=False,
        )
        h = WorkerHarness(
            _ritual_responder, clusters=[], self_memories=[],
            shared_moments=rows, agent=agent,
        )
        stats = h.worker.run()
        self.assertFalse(stats["ritual_dirty"])
        self.assertEqual(
            h.store.list_by(subject="relationship", kind="ritual"), []
        )


# ── proposer (direct) ───────────────────────────────────────────────────


def _ctx(responder):
    calls: dict[str, str] = {}

    def call_llm(system, user):
        calls["system"] = system
        calls["user"] = user
        return responder(system, user)["concepts"]

    ctx = ProposerContext(call_llm=call_llm, user_name="Jacob",
                          assistant_name="Aiko")
    return ctx, calls


def _group(member_ids, *, vibe="playful", weekday="Friday"):
    members = tuple(
        rg.MomentLite(id=mid, text=f"moment {mid}", vibe=vibe, weekday=weekday)
        for mid in member_ids
    )
    return rg.RitualGroup(
        member_ids=tuple(member_ids),
        dominant_vibe=vibe,
        weekday_hint=weekday,
        members=members,
    )


class ProposerTests(unittest.TestCase):
    def test_prompt_shows_group_annotation(self) -> None:
        ctx, calls = _ctx(_ritual_responder)
        out = propose_relationship_ritual(ctx, groups=[_group([11, 12, 13])])
        self.assertIn("vibe: playful", calls["user"])
        self.assertIn("usually Friday", calls["user"])
        self.assertIn("[11]", calls["user"])
        self.assertIn("Jacob", calls["user"])
        self.assertIn("Aiko", calls["user"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "ritual")
        self.assertEqual(out[0].subject, "relationship")
        self.assertEqual(
            {i for _t, i in out[0].evidence}, {"11", "12", "13"}
        )

    def test_min_sources_rejects_thin_group(self) -> None:
        def responder(system, user):
            return {"concepts": [{
                "label": "a one-off thing",
                "group_index": 0,
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_relationship_ritual(ctx, groups=[_group([11])])
        self.assertEqual(out, [])  # < min_sources (2)

    def test_cold_start_no_groups(self) -> None:
        ctx, _ = _ctx(_ritual_responder)
        self.assertEqual(propose_relationship_ritual(ctx, groups=[]), [])

    def test_reinforce_by_id(self) -> None:
        from app.core.concepts.proposers import ExistingConcept

        def responder(system, user):
            return {"concepts": [{
                "reinforces_id": 42,
                "group_index": 0,
                "rationale": "more of the same ritual",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_relationship_ritual(
            ctx, groups=[_group([11, 12, 13])],
            existing=[ExistingConcept(id=42, label="Friday debugging")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")


# ── rendering header ────────────────────────────────────────────────────


class RenderingTests(unittest.TestCase):
    def test_ritual_header_dispatch(self) -> None:
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        rel = InnerLifePart1Mixin._concept_group_header(
            "relationship", "ritual", "Jacob"
        )
        self.assertIn("Rituals", rel)
        self.assertIn("Jacob", rel)
        aiko = InnerLifePart1Mixin._concept_group_header(
            "aiko", "ritual", "Jacob"
        )
        self.assertIn("Rituals", aiko)


if __name__ == "__main__":
    unittest.main()
