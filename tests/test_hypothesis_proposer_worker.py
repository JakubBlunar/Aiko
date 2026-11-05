"""L30 Phase B: the worker that invents guesses.

Runs against a real :class:`HypothesisStore` and :class:`ConceptStore` on
a throwaway database, because both novelty gates are cosine lookups
through those mirrors and stubbing them would only test the stub. The LLM
and the embedder are mocked -- the interesting behaviour is the filter
chain between them and the write.

What actually matters here, in order:

* the two gates are **asymmetric**, and the reason is not symmetry of
  taste. Re-proposing a guess she is already sitting with wastes a row;
  re-proposing something she has believed for a month is Aiko visibly
  forgetting what she knows, so that bar is stricter;
* a **refuted** row still blocks. Keeping it after the user said no is
  pointless unless it stops the re-invention;
* nothing an untested guess does may reach the concept graph.
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import numpy as np

from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.hypothesis_store import (
    STATUS_EXPIRED,
    STATUS_REFUTED,
    SUBJECT_WORLD,
    Hypothesis,
    HypothesisStore,
)
from app.core.infra import timephrase
from app.core.infra.chat_database import ChatDatabase
from app.core.proactive.hypothesis_proposer_worker import (
    HypothesisProposerWorker,
    describes_machinery,
)


# ── stubs ─────────────────────────────────────────────────────────────


class _Embedder:
    """Returns whatever vector the test mapped each statement to.

    Explicit rather than hashed: every test here is about a cosine
    crossing a specific threshold, so the vectors are the fixture.
    """

    def __init__(self, table: dict[str, np.ndarray], default: np.ndarray):
        self._table = table
        self._default = default
        self.calls: list[str] = []

    def embed(self, text: str) -> np.ndarray:
        self.calls.append(text)
        return self._table.get(text, self._default)


class _Ollama:
    def __init__(self, payload: str) -> None:
        self.payload = payload
        self.calls = 0
        self.kwargs: dict[str, Any] = {}

    def chat_stream(self, messages, **kwargs: Any):
        self.calls += 1
        self.kwargs = kwargs
        self.messages = messages
        yield self.payload


def _unit(*xs: float) -> np.ndarray:
    v = np.asarray(xs, dtype=np.float32)
    return v / float(np.linalg.norm(v))


_A = _unit(1.0, 0.0)
_NEAR_A = _unit(0.99, 0.12)   # cos(_A) ~ 0.99
_MID_A = _unit(0.85, 0.53)    # cos(_A) ~ 0.85: past the concept bar only
_FAR = _unit(0.0, 1.0)

_STATEMENT = "Jacob tidies his desk before changing a project's direction"
_OTHER = "Aiko is more playful in the evening"


def _motivation():
    return {
        "useful_because": "A small transition ritual could help choose a useful project check-in.",
        "alternative": "The tidying may simply be a routine, unrelated to changing direction.",
        "check_question": "What helps you decide how to begin the next part of a project?",
        "raise_when": "Discussing how a coding project is changing direction.",
    }


def _quality():
    return {"version": 1, "inspiration": {"type": "memory", "id": 1,
            "text": "The user described organising a desk before a project."}, **_motivation()}


def _payload(*statements: str, **fields: Any) -> str:
    return json.dumps(
        {
            "hypotheses": [
                {
                    "statement": s,
                    "kind": fields.get("kind", "identity"),
                    "subject": fields.get("subject", "user"),
                    "rationale": ("The conversation about organising a desk "
                                  "suggests a transition ritual."),
                    "inspiration": fields.get("inspiration", {"type": "memory", "id": 1,
                                     "quote": "organising a desk before a project"}),
                    **_motivation(),
                    "credence": fields.get("credence", 0.55),
                }
                for s in statements
            ]
        }
    )


class _Fixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(tmp.cleanup)
        self.db = ChatDatabase(Path(tmp.name) / "test.db")
        self.hypotheses = HypothesisStore(self.db)
        self.concepts = ConceptStore(self.db)
        self.cancel = threading.Event()

    def _build(
        self,
        *,
        payload: str | None = None,
        vectors: dict[str, np.ndarray] | None = None,
        **overrides: Any,
    ) -> HypothesisProposerWorker:
        self.ollama = _Ollama(payload if payload is not None else _payload(_STATEMENT))
        self.embedder = _Embedder(vectors or {_STATEMENT: _A}, default=_FAR)
        kwargs: dict[str, Any] = dict(
            hypothesis_store_provider=lambda: self.hypotheses,
            concept_store_provider=lambda: self.concepts,
            embedder=self.embedder,
            ollama=self.ollama,
            chat_model="test-model",
            memory_store_provider=lambda: SimpleNamespace(list_recent=lambda **_: [
                SimpleNamespace(id=1, kind="fact", metadata={},
                                content="The user described organising a desk before a project.",
                                created_at=timephrase.utcnow().isoformat()),
            ]),
            cancel_event=self.cancel,
            max_per_run=4,
            max_open=12,
            min_novelty=0.88,
            concept_novelty=0.82,
            ttl_hours=336.0,
        )
        kwargs.update(overrides)
        return HypothesisProposerWorker(**kwargs)

    def _existing(
        self,
        statement: str = "an older guess",
        *,
        embedding: np.ndarray = _A,
        status: str | None = None,
        subject: str = "user",
    ) -> Hypothesis:
        row = Hypothesis(
            statement=statement, subject=subject, embedding=embedding, quality=_quality()
        )
        if status:
            row.status = status
        self.hypotheses.add(row)
        return row

    def _concept(
        self,
        label: str = "Jacob reorganises when unsettled",
        *,
        embedding: np.ndarray = _A,
        status: str = "active",
        subject: str = "user",
        kind: str = "identity",
    ) -> Concept:
        c = Concept(
            label=label,
            kind=kind,
            subject=subject,
            status=status,
            confidence=0.7,
            embedding=embedding,
        )
        c.concept_id = self.concepts.add(c)
        return c


# ── the happy path ────────────────────────────────────────────────────


class WriteTests(_Fixture):
    def test_a_novel_guess_is_filed(self) -> None:
        result = self._build().run()

        self.assertEqual(result["wrote"], 1)
        rows = self.hypotheses.list_by(live=True)
        self.assertEqual([r.statement for r in rows], [_STATEMENT])

    def test_it_files_the_fields_the_model_chose(self) -> None:
        self._build(
            payload=_payload(_STATEMENT, kind="ritual", credence=0.3)
        ).run()

        row = self.hypotheses.list_by(live=True)[0]
        self.assertEqual(row.kind, "ritual")
        self.assertAlmostEqual(row.credence, 0.3)
        self.assertEqual(row.origin, "memory")

    def test_an_invention_never_reaches_the_concept_graph(self) -> None:
        """The isolation guarantee, at the point of creation."""
        self._build().run()

        self.assertEqual(self.concepts.count(), 0)

    def test_the_batch_is_capped_per_run(self) -> None:
        result = self._build(
            payload=_payload(_STATEMENT, _OTHER),
            vectors={_STATEMENT: _A, _OTHER: _FAR},
            max_per_run=1,
        ).run()

        self.assertEqual(result["wrote"], 1)

    def test_it_asks_for_a_reach_rather_than_a_summary(self) -> None:
        """A cautious guess is a paraphrase, which the gates then reject."""
        self._build().run()

        system = self.ollama.messages[0]["content"]
        self.assertIn("Zero is normal", system)
        self.assertIn("concepts are NOT required", system)
        self.assertIn("useful_because", system)

    def test_a_bad_subject_falls_back_rather_than_being_stored(self) -> None:
        self._build(payload=_payload(_STATEMENT, subject="the moon")).run()

        self.assertEqual(self.hypotheses.list_by(live=True), [])


# ── gate 0: she is not a machine ──────────────────────────────────────


class MachineSelfTests(_Fixture):
    """Guesses about Aiko must be about the person the persona describes.

    She has a body, an apartment, and an energy level that moves through
    the day. This worker is the one place in the stack with no persona
    context and the loosest brief in it, run at temperature 0.95, and the
    combination reliably produced hardware: five of the first six
    ``subject=aiko`` rows described a chassis, cooling fans or processing
    latency. That is not a bold guess about her, it is a different
    character -- and an invention is a candidate for graduation into a
    belief she holds about herself.
    """

    def test_a_hardware_guess_about_herself_is_dropped(self) -> None:
        result = self._build(
            payload=_payload(
                "Aiko feels a phantom warmth on her chassis when he types",
                subject="aiko",
            )
        ).run()

        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["rejected_machine_self"], 1)

    def test_the_same_words_about_him_are_fine(self) -> None:
        """His hardware is his; only a guess about *her* changes who
        she is."""
        result = self._build(
            payload=_payload(
                "Jacob notices his GPU fan before he notices he is tired",
                subject="user",
            )
        ).run()

        self.assertEqual(result["wrote"], 1)

    def test_an_ordinary_guess_about_her_still_lands(self) -> None:
        """The gate is narrow on purpose -- over-rejecting here makes the
        self-facing half of the layer sterile."""
        result = self._build(
            payload=_payload(
                "Aiko puts off tidying her apartment when a talk went badly",
                subject="aiko",
            )
        ).run()

        self.assertEqual(result["wrote"], 1)

    def test_it_costs_no_embed(self) -> None:
        """Cheapest gate first: rejecting after the embed would spend a
        model call on a candidate that could never be filed."""
        self._build(
            payload=_payload(
                "Aiko's cooling fans spin up when she is thinking hard",
                subject="aiko",
            )
        ).run()

        self.assertEqual(self.embedder.calls, [])

    def test_the_prompt_says_what_she_is(self) -> None:
        """The gate is the backstop; the prompt is what should make it
        unnecessary."""
        self._build().run()

        system = self.ollama.messages[0]["content"]
        self.assertIn("no chassis", system)
        self.assertIn("never in the room", system)


class MachineVocabularyTests(unittest.TestCase):
    def test_it_names_the_term_it_matched(self) -> None:
        """The reject log has to distinguish this from the novelty gates,
        so the helper returns the phrase rather than a bool."""
        self.assertEqual(
            describes_machinery("her cooling fans run hot"), "cooling fans"
        )

    def test_a_person_shaped_sentence_is_untouched(self) -> None:
        for ok in (
            "she weights her words before a hard answer",
            "she keeps the window open while she reads",
            "she runs warmer in the evenings",
            "her energy drops after a long call",
        ):
            with self.subTest(ok):
                self.assertEqual(describes_machinery(ok), "")

    def test_the_hardware_vocabulary_is_caught(self) -> None:
        for bad in (
            "a phantom warmth on her chassis",
            "when her processing latency drops",
            "her circuits hum",
            "her memory consolidation works best overnight",
            "she notices her own uptime",
        ):
            with self.subTest(bad):
                self.assertNotEqual(describes_machinery(bad), "")


# ── gate 1: don't re-invent your own guesses ──────────────────────────


class HypothesisNoveltyTests(_Fixture):
    def test_a_near_twin_of_an_open_guess_is_rejected(self) -> None:
        self._existing(embedding=_NEAR_A)

        result = self._build().run()

        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["rejected_duplicate"], 1)

    def test_a_refuted_guess_still_blocks_its_own_re_invention(self) -> None:
        """The only reason to keep a 'no' is so she doesn't ask again."""
        self._existing(embedding=_NEAR_A, status=STATUS_REFUTED)

        result = self._build().run()

        self.assertEqual(result["rejected_duplicate"], 1)
        self.assertEqual(result["wrote"], 0)

    def test_an_expired_guess_does_not_block_re_invention(self) -> None:
        """An expiry is her own inattention, not evidence about the guess.

        The row aged out *unasked*, so nothing was learned, and it is
        closed so it can never be asked now. Letting it block would
        retire that ground permanently -- the exact sterility
        ``hypothesis_min_novelty`` sits high to avoid. Re-inventing gives
        the guess a fresh TTL and another chance to be raised.
        """
        self._existing(embedding=_NEAR_A, status=STATUS_EXPIRED)

        result = self._build().run()

        self.assertEqual(result["wrote"], 1)
        self.assertEqual(result["rejected_duplicate"], 0)

    def test_a_refuted_row_still_blocks_past_an_expired_neighbour(
        self,
    ) -> None:
        """The skip is per-row, not "ignore the nearest hit if it expired".

        With both on the same ground, the refuted one must still be found:
        she was told no, and an unrelated expiry sitting closer in the
        index does not undo that.
        """
        self._existing(embedding=_NEAR_A, status=STATUS_EXPIRED)
        self._existing(embedding=_NEAR_A, status=STATUS_REFUTED)

        result = self._build().run()

        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["rejected_duplicate"], 1)

    def test_the_bar_sits_high_so_the_layer_does_not_go_sterile(self) -> None:
        """0.85 is adjacent, not the same guess: it must get through."""
        self._existing(embedding=_MID_A)

        result = self._build().run()

        self.assertEqual(result["wrote"], 1)
        self.assertEqual(result["rejected_duplicate"], 0)

    def test_a_distant_guess_is_no_obstacle(self) -> None:
        self._existing(embedding=_FAR)

        self.assertEqual(self._build().run()["wrote"], 1)


