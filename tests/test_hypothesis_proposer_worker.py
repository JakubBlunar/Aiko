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
from datetime import timedelta
from pathlib import Path
from typing import Any

import numpy as np

from app.core.concepts.concept_store import Concept, ConceptStore
from app.core.concepts.hypothesis_store import (
    ORIGIN_FREE,
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


def _payload(*statements: str, **fields: Any) -> str:
    return json.dumps(
        {
            "hypotheses": [
                {
                    "statement": s,
                    "kind": fields.get("kind", "pattern"),
                    "subject": fields.get("subject", "user"),
                    "rationale": "a guess",
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
            statement=statement, subject=subject, embedding=embedding
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
        self.assertEqual(row.origin, ORIGIN_FREE)

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

        self.assertGreater(
            float(self.ollama.kwargs["options"]["temperature"]), 0.8
        )

    def test_a_bad_subject_falls_back_rather_than_being_stored(self) -> None:
        self._build(payload=_payload(_STATEMENT, subject="the moon")).run()

        self.assertEqual(self.hypotheses.list_by(live=True)[0].subject, "user")


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

        self.assertEqual(worker.run()["wrote"], 1)

    def test_a_raising_memory_provider_is_absorbed(self) -> None:
        def _boom():
            raise RuntimeError("db locked")

        self.assertEqual(
            self._build(memory_store_provider=_boom).run()["wrote"], 1
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

        self.assertEqual(entry["subject"], "user")
        self.assertEqual(entry["kind"], "identity")


class SettingsWiringTests(_Fixture):
    """The knobs must actually reach the gates they name."""

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


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
