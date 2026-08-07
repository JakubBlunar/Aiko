"""L30c end to end: she asked, they answered, what changed?

:mod:`test_hypothesis_resolution` covers what each verdict writes to a
concept. This covers the loop around it -- routing an awaiting cue to the
adjudicator, storing the answer as evidence, and retiring or releasing
the row -- because the ordering against generic stage-B settlement is a
contract that only shows up when the pieces run together.

The one worth naming: ``_resolve_concept_hypotheses`` runs *before*
``_settle_awaiting_cues`` and finishes every ``concept_hypothesis`` row
itself. Generic stage B decides "did they answer?" from topical overlap
alone, so left to it a flat denial would count as a satisfied question
and the belief would carry on unchallenged.
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.concepts.concept_event_store import ConceptEventStore
from app.core.concepts.concept_store import Concept, ConceptEdge, ConceptStore
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.cue_store import (
    STATE_AWAITING,
    STATE_EXPIRED,
    STATE_PENDING,
    STATE_USED,
    CueStore,
)
from app.core.session.cue_pool_mixin import CuePoolMixin
from app.core.session.post_turn_helpers_mixin import PostTurnHelpersMixin


_BELIEF_VEC = np.asarray([1.0, 0.0], dtype=np.float32)
_ORTHOGONAL = np.asarray([0.0, 1.0], dtype=np.float32)


def _json(verdict: str) -> str:
    return f'{{"verdict": "{verdict}", "restated": "", "reason": "r"}}'


class _FakeOllama:
    def __init__(self, *payloads: str) -> None:
        self._payloads = list(payloads)
        self.calls = 0

    def chat_stream(self, messages, **kwargs):
        self.calls += 1
        yield self._payloads.pop(0) if self._payloads else _json("unclear")


class _FakeMemoryStore:
    def __init__(self) -> None:
        self.added: list[str] = []
        self._next_id = 500

    def add(self, *, content: str, **_kw: Any):
        self.added.append(content)
        self._next_id += 1
        return SimpleNamespace(id=self._next_id, content=content)


class _Host(CuePoolMixin, PostTurnHelpersMixin):
    """A SessionController narrowed to what the resolver reaches for."""

    def __init__(
        self,
        *,
        cue_store: CueStore,
        concept_store: ConceptStore,
        event_store: ConceptEventStore,
        ollama: Any,
        enabled: bool = True,
        memory_store: Any = None,
        reply_vec: Any = None,
    ) -> None:
        self._cue_store = cue_store
        self._surfaced_pool_cues: list = []
        self._cue_pool_listeners: list = []
        self._concept_store = concept_store
        self._concept_event_store = event_store
        self._maintenance_client = ollama
        self._effective_worker_model = "worker"
        self._memory_store = memory_store
        self._memory_listeners: list = []
        # Orthogonal to the belief vector unless a test says otherwise:
        # the default is a reply that shares nothing with the hunch.
        vec = _ORTHOGONAL if reply_vec is None else reply_vec
        self._embedder = SimpleNamespace(embed=lambda text: vec)
        self._settings = SimpleNamespace(
            agent=SimpleNamespace(concept_hypothesis_ask_enabled=enabled),
        )
        self._memory_settings = SimpleNamespace(
            concept_hypothesis_deny_penalty=0.25,
        )
        self.session_key = "s1"

    def _notify_memory_added(self, memory: Any) -> None:
        pass


class _Fixture(unittest.TestCase):
    LABEL = "Jacob treats walking as thinking time"

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = ChatDatabase(Path(tmp.name) / "chat.db")
        self.cues = CueStore(self.db)
        self.concepts = ConceptStore(self.db)
        self.events = ConceptEventStore(self.db)
        self.memories = _FakeMemoryStore()

        self.concept = Concept(
            label=self.LABEL,
            kind="pattern",
            subject="user",
            status="candidate",
            confidence=0.6,
            plasticity=0.5,
            evidence_count=1,
            distinct_source_count=1,
            embedding=np.asarray([1.0, 0.0], dtype=np.float32),
        )
        self.concept.concept_id = self.concepts.add(self.concept)
        # A real edge behind the counter, because the confirm path
        # recounts from ``evidence_of`` rather than incrementing.
        self.concepts.add_edge(
            ConceptEdge(
                src_type="memory",
                src_id="11",
                dst_type="concept",
                dst_id=str(self.concept.concept_id),
                relation="evidence",
                polarity=1,
                strength=1.0,
            )
        )

    def _host(self, *verdicts: str, **kw: Any) -> _Host:
        return _Host(
            cue_store=self.cues,
            concept_store=self.concepts,
            event_store=self.events,
            ollama=_FakeOllama(*[_json(v) for v in verdicts]),
            memory_store=self.memories,
            **kw,
        )

    def _awaiting_cue(self, *, target_id: int | None = None) -> int:
        cue_id = self.cues.add(
            "concept_hypothesis",
            self.LABEL,
            "you've had a hunch you never checked",
            payload={
                "target_type": "concept",
                "target_id": (
                    self.concept.concept_id if target_id is None else target_id
                ),
                "label": self.LABEL,
                "kind": "pattern",
                "subject": "user",
                "importance": 0.8,
            },
            embedding=_BELIEF_VEC,
        )
        self.cues.mark_surfaced(cue_id)
        self.cues.mark_asked(cue_id)
        return cue_id

    def _state(self, cue_id: int) -> str:
        return next(r.state for r in self.cues.list_for_user() if r.id == cue_id)

    def _reload(self) -> Concept:
        self.concepts.load_all()
        return self.concepts.get(self.concept.concept_id)


class SettledVerdictTests(_Fixture):
    def test_a_confirm_grounds_the_belief_and_retires_the_cue(self) -> None:
        cue_id = self._awaiting_cue()
        host = self._host("CONFIRM")
        host._resolve_concept_hypotheses(user_text="yeah, pretty much always")

        self.assertEqual(self._state(cue_id), STATE_USED)
        self.assertEqual(self._reload().distinct_source_count, 2)

    def test_a_deny_lowers_conviction_and_retires_the_cue(self) -> None:
        cue_id = self._awaiting_cue()
        host = self._host("DENY")
        host._resolve_concept_hypotheses(user_text="no, not really at all")

        self.assertEqual(self._state(cue_id), STATE_USED)
        self.assertLess(self._reload().confidence, 0.6)

    def test_a_correct_also_spends_the_question(self) -> None:
        # She asked and got a real answer. That the answer re-worded the
        # belief rather than settling it does not buy a second ask.
        cue_id = self._awaiting_cue()
        host = self._host("CORRECT")
        host._resolve_concept_hypotheses(
            user_text="close -- it's more that I hate sitting still"
        )
        self.assertEqual(self._state(cue_id), STATE_USED)

    def test_the_answer_is_stored_with_the_question_for_context(self) -> None:
        # "yeah, pretty much" means nothing on its own, and this row has
        # to stand as evidence long after the question is forgotten.
        self._awaiting_cue()
        host = self._host("CONFIRM")
        host._resolve_concept_hypotheses(user_text="yeah, pretty much")

        self.assertEqual(len(self.memories.added), 1)
        stored = self.memories.added[0]
        self.assertIn(self.LABEL, stored)
        self.assertIn("yeah, pretty much", stored)

    def test_each_verdict_leaves_a_concept_event(self) -> None:
        self._awaiting_cue()
        self._host("DENY")._resolve_concept_hypotheses(user_text="no, never")

        types = [e.event_type for e in self.events.list(limit=5)]
        self.assertIn("hypothesis_denied", types)


class UnclearTests(_Fixture):
    def test_a_dodge_changes_nothing_about_the_belief(self) -> None:
        self._awaiting_cue()
        host = self._host("UNCLEAR")
        host._resolve_concept_hypotheses(
            user_text="ha, wouldn't you like to know"
        )

        self.assertAlmostEqual(self._reload().confidence, 0.6)
        self.assertEqual(self.memories.added, [])

    def test_a_dodge_drops_the_hunch_rather_than_re_asking(self) -> None:
        # ``max_asks=1``, and reaching ``awaiting`` already spent it.
        # Pressing someone twice on whether a guess about them is true
        # reads as doubting their first answer.
        cue_id = self._awaiting_cue()
        host = self._host("UNCLEAR")
        host._resolve_concept_hypotheses(user_text="anyway, never mind")

        self.assertEqual(self._state(cue_id), STATE_EXPIRED)

    def test_a_hunch_with_asks_to_spare_would_be_released(self) -> None:
        # Guards the shared shape rather than live behaviour: the release
        # arm is unreachable at ``max_asks=1``, and raising the policy
        # should be a one-line change rather than a rewrite.
        cue_id = self._awaiting_cue()
        row = next(r for r in self.cues.list_for_user() if r.id == cue_id)
        row.ask_count = 0
        self._host()._release_unanswered_hypothesis(self.cues, row, "dodged")

        self.assertEqual(self._state(cue_id), STATE_PENDING)

    def test_a_paraphrased_answer_survives_the_gate(self) -> None:
        # Long, on-subject, and sharing no content word with the belief.
        # Lexical overlap alone would discard it; the reply vector is
        # embedded precisely so the semantic half can rescue it.
        self._awaiting_cue()
        host = self._host("CONFIRM", reply_vec=_BELIEF_VEC)
        host._resolve_concept_hypotheses(
            user_text=(
                "Completely -- the pavement is where anything resembling a "
                "coherent plan tends to assemble itself, unhelpfully far "
                "from any means of writing the thing down."
            )
        )
        self.assertEqual(host._maintenance_client.calls, 1)

    def test_an_off_subject_reply_costs_no_llm_call(self) -> None:
        self._awaiting_cue()
        host = self._host("CONFIRM")
        host._resolve_concept_hypotheses(
            user_text=(
                "So the deployment finally went through last night after we "
                "rebuilt the image and re-ran the whole migration by hand."
            )
        )
        self.assertEqual(host._maintenance_client.calls, 0)
        self.assertAlmostEqual(self._reload().confidence, 0.6)


class OwnershipTests(_Fixture):
    def test_it_leaves_no_row_for_the_generic_matcher(self) -> None:
        # The ordering contract: every awaiting row of this type reaches
        # a terminal state or a release here, so stage B -- which reads
        # topical overlap and would score a denial as a satisfied
        # question -- never sees one.
        self._awaiting_cue()
        self._awaiting_cue()
        host = self._host("CONFIRM", "UNCLEAR")
        host._resolve_concept_hypotheses(user_text="yes, that's about right")

        self.assertEqual(
            self.cues.in_state(STATE_AWAITING, cue_type="concept_hypothesis"),
            [],
        )

    def test_a_deleted_target_concept_is_survivable(self) -> None:
        cue_id = self._awaiting_cue(target_id=99999)
        host = self._host("CONFIRM")
        host._resolve_concept_hypotheses(user_text="yes, definitely")

        self.assertEqual(self._state(cue_id), STATE_USED)
        self.assertEqual(self.memories.added, [])

    def test_a_missing_memory_store_downgrades_a_confirm(self) -> None:
        # A confirm with nothing to attach must not claim a source it
        # does not have.
        self._awaiting_cue()
        host = _Host(
            cue_store=self.cues,
            concept_store=self.concepts,
            event_store=self.events,
            ollama=_FakeOllama(_json("CONFIRM")),
            memory_store=None,
        )
        host._resolve_concept_hypotheses(user_text="yes, always")
        self.assertEqual(self._reload().distinct_source_count, 1)

    def test_the_master_switch_leaves_everything_alone(self) -> None:
        cue_id = self._awaiting_cue()
        host = self._host("CONFIRM", enabled=False)
        host._resolve_concept_hypotheses(user_text="yes, always")

        self.assertEqual(self._state(cue_id), STATE_AWAITING)
        self.assertEqual(host._maintenance_client.calls, 0)

    def test_no_concept_graph_is_a_no_op(self) -> None:
        cue_id = self._awaiting_cue()
        host = self._host("CONFIRM")
        host._concept_store = None
        host._resolve_concept_hypotheses(user_text="yes, always")
        self.assertEqual(self._state(cue_id), STATE_AWAITING)

    def test_nothing_awaiting_costs_nothing(self) -> None:
        host = self._host("CONFIRM")
        host._resolve_concept_hypotheses(user_text="yes, always")
        self.assertEqual(host._maintenance_client.calls, 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