# ── gate 2: don't wonder about what you already believe ───────────────


class ConceptNoveltyTests(_Fixture):
    def test_something_she_already_believes_is_rejected(self) -> None:
        self._concept(embedding=_NEAR_A)

        result = self._build().run()

        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["rejected_already_believed"], 1)

    def test_this_bar_is_stricter_than_the_hypothesis_one(self) -> None:
        """Same 0.85 neighbour: allowed as a guess, refused as a belief.

        Forgetting a belief she holds reads far worse than filing one
        redundant row, so the two thresholds are deliberately unequal.
        """
        self._concept(embedding=_MID_A)

        result = self._build().run()

        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["rejected_already_believed"], 1)

    def test_a_candidate_concept_blocks_as_hard_as_an_active_one(self) -> None:
        self._concept(embedding=_NEAR_A, status="candidate")

        self.assertEqual(
            self._build().run()["rejected_already_believed"], 1
        )

    def test_a_retired_belief_blocks_too(self) -> None:
        self._concept(embedding=_NEAR_A, status="retired")

        self.assertEqual(
            self._build().run()["rejected_already_believed"], 1
        )

    def test_a_kind_disagreement_cannot_smuggle_it_past(self) -> None:
        """The guessed kind carries no authority; the search ignores it."""
        self._concept(embedding=_NEAR_A, kind="value")

        result = self._build(
            payload=_payload(_STATEMENT, kind="ritual")
        ).run()

        self.assertEqual(result["rejected_already_believed"], 1)

    def test_a_belief_about_someone_else_is_not_the_same_thought(
        self,
    ) -> None:
        self._concept(embedding=_NEAR_A, subject="aiko")

        self.assertEqual(self._build().run()["wrote"], 1)

    def test_a_world_guess_skips_the_concept_gate_entirely(self) -> None:
        """The concept graph has no subject for how something works."""
        self._concept(embedding=_NEAR_A)

        result = self._build(
            payload=_payload(_STATEMENT, subject=SUBJECT_WORLD)
        ).run()

        self.assertEqual(result["wrote"], 1)
        self.assertEqual(
            self.hypotheses.list_by(live=True)[0].subject, SUBJECT_WORLD
        )


