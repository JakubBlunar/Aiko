"""K85c — the pursuit kind: gate, proposer, and synthesis pass.

The design point being defended here is the gate. A pursuit is the one
concept Aiko gets to *open* with, so unlike taste -- which only colours
enthusiasm inside a conversation he already started -- a wrong one is her
announcing an interest she doesn't have. Hence three notes and a week,
and hence the proposer being told that recurrence, not vividness, is the
evidence.
"""
from __future__ import annotations

import json
import unittest
from types import SimpleNamespace
from typing import Any

from app.core.concepts.concept_kinds import get_kind
from app.core.concepts.concept_lifecycle import (
    pursuit_evidence_gate,
    taste_evidence_gate,
)
from app.core.concepts.proposers import CONCEPT_PROPOSERS
from app.core.concepts.proposers.base import ProposerContext
from app.core.concepts.proposers.pursuit_aiko import (
    SPEC,
    propose_pursuit_aiko,
)


class KindRegistrationTests(unittest.TestCase):
    def test_the_kind_is_registered_for_aiko(self) -> None:
        kind = get_kind("pursuit")
        self.assertIsNotNone(kind)
        assert kind is not None
        self.assertEqual(kind.subject, "aiko")
        self.assertEqual(kind.evidence_model, "set")
        self.assertIs(kind.promotion_gate, pursuit_evidence_gate)

    def test_it_is_never_pinned_into_every_turn(self) -> None:
        # A standing "you are into gardening" on every turn is the canned
        # hobby the backlog warns about.
        kind = get_kind("pursuit")
        assert kind is not None
        self.assertFalse(kind.core_always_on)

    def test_it_sits_between_taste_and_value_on_both_axes(self) -> None:
        pursuit = get_kind("pursuit")
        taste = get_kind("taste")
        value = get_kind("value")
        assert pursuit is not None and taste is not None
        assert value is not None
        self.assertLess(pursuit.plasticity_default, taste.plasticity_default)
        self.assertGreater(
            pursuit.plasticity_default, value.plasticity_default,
        )
        self.assertGreater(pursuit.importance, taste.importance)
        self.assertLess(pursuit.importance, value.importance)

    def test_the_proposer_is_in_the_registry_before_the_metas(self) -> None:
        kinds = [s.kind for s in CONCEPT_PROPOSERS]
        self.assertIn("pursuit", kinds)
        self.assertLess(kinds.index("pursuit"), kinds.index("tension"))


class GateTests(unittest.TestCase):
    def _gate(self, **over: Any) -> bool:
        args: dict[str, Any] = {
            "distinct_source_count": 3,
            "age_days": 8.0,
            "confidence": 0.7,
            "min_sources": 2,
            "min_age_days": 0.0,
            "min_confidence": 0.5,
        }
        args.update(over)
        return pursuit_evidence_gate(**args)

    def test_three_notes_over_a_week_promote(self) -> None:
        self.assertTrue(self._gate())

    def test_two_notes_are_a_habit_not_a_pursuit(self) -> None:
        self.assertFalse(self._gate(distinct_source_count=2))

    def test_an_afternoon_of_beats_does_not_promote(self) -> None:
        # Three notes can land inside one long away stretch; the week is
        # what tells a standing interest from a spell of good weather.
        self.assertFalse(self._gate(age_days=0.5))

    def test_it_is_strictly_harder_than_taste(self) -> None:
        common = {
            "distinct_source_count": 2,
            "age_days": 1.0,
            "confidence": 0.7,
            "min_sources": 2,
            "min_age_days": 0.0,
            "min_confidence": 0.5,
        }
        self.assertTrue(taste_evidence_gate(**common))
        self.assertFalse(pursuit_evidence_gate(**common))

    def test_a_stricter_caller_still_wins(self) -> None:
        self.assertFalse(self._gate(min_sources=5))


class _Mem:
    def __init__(self, mem_id: int, content: str) -> None:
        self.id = mem_id
        self.content = content
        self.created_at = "2026-08-01T10:00:00+00:00"
        self.salience = 0.5


class ProposerTests(unittest.TestCase):
    def _ctx(self, reply: list[dict[str, Any]]) -> tuple[ProposerContext, list]:
        seen: list[tuple[str, str]] = []

        def call_llm(system: str, user: str) -> list[dict[str, Any]]:
            seen.append((system, user))
            return reply

        return (
            ProposerContext(
                call_llm=call_llm, user_name="Jacob", assistant_name="Aiko",
            ),
            seen,
        )

    def _notes(self) -> list[_Mem]:
        return [
            _Mem(1, "Watered the tomatoes; one really needed it."),
            _Mem(2, "Back out to the tomatoes, first proper fruit."),
            _Mem(3, "Sat with the tomatoes a while, nothing to do."),
        ]

    def test_a_recurring_thread_becomes_a_candidate(self) -> None:
        ctx, _seen = self._ctx([
            {
                "label": "you keep coming back to the tomatoes",
                "evidence_memory_ids": [1, 2, 3],
                "rationale": "three separate visits",
                "confidence": 0.7,
            }
        ])
        out = propose_pursuit_aiko(ctx, memories=self._notes())
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].kind, "pursuit")
        self.assertEqual(out[0].subject, "aiko")
        self.assertEqual(
            sorted(out[0].evidence),
            [("memory", "1"), ("memory", "2"), ("memory", "3")],
        )

    def test_a_single_note_cannot_carry_one(self) -> None:
        ctx, _seen = self._ctx([
            {
                "label": "you are into gardening",
                "evidence_memory_ids": [1],
                "rationale": "one good afternoon",
                "confidence": 0.9,
            }
        ])
        self.assertEqual(propose_pursuit_aiko(ctx, memories=self._notes()), [])

    def test_an_empty_pool_never_calls_the_model(self) -> None:
        ctx, seen = self._ctx([])
        self.assertEqual(propose_pursuit_aiko(ctx, memories=[]), [])
        self.assertEqual(seen, [])

    def test_the_prompt_puts_him_out_of_the_picture(self) -> None:
        ctx, seen = self._ctx([])
        propose_pursuit_aiko(ctx, memories=self._notes())
        system = seen[0][0]
        self.assertIn("NOT about Jacob", system)
        self.assertIn("RECURRENCE is the evidence", system)

    def test_the_spec_routes_to_its_own_population(self) -> None:
        self.assertEqual(SPEC.population, "pursuit")
        self.assertEqual(SPEC.sig_key, "concept_synth.pursuit_sig.aiko")


