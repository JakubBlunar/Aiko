"""Tests for H12 shared values (kind=value, subject=relationship).

The pair had 30 concepts and no answer to "what are we for": 25 tension,
4 ritual, 1 narrative. This proposer reads the same ``shared_moment``
groups L7 does and asks for the commitment underneath, so most of what is
tested here is the line between the two -- a value that shows up in only
one recurring activity is that activity, named twice.
"""
from __future__ import annotations

import types
import unittest
from datetime import datetime, timedelta, timezone

import numpy as np

from app.core.concepts import ritual_grouping as rg
from app.core.concepts.proposers import CONCEPT_PROPOSERS, ProposerContext
from app.core.concepts.proposers.value_relationship import (
    propose_value_relationship,
)

from tests.test_concept_synthesis_worker import MemStub, WorkerHarness

_UTC = timezone.utc


def _vec(*xs) -> np.ndarray:
    return np.asarray(xs, dtype=np.float32)


def _group(member_ids, *, vibe="tender"):
    members = tuple(
        rg.MomentLite(id=mid, text=f"moment {mid}", vibe=vibe, weekday=None)
        for mid in member_ids
    )
    return rg.RitualGroup(
        member_ids=tuple(member_ids),
        dominant_vibe=vibe,
        weekday_hint=None,
        members=members,
    )


def _ctx(responder):
    calls: dict[str, str] = {}

    def call_llm(system, user):
        calls["system"] = system
        calls["user"] = user
        return responder(system, user)["concepts"]

    return ProposerContext(
        call_llm=call_llm, user_name="Jacob", assistant_name="Aiko"
    ), calls