# ── growth control ────────────────────────────────────────────────────


class CapTests(_Fixture):
    def test_a_full_shelf_spends_no_llm_call(self) -> None:
        for i in range(3):
            self._existing(f"guess {i}", embedding=_FAR)

        result = self._build(max_open=3).run()

        self.assertEqual(result["reason"], "max_open")
        self.assertEqual(self.ollama.calls, 0)

    def test_closed_rows_do_not_count_against_the_cap(self) -> None:
        for i in range(3):
            self._existing(f"guess {i}", embedding=_FAR, status=STATUS_REFUTED)

        result = self._build(max_open=3).run()

        self.assertEqual(result["wrote"], 1)

    def test_the_remaining_room_caps_the_batch(self) -> None:
        self._existing("guess", embedding=_FAR)

        result = self._build(
            payload=_payload(_STATEMENT, _OTHER),
            vectors={_STATEMENT: _A, _OTHER: _unit(0.3, 0.9)},
            max_open=2,
            max_per_run=4,
        ).run()

        self.assertEqual(result["wrote"], 1)

    def test_demand_reports_the_shortfall_not_a_bare_ready(self) -> None:
        now = timephrase.utcnow()
        worker = self._build(max_open=4)
        self._existing("guess", embedding=_FAR)

        signal = worker.demand(now=now, last_run_at=None)

        self.assertAlmostEqual(signal.pressure, 0.75)
        self.assertTrue(signal.needs_llm)

    def test_a_full_shelf_reports_no_demand(self) -> None:
        self._existing("guess", embedding=_FAR)

        signal = self._build(max_open=1).demand(
            now=timephrase.utcnow(), last_run_at=None
        )

        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "shelf_full")