class _Store:
    def __init__(self, mems: list[_Mem]) -> None:
        self._mems = mems
        self.asked: list[tuple[str, ...]] = []

    def iter_by_kinds(self, kinds: tuple[str, ...]) -> list[_Mem]:
        self.asked.append(tuple(kinds))
        return list(self._mems)


class _Worker:
    """The slice of ConceptSynthesisWorker the pursuit pass touches."""

    from app.core.concepts.concept_synthesis_worker import (
        ConceptSynthesisWorker as _Real,
    )

    _run_pursuit_pass = _Real._run_pursuit_pass
    _pursuit_min_notes = _Real._pursuit_min_notes
    _max_pursuit_memories = _Real._max_pursuit_memories

    def __init__(self, mems: list[_Mem], *, enabled: bool = True) -> None:
        self._memory_store = _Store(mems)
        self._agent_settings = SimpleNamespace(
            pursuit_synthesis_enabled=enabled,
        )
        self._memory_settings = SimpleNamespace(
            pursuit_min_notes=3,
            concept_synthesis_max_pursuit_memories=2,
        )
        self._dirty_size_delta = 2
        self._sigs: dict[str, str] = {}
        self.offered: list[list[_Mem]] = []

    def _load_sigs(self, key: str) -> dict[str, Any]:
        raw = self._sigs.get(key)
        return json.loads(raw) if raw else {}

    def _save_sigs(self, key: str, blob: dict[str, Any]) -> None:
        self._sigs[key] = json.dumps(blob)

    def _existing_for(self, spec: Any, **_kw: Any) -> list:
        return []

    def _propose(self, _ctx: Any, *, memories: Any, existing: Any) -> list:
        self.offered.append(list(memories))
        return [f"proposal-{len(memories)}"]

    def spec(self) -> Any:
        return SimpleNamespace(
            kind="pursuit",
            subject="aiko",
            sig_key="concept_synth.pursuit_sig.aiko",
            propose=self._propose,
        )


class PassTests(unittest.TestCase):
    def _mems(self, count: int) -> list[_Mem]:
        out = []
        for i in range(count):
            mem = _Mem(i + 1, f"note {i}")
            mem.created_at = f"2026-08-{i + 1:02d}T10:00:00+00:00"
            out.append(mem)
        return out

    def test_a_cold_pool_is_a_no_op(self) -> None:
        worker = _Worker(self._mems(2))
        stats: dict[str, Any] = {}
        self.assertEqual(
            worker._run_pursuit_pass(None, worker.spec(), stats), []
        )
        self.assertFalse(stats["pursuit_dirty"])
        self.assertEqual(worker.offered, [])

    def test_it_reads_only_pursuit_notes(self) -> None:
        worker = _Worker(self._mems(4))
        worker._run_pursuit_pass(None, worker.spec(), {})
        self.assertEqual(worker._memory_store.asked, [("pursuit_note",)])

    def test_the_batch_is_the_most_recent_in_order(self) -> None:
        # Chronological, not salience-sorted: recurrence is the signal,
        # and a salience sort hides the dull repetition that proves it.
        worker = _Worker(self._mems(5))
        worker._run_pursuit_pass(None, worker.spec(), {})
        offered = [m.id for m in worker.offered[0]]
        self.assertEqual(offered, [4, 5])

    def test_a_settled_pool_does_not_re_fire(self) -> None:
        worker = _Worker(self._mems(4))
        stats: dict[str, Any] = {}
        worker._run_pursuit_pass(None, worker.spec(), stats)
        self.assertTrue(stats["pursuit_dirty"])
        self.assertEqual(
            worker._run_pursuit_pass(None, worker.spec(), stats), []
        )
        self.assertFalse(stats["pursuit_dirty"])
        self.assertEqual(len(worker.offered), 1)

    def test_enough_new_notes_re_fire_it(self) -> None:
        worker = _Worker(self._mems(4))
        worker._run_pursuit_pass(None, worker.spec(), {})
        worker._memory_store = _Store(self._mems(6))
        worker._run_pursuit_pass(None, worker.spec(), {})
        self.assertEqual(len(worker.offered), 2)

    def test_force_overrides_the_signature(self) -> None:
        worker = _Worker(self._mems(4))
        worker._run_pursuit_pass(None, worker.spec(), {})
        worker._run_pursuit_pass(None, worker.spec(), {}, True)
        self.assertEqual(len(worker.offered), 2)

    def test_the_switch_skips_the_pass(self) -> None:
        worker = _Worker(self._mems(9), enabled=False)
        self.assertEqual(
            worker._run_pursuit_pass(None, worker.spec(), {}), []
        )
        self.assertEqual(worker._memory_store.asked, [])


if __name__ == "__main__":
    unittest.main()