def _value_responder(system, user):
    if "SHARED VALUES between" in system:
        return {"concepts": [{
            "label": "they say the awkward thing rather than smoothing it",
            "evidence_memory_ids": [11, 12, 21],
            "rationale": "the hard conversations recur across both stretches",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


class ProposerTests(unittest.TestCase):
    def test_a_value_spanning_two_groups_is_proposed(self) -> None:
        ctx, calls = _ctx(_value_responder)
        out = propose_value_relationship(
            ctx, groups=[_group([11, 12, 13]), _group([21, 22, 23])]
        )
        self.assertIn("GROUP [0]", calls["user"])
        self.assertIn("GROUP [1]", calls["user"])
        self.assertIn("Jacob", calls["user"])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "value")
        self.assertEqual(out[0].subject, "relationship")
        self.assertEqual(out[0].evidence_model, "set")
        self.assertEqual(
            {i for _t, i in out[0].evidence}, {"11", "12", "21"}
        )

    def test_a_value_inside_one_group_is_the_ritual_again(self) -> None:
        # The whole reason this proposer can coexist with L7: a principle
        # only visible in one recurring activity is that activity.
        def responder(_system, _user):
            return {"concepts": [{
                "label": "they wind down together",
                "evidence_memory_ids": [11, 12, 13],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_value_relationship(
            ctx, groups=[_group([11, 12, 13]), _group([21, 22, 23])]
        )
        self.assertEqual(out, [])

    def test_one_group_is_not_enough_to_ask(self) -> None:
        # Nothing can span two groups when there is only one, so the call
        # is skipped rather than made and rejected.
        called = []

        def responder(_system, _user):
            called.append(1)
            return {"concepts": []}

        ctx, _ = _ctx(responder)
        self.assertEqual(
            propose_value_relationship(ctx, groups=[_group([11, 12, 13])]), []
        )
        self.assertEqual(called, [])

    def test_invented_moment_ids_are_dropped(self) -> None:
        def responder(_system, _user):
            return {"concepts": [{
                "label": "they protect each other's quiet",
                "evidence_memory_ids": [11, 21, 999],
                "confidence": 0.8,
            }]}

        ctx, _ = _ctx(responder)
        out = propose_value_relationship(
            ctx, groups=[_group([11, 12, 13]), _group([21, 22, 23])]
        )
        self.assertEqual({i for _t, i in out[0].evidence}, {"11", "21"})

    def test_a_citation_of_nothing_real_yields_nothing(self) -> None:
        def responder(_system, _user):
            return {"concepts": [{
                "label": "they value each other",
                "evidence_memory_ids": [901, 902],
                "confidence": 0.9,
            }]}

        ctx, _ = _ctx(responder)
        self.assertEqual(
            propose_value_relationship(
                ctx, groups=[_group([11, 12, 13]), _group([21, 22, 23])]
            ),
            [],
        )

    def test_reinforce_by_id_skips_the_span_rule(self) -> None:
        # Reinforcement adds support to a value that already cleared the
        # bar; requiring it to re-clear would make known values unfeedable.
        from app.core.concepts.proposers import ExistingConcept

        def responder(_system, _user):
            return {"concepts": [{
                "reinforces_id": 42,
                "evidence_memory_ids": [11, 12],
                "rationale": "more of the same",
            }]}

        ctx, _ = _ctx(responder)
        out = propose_value_relationship(
            ctx,
            groups=[_group([11, 12, 13]), _group([21, 22, 23])],
            existing=[ExistingConcept(id=42, label="they say the hard thing")],
        )
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].reinforces_id, 42)
        self.assertEqual(out[0].label, "")

    def test_junk_rows_are_survived(self) -> None:
        def responder(_system, _user):
            return {"concepts": ["nope", None, 7]}

        ctx, _ = _ctx(responder)
        self.assertEqual(
            propose_value_relationship(
                ctx, groups=[_group([11, 12, 13]), _group([21, 22, 23])]
            ),
            [],
        )


# ── worker pass ─────────────────────────────────────────────────────────


def _moment_rows(n: int, *, start_id: int = 500) -> list[MemStub]:
    """Two distinct recurring patterns, so grouping yields two groups."""
    base = datetime(2026, 1, 2, 20, 0, tzinfo=_UTC)
    common = 4.0
    return [
        MemStub(
            start_id + i,
            f"Shared moment: together {i}",
            "shared_moment",
            0.5 + i * 0.05,
            metadata={
                "vibe": "tender",
                "when": (base + timedelta(days=7 * i)).isoformat(),
                "what": f"together {i}",
            },
            embedding=(
                _vec(common, 1.0, 0.0) if i < n // 2 else _vec(common, 0.0, 1.0)
            ),
        )
        for i in range(n)
    ]


def _both_responder(system, user):
    if "SHARED VALUES between" in system:
        return {"concepts": [{
            "label": "they say the awkward thing rather than smoothing it",
            "evidence_memory_ids": [500, 501, 503, 504],
            "rationale": "shows up in both stretches",
            "confidence": 0.7,
        }]}
    if "RELATIONSHIP RITUALS between" in system:
        return {"concepts": [{
            "label": "winding down together",
            "group_index": 0,
            "rationale": "recurring",
            "confidence": 0.7,
        }]}
    return {"concepts": []}


class WorkerPassTests(unittest.TestCase):
    def test_the_pass_creates_a_shared_value(self) -> None:
        rows = _moment_rows(6)
        h = WorkerHarness(
            _both_responder, clusters=[], self_memories=[],
            shared_moments=rows,
        )
        stats = h.worker.run()
        self.assertTrue(stats["shared_value_dirty"])
        out = h.store.list_by(subject="relationship", kind="value")
        self.assertEqual(len(out), 1)
        ev = h.store.evidence_of(out[0].concept_id)
        self.assertTrue(all(e.src_type == "memory" for e in ev))

    def test_the_two_passes_keep_separate_watermarks(self) -> None:
        # Sharing one sig key would let whichever ran first mark the corpus
        # settled and silence the other for good.
        rows = _moment_rows(6)
        h = WorkerHarness(
            _both_responder, clusters=[], self_memories=[],
            shared_moments=rows,
        )
        stats = h.worker.run()
        self.assertTrue(stats["ritual_dirty"])
        self.assertTrue(stats["shared_value_dirty"])
        self.assertEqual(
            len(h.store.list_by(subject="relationship", kind="ritual")), 1
        )
        self.assertEqual(
            len(h.store.list_by(subject="relationship", kind="value")), 1
        )

    def test_a_clean_rerun_is_a_noop(self) -> None:
        rows = _moment_rows(6)
        h = WorkerHarness(
            _both_responder, clusters=[], self_memories=[],
            shared_moments=rows,
        )
        h.worker.run()
        calls = h.ollama.calls
        stats = h.worker.run()
        self.assertFalse(stats["shared_value_dirty"])
        self.assertEqual(h.ollama.calls, calls)

    def test_its_own_switch_turns_it_off_without_touching_rituals(self) -> None:
        rows = _moment_rows(6)
        agent = types.SimpleNamespace(
            concepts_enabled=True,
            concept_synthesis_enabled=True,
            shared_value_synthesis_enabled=False,
        )
        h = WorkerHarness(
            _both_responder, clusters=[], self_memories=[],
            shared_moments=rows, agent=agent,
        )
        stats = h.worker.run()
        self.assertFalse(stats["shared_value_dirty"])
        self.assertEqual(
            h.store.list_by(subject="relationship", kind="value"), []
        )
        self.assertTrue(stats["ritual_dirty"])


# ── registration + rendering ────────────────────────────────────────────


class WiringTests(unittest.TestCase):
    def test_the_spec_is_registered_next_to_the_ritual_one(self) -> None:
        specs = [
            (s.kind, s.subject) for s in CONCEPT_PROPOSERS
            if s.population == "shared_moments"
        ]
        self.assertEqual(
            specs, [("ritual", "relationship"), ("value", "relationship")]
        )

    def test_the_two_shared_moment_specs_do_not_share_a_sig_key(self) -> None:
        keys = [
            s.sig_key for s in CONCEPT_PROPOSERS
            if s.population == "shared_moments"
        ]
        self.assertEqual(len(set(keys)), len(keys))

    def test_the_header_it_renders_under_already_existed(self) -> None:
        # The render side was waiting for rows nobody minted.
        from app.core.session.inner_life_part1 import InnerLifePart1Mixin

        header = InnerLifePart1Mixin._concept_group_header(
            "relationship", "value", "Jacob"
        )
        self.assertIn("both value", header)
        self.assertIn("Jacob", header)

    def test_a_shared_value_is_not_routed_to_the_profile_block(self) -> None:
        # ``value`` names ``profile_block`` for subject=user only; a shared
        # value belongs on the relevance path like the other pair concepts.
        from app.core.concepts.concept_kinds import get_kind

        self.assertEqual(
            get_kind("value").surfacing_targets, {"user": "profile_block"}
        )


if __name__ == "__main__":
    unittest.main()