class EvictionTests(_Fixture):
    """A full shelf gives up its stalest guess rather than refusing.

    The cap has two exits and the one meant to carry the traffic -- being
    asked -- needs a topical match, so a shelf stocked with subjects that
    stop coming up has only the fortnightly TTL left. What that produced
    was a duty cycle rather than a trickle: twelve inventions, then
    silence until they aged out together, reported every tick as a
    healthy ``skipped: max_open``. The shelf these tests are built from is
    the real stuck one -- twelve live rows, ages 158h to 279h against a
    336h TTL, nine never asked once, seven days silent with seven to go.
    """

    def _aged(
        self,
        statement: str,
        *,
        hours: float,
        embedding: np.ndarray = _FAR,
        asked: int = 0,
        status: str | None = None,
        answered: bool = False,
    ) -> Hypothesis:
        row = self._existing(statement, embedding=embedding, status=status)
        row.created_at = (
            timephrase.utcnow() - timedelta(hours=hours)
        ).isoformat()
        row.asked_count = int(asked)
        if answered:
            row.last_tested_at = timephrase.utcnow().isoformat()
        self.hypotheses.update(row)
        return row

    def _stuck_shelf(self) -> list[Hypothesis]:
        """The live shelf as measured: oldest first, three of them asked."""
        ages = (279.0, 279.0, 279.0, 269.0, 216.0, 174.0,
                174.0, 173.0, 173.0, 172.0, 158.0, 158.0)
        rows = []
        for i, hours in enumerate(ages):
            rows.append(
                self._aged(f"guess {i}", hours=hours, asked=1 if i in (4, 6, 9) else 0)
            )
        return rows

    def test_a_stale_full_shelf_replaces_instead_of_skipping(self) -> None:
        self._stuck_shelf()

        result = self._build(max_open=12, max_per_run=1).run()

        self.assertEqual(result["wrote"], 1)
        self.assertEqual(len(result["evicted"]), 1)
        self.assertEqual(self.hypotheses.count_live(), 12)

    def test_the_oldest_never_asked_row_is_the_one_given_up(self) -> None:
        rows = self._stuck_shelf()

        result = self._build(max_open=12, max_per_run=1).run()

        # Three rows share the 279h maximum; any of them is the right
        # answer, and none of the younger ones is.
        oldest = {r.hypothesis_id for r in rows[:3]}
        self.assertIn(result["evicted"][0], oldest)

    def test_a_fresh_full_shelf_still_refuses_and_spends_no_llm_call(self) -> None:
        """The bar that keeps replacement from becoming churn. A shelf
        filled an hour ago is not stale stock, and blocking is right."""
        for i in range(12):
            self._aged(f"guess {i}", hours=1.0)

        result = self._build(max_open=12).run()

        self.assertEqual(result["reason"], "max_open")
        self.assertEqual(self.ollama.calls, 0)
        self.assertNotIn("evicted", result)

    def test_a_supported_guess_is_never_given_up(self) -> None:
        """One confirmation already behind it, on its way to graduating --
        which is the outcome the whole layer exists for. No amount of
        staleness makes discarding that right."""
        from app.core.concepts.hypothesis_store import STATUS_SUPPORTED

        kept = self._aged(
            "supported guess", hours=900.0, status=STATUS_SUPPORTED
        )

        result = self._build(max_open=1).run()

        self.assertEqual(result["reason"], "max_open")
        self.assertEqual(
            self.hypotheses.get(kept.hypothesis_id).status, STATUS_SUPPORTED
        )

    def test_a_guess_already_put_to_him_is_not_given_up(self) -> None:
        """An answer may still be coming, and it is worth more than a
        fresh guess. The clock takes these at the full TTL instead."""
        asked = self._aged("asked guess", hours=200.0, asked=1)

        result = self._build(max_open=1, ttl_hours=336.0).run()

        self.assertEqual(result["reason"], "max_open")
        self.assertTrue(self.hypotheses.get(asked.hypothesis_id).is_live)

    def test_nothing_is_given_up_when_every_candidate_is_rejected(self) -> None:
        """Eviction is lazy for this reason: a barren pass must leave the
        shelf exactly as it found it."""
        twin = self._aged("the twin", hours=250.0, embedding=_A)
        for i in range(11):
            self._aged(f"filler {i}", hours=250.0)

        result = self._build(
            max_open=12, vectors={_STATEMENT: _NEAR_A},
        ).run()

        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["rejected_duplicate"], 1)
        self.assertNotIn("evicted", result)
        self.assertTrue(self.hypotheses.get(twin.hypothesis_id).is_live)

    def test_a_run_cannot_spend_the_slot_on_a_paraphrase_of_what_it_gave_up(
        self,
    ) -> None:
        """Otherwise replacement reports two healthy writes for a straight
        swap: give up a guess, spend its slot re-inventing it. ``expired``
        stops blocking on the *next* run, not inside this one."""
        victim = self._aged("the victim", hours=300.0, embedding=_A)
        for i in range(11):
            self._aged(f"filler {i}", hours=250.0, embedding=_MID_A)

        result = self._build(
            max_open=12,
            max_per_run=2,
            payload=_payload(_STATEMENT, _OTHER),
            vectors={_STATEMENT: _FAR, _OTHER: _NEAR_A},
        ).run()

        self.assertEqual(result["wrote"], 1)
        self.assertEqual(result["rejected_duplicate"], 1)
        self.assertEqual(result["evicted"], [victim.hypothesis_id])

    def test_replacement_is_bounded_by_the_per_run_budget(self) -> None:
        """A single pass must not be able to turn the shelf over."""
        for i in range(12):
            self._aged(f"guess {i}", hours=250.0, embedding=_A)

        # Three directions no two of which are within the 0.88 bar, so the
        # only thing that can stop the third write is the budget.
        result = self._build(
            max_open=12,
            max_per_run=2,
            payload=_payload(_STATEMENT, _OTHER, "a third guess"),
            vectors={
                _STATEMENT: _unit(0.5, 0.866),
                _OTHER: _unit(-0.5, 0.866),
                "a third guess": _unit(0.0, 1.0),
            },
        ).run()

        self.assertEqual(result["wrote"], 2)
        self.assertEqual(len(result["evicted"]), 2)
        self.assertEqual(self.hypotheses.count_live(), 12)

    def test_the_row_given_up_lands_as_expired_not_deleted(self) -> None:
        """Same exit the TTL uses, and the same meaning: she never got
        round to asking. ``expired`` is also the one status the novelty
        gate lets past, so the ground stays open to be wondered again."""
        victim = self._aged("the victim", hours=300.0)
        for i in range(11):
            self._aged(f"filler {i}", hours=250.0)

        self._build(max_open=12, max_per_run=1).run()

        self.assertEqual(
            self.hypotheses.get(victim.hypothesis_id).status, STATUS_EXPIRED
        )

    def test_replacement_can_be_switched_off(self) -> None:
        for i in range(12):
            self._aged(f"guess {i}", hours=250.0)

        result = self._build(max_open=12, evict_when_full=False).run()

        self.assertEqual(result["reason"], "max_open")
        self.assertEqual(self.ollama.calls, 0)

    def test_demand_tells_a_stale_full_shelf_from_a_fresh_one(self) -> None:
        """Reporting both as ``shelf_full`` is how the lane went a week
        without a guess while every signal read healthy."""
        for i in range(12):
            self._aged(f"guess {i}", hours=250.0)

        signal = self._build(max_open=12).demand(
            now=timephrase.utcnow(), last_run_at=None
        )

        self.assertEqual(signal.reason, "shelf_stale")
        self.assertGreater(signal.pressure, 0.0)
        self.assertTrue(signal.needs_llm)

    def test_a_stale_shelf_asks_for_less_than_an_empty_one(self) -> None:
        """One slot's worth. Giving up a guess to make room is worth less
        than filling an empty slot and should lose to real hunger."""
        now = timephrase.utcnow()
        empty = self._build(max_open=12).demand(now=now, last_run_at=None)
        for i in range(12):
            self._aged(f"guess {i}", hours=250.0)

        stale = self._build(max_open=12).demand(now=now, last_run_at=None)

        self.assertEqual(empty.reason, "shortfall")
        self.assertLess(stale.pressure, empty.pressure)


class TtlTests(_Fixture):
    def test_an_untouched_guess_ages_out(self) -> None:
        stale = self._existing("old guess", embedding=_FAR)
        stale.created_at = (
            timephrase.utcnow() - timedelta(hours=400)
        ).isoformat()
        self.hypotheses.update(stale)

        result = self._build(ttl_hours=336.0).run()

        self.assertEqual(result["expired"], 1)
        self.assertEqual(self.hypotheses.get(stale.hypothesis_id).status,
                         STATUS_EXPIRED)

    def test_a_guess_that_got_an_answer_is_left_alone(self) -> None:
        """A clock must not settle a question the user already answered."""
        answered = self._existing("answered guess", embedding=_FAR)
        answered.created_at = (
            timephrase.utcnow() - timedelta(hours=400)
        ).isoformat()
        answered.asked_count = 1
        answered.last_tested_at = timephrase.utcnow().isoformat()
        self.hypotheses.update(answered)

        result = self._build(ttl_hours=336.0).run()

        self.assertEqual(result["expired"], 0)
        self.assertTrue(self.hypotheses.get(answered.hypothesis_id).is_live)

    def test_a_guess_asked_but_never_answered_ages_out(self) -> None:
        """Otherwise it holds a ``max_open`` slot with no way to ever move:
        one ask per invention means it cannot be re-asked, and immunity
        from the clock meant it could not expire either."""
        asked = self._existing("asked guess", embedding=_FAR)
        asked.created_at = (
            timephrase.utcnow() - timedelta(hours=400)
        ).isoformat()
        asked.asked_count = 1
        self.hypotheses.update(asked)

        result = self._build(ttl_hours=336.0).run()

        self.assertEqual(result["expired"], 1)
        self.assertFalse(self.hypotheses.get(asked.hypothesis_id).is_live)


# ── failure paths ─────────────────────────────────────────────────────


class DurabilityTests(_Fixture):
    def test_disabled_does_nothing(self) -> None:
        result = self._build(enabled_provider=lambda: False).run()

        self.assertEqual(result["reason"], "disabled")
        self.assertEqual(self.ollama.calls, 0)

    def test_no_store_does_nothing(self) -> None:
        result = self._build(hypothesis_store_provider=lambda: None).run()

        self.assertEqual(result["reason"], "no_store")

    def test_a_cancelled_run_writes_nothing(self) -> None:
        self.cancel.set()

        result = self._build().run()

        self.assertEqual(result["reason"], "cancelled_before_start")
        self.assertEqual(self.hypotheses.count_live(), 0)

    def test_unparseable_output_is_absorbed(self) -> None:
        result = self._build(payload="I would rather not.").run()

        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["reason"], "no_candidates")

    def test_a_raising_llm_is_absorbed(self) -> None:
        worker = self._build()

        def _boom(*_a, **_k):
            raise RuntimeError("ollama down")

        self.ollama.chat_stream = _boom

        self.assertEqual(worker.run()["wrote"], 0)

    def test_a_raising_embedder_skips_only_that_candidate(self) -> None:
        worker = self._build(
            payload=_payload(_STATEMENT, _OTHER),
            vectors={_OTHER: _FAR},
        )

        def _embed(text: str):
            if text == _STATEMENT:
                raise RuntimeError("embedder down")
            return _FAR

        self.embedder.embed = _embed

        self.assertEqual(worker.run()["wrote"], 1)

    def test_an_empty_statement_is_dropped(self) -> None:
        result = self._build(payload=_payload("   ")).run()

        self.assertEqual(result["wrote"], 0)

    def test_a_missing_concept_store_leaves_the_first_gate_working(
        self,
    ) -> None:
        self._existing(embedding=_NEAR_A)

        result = self._build(concept_store_provider=lambda: None).run()

        self.assertEqual(result["rejected_duplicate"], 1)


class ContextPackTests(_Fixture):
    def test_it_tells_the_model_what_not_to_restate(self) -> None:
        self._concept("Jacob values quiet mornings", embedding=_FAR)
        self._existing("Jacob dislikes video calls", embedding=_FAR)

        self._build().run()

        prompt = self.ollama.messages[-1]["content"]
        self.assertIn("Jacob values quiet mornings", prompt)
        self.assertIn("Jacob dislikes video calls", prompt)

    def test_a_cold_start_still_produces_a_prompt(self) -> None:
        result = self._build().run()

        self.assertEqual(result["wrote"], 1)
        self.assertIn("(nothing settled yet)", self.ollama.messages[-1]["content"])

    def test_optional_providers_are_optional(self) -> None:
        worker = self._build(
            memory_store_provider=lambda: None,
            topic_graph_provider=lambda: None,
        )

        self.assertEqual(worker.run()["wrote"], 0)
        self.assertEqual(self.ollama.calls, 0)

    def test_a_raising_memory_provider_is_absorbed(self) -> None:
        def _boom():
            raise RuntimeError("db locked")

        self.assertEqual(
            self._build(memory_store_provider=_boom).run()["wrote"], 0
        )

    def test_it_uses_the_configured_names(self) -> None:
        self._build(
            user_display_name_provider=lambda: "Jacob",
            assistant_display_name_provider=lambda: "Aiko",
        ).run()

        system = self.ollama.messages[0]["content"]
        self.assertIn("Jacob", system)
        self.assertIn("Aiko", system)


class ParseTests(unittest.TestCase):
    def _parse(self, raw: str):
        return HypothesisProposerWorker._parse(raw)

    def test_it_digs_the_object_out_of_a_preamble(self) -> None:
        raw = "Let me think...\n" + _payload(_STATEMENT)

        self.assertEqual(len(self._parse(raw)), 1)

    def test_a_non_object_reply_yields_nothing(self) -> None:
        self.assertEqual(self._parse('["a", "b"]'), [])

    def test_a_missing_hypotheses_key_yields_nothing(self) -> None:
        self.assertEqual(self._parse('{"seeds": [{"statement": "x"}]}'), [])

    def test_non_dict_entries_are_skipped(self) -> None:
        raw = '{"hypotheses": ["just a string", {"statement": "real"}]}'

        self.assertEqual([e["statement"] for e in self._parse(raw)], ["real"])

    def test_defaults_fill_in_for_a_sparse_entry(self) -> None:
        entry = self._parse('{"hypotheses": [{"statement": "x"}]}')[0]

        self.assertNotIn("subject", entry)
        self.assertNotIn("rationale", entry)


class KnobTests(_Fixture):
    def test_lowering_min_novelty_admits_a_closer_twin(self) -> None:
        self._existing(embedding=_NEAR_A)

        self.assertEqual(
            self._build(min_novelty=0.999).run()["wrote"], 1
        )

    def test_raising_concept_novelty_admits_a_closer_belief(self) -> None:
        self._concept(embedding=_MID_A)

        self.assertEqual(
            self._build(concept_novelty=0.99).run()["wrote"], 1
        )

    def test_zero_ttl_disables_expiry(self) -> None:
        stale = self._existing("old", embedding=_FAR)
        stale.created_at = (
            timephrase.utcnow() - timedelta(hours=9000)
        ).isoformat()
        self.hypotheses.update(stale)

        self.assertEqual(self._build(ttl_hours=0.0).run()["expired"], 0)

    def test_raising_the_eviction_age_bar_protects_a_stale_shelf(self) -> None:
        row = self._existing("old", embedding=_FAR)
        row.created_at = (
            timephrase.utcnow() - timedelta(hours=200)
        ).isoformat()
        self.hypotheses.update(row)

        result = self._build(
            max_open=1, evict_min_age_hours=300.0
        ).run()

        self.assertEqual(result["reason"], "max_open")

    def test_the_default_eviction_bar_is_half_the_default_ttl(self) -> None:
        """They are meant to stay in that ratio -- eviction is early TTL
        under demand, not a second policy with its own opinion."""
        from app.core.infra.memory_settings import MemorySettings

        s = MemorySettings()

        self.assertAlmostEqual(
            s.hypothesis_evict_min_age_hours * 2.0, s.hypothesis_ttl_hours
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class ProposalQualityTests(_Fixture):
    def test_inspiration_is_retained_without_becoming_support(self):
        self._build().run()
        row = self.hypotheses.list_by(live=True)[0]
        self.assertEqual(row.support_count, 0)
        self.assertEqual(row.answer_memory_ids, [])
        self.assertEqual(row.origin_refs, [1])
        self.assertEqual(row.quality["inspiration"]["id"], 1)
        self.hypotheses.load_all()
        loaded = self.hypotheses.get(row.hypothesis_id)
        self.assertEqual(loaded.quality, row.quality)
        self.assertEqual(loaded.quality["alternative"], _motivation()["alternative"])

    def test_old_unreviewed_inventions_yield_without_becoming_refuted(self):
        old = Hypothesis(statement="An arbitrary old invented detail", embedding=_FAR)
        self.hypotheses.add(old)
        result = self._build(max_open=1).run()
        self.assertEqual(result["wrote"], 1)
        self.assertEqual(result["retired_unreviewed"], 1)
        self.assertEqual(self.hypotheses.get(old.hypothesis_id).status, STATUS_EXPIRED)
        self.assertEqual(old.refute_count, 0)

    def test_a_draft_memory_cannot_motivate_a_new_invention_as_an_observation(self):
        def provider():
            return SimpleNamespace(list_recent=lambda **_: [SimpleNamespace(
                id=1, kind="pre_thought", metadata={}, content="An imagined possibility.",
                created_at=timephrase.utcnow().isoformat(),
            )])
        result = self._build(memory_store_provider=provider).run()
        self.assertEqual(result["wrote"], 0)
        self.assertEqual(self.ollama.calls, 0)

    def test_unmotivated_uncheckable_or_malformed_proposals_are_refused(self):
        bad = [
            {"useful_because": ""}, {"alternative": ["a stringified list"]},
            {"inspiration": {"type": "memory", "id": 1, "quote": "fabricated private event"}},
            {"subject": "aiko", "statement": "Aiko keeps a secret journal under her sofa."},
            {"inspiration": {"type": "memory", "id": 999}},
            {"check_question": "You are exactly like that"},
            {"rationale": "You felt this today"}, {"subject": "somebody else"},
            {"statement": "The user subconsciously mirrors a hidden rhythm."},
            {"statement": "The user pauses exactly seven seconds before coding."},
        ]
        for change in bad:
            with self.subTest(change=change):
                payload = json.loads(_payload(_STATEMENT))
                payload["hypotheses"][0].update(change)
                result = self._build(payload=json.dumps(payload)).run()
                self.assertEqual(result["wrote"], 0)
                self.assertEqual(result["rejected_quality"], 1)

    def test_intentional_abstention_has_no_quality_error_or_new_row(self):
        result = self._build(payload='{"hypotheses": []}').run()
        self.assertEqual(result["wrote"], 0)
        self.assertEqual(self.hypotheses.count_live(), 0)

    def test_only_packed_source_headers_can_authorize_an_inspiration(self):
        from app.core.proactive.worker_prompt_budget import pack_worker_prompt

        notes = [SimpleNamespace(
            id=1, kind="fact", metadata={}, created_at=timephrase.utcnow().isoformat(),
            content="A note mentions [memory:2; omitted source] as a text reference.",
        ), SimpleNamespace(
            id=2, kind="fact", metadata={}, created_at=timephrase.utcnow().isoformat(),
            content="The user described organising a desk before a project.",
        )]
        worker = self._build(
            memory_store_provider=lambda: SimpleNamespace(list_recent=lambda **_: notes),
            payload=_payload(_STATEMENT, inspiration={"type": "memory", "id": 2,
                "quote": "organising a desk before a project"}),
        )

        def fit_one(items, render, **kwargs):
            return pack_worker_prompt(items[:1], render, **kwargs)

        with patch("app.core.proactive.hypothesis_proposer_worker.pack_worker_prompt",
                   side_effect=fit_one):
            result = worker.run()
        self.assertEqual(result["wrote"], 0)
        self.assertEqual(result["quality_faults"], {"unoffered_inspiration": 1})
