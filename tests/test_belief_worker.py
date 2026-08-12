"""Tests for :mod:`app.core.relationship.belief_worker`."""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.core.relationship.belief_store import BeliefStore, KIND_MOOD, KIND_OPINION
from app.core.relationship.belief_worker import BeliefInferenceWorker
from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter


class _StubEmbedder:
    """Deterministic 4-dim embedder so tests can assert on the cosine path.

    Uses md5 instead of ``hash()`` so the same token maps to the same
    slot across Python runs (``PYTHONHASHSEED`` is randomised by default).
    """

    DIM = 4

    @staticmethod
    def _slot(token: str) -> int:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "little") % _StubEmbedder.DIM

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in (text or "").lower().split():
            vec[self._slot(token)] += 1.0
        norm = float(np.linalg.norm(vec))
        if norm > 0.0:
            vec /= norm
        return vec


@dataclass
class _StubOllama:
    """Yields one pre-canned JSON-array response per chat_stream call."""

    responses: list[str] = field(default_factory=list)
    chat_calls: list[dict[str, Any]] = field(default_factory=list)
    raise_on_call: bool = False

    def chat_stream(
        self,
        messages: list[dict[str, Any]],
        options: dict[str, Any] | None = None,
        *,
        model: str | None = None,
        keep_alive: str | None = None,
        stop_event: threading.Event | None = None,
        format_json: bool = False,
        think: bool = False,
        **kwargs: Any,
    ) -> Iterable[str]:
        self.chat_calls.append({
            "messages": messages,
            "format_json": format_json,
        })
        if self.raise_on_call:
            raise RuntimeError("simulated ollama outage")
        if not self.responses:
            yield "[]"
            return
        yield self.responses.pop(0)


@dataclass
class _StubAgent:
    belief_tracking_enabled: bool = True
    belief_worker_enabled: bool = True
    belief_interest_bias_enabled: bool = True
    belief_worker_per_hour_cap: int = 10
    belief_worker_per_day_cap: int = 50
    belief_worker_scrub_transcript: bool = False


class _StubConcept:
    def __init__(self, label: str, *, kind: str = "identity") -> None:
        self.label = label
        self.kind = kind
        self.subject = "user"
        self.confidence = 0.8
        self.status = "active"


class _StubView:
    """A ConceptView narrowed to the one read the worker performs."""

    def __init__(
        self,
        rows: list[_StubConcept] | None = None,
        *,
        enabled: bool = True,
        raises: bool = False,
    ) -> None:
        self._rows = list(rows or [])
        self.enabled = enabled
        self._raises = raises
        self.consumers: list[str] = []

    def for_consumer(self, consumer, *, subject=None):
        self.consumers.append(str(consumer))
        if self._raises:
            raise RuntimeError("store is gone")
        return list(self._rows)


@dataclass
class _StubBeliefSettings:
    belief_worker_interval_seconds: int = 3600
    belief_worker_lookback_turns: int = 12
    belief_worker_interest_top_n: int = 5
    belief_worker_reconsider_max: int = 3
    belief_max_active_per_user: int = 200


def _build_world(
    *,
    responses: list[str] | None = None,
    cap_hour: int = 10,
    cap_day: int = 50,
    # Shaped like the real thing: ``messages.session_id`` holds the scoped
    # ``user_id:session_id`` key, and wiring the bare id here is why this
    # worker mined one belief in three months.
    session_key: str = "u1:session-1",
    user_messages: list[str] | None = None,
    agent: "_StubAgent | None" = None,
    interest_map: Any = None,
    view: "_StubView | None" = None,
) -> tuple[BeliefInferenceWorker, BeliefStore, _StubOllama, FactCheckRateLimiter]:
    tmp = tempfile.mkdtemp()
    db = ChatDatabase(Path(tmp) / "t.db")
    store = BeliefStore(db)
    ollama = _StubOllama(responses=list(responses or []))
    rate_limiter = FactCheckRateLimiter(
        db,
        per_hour_cap=cap_hour,
        per_day_cap=cap_day,
        state_key="belief_worker.test",
    )
    # Seed the message store with some user turns.
    if user_messages is None:
        user_messages = [
            "I'm so excited about the Tokyo trip next month!",
            "Rust language really feels overhyped to me lately.",
        ]
    for content in user_messages:
        db.add_message(
            session_id=session_key, role="user", content=content,
        )
        db.add_message(
            session_id=session_key, role="assistant",
            content="ack",
        )
    worker = BeliefInferenceWorker(
        belief_store=store,
        chat_db=db,
        embedder=_StubEmbedder(),
        ollama=ollama,
        chat_model="llama3:latest",
        rate_limiter=rate_limiter,
        cancel_event=threading.Event(),
        agent_settings=agent or _StubAgent(),
        belief_settings=_StubBeliefSettings(),
        session_key_provider=lambda: session_key,
        user_id_provider=lambda: "u1",
        user_names_provider=lambda: ["Jacob"],
        assistant_name_provider=lambda: "Aiko",
        interest_map_provider=(lambda: interest_map) if interest_map is not None else None,
        view_provider=(lambda: view) if view is not None else None,
    )
    return worker, store, ollama, rate_limiter


class SessionKeyTests(unittest.TestCase):
    """The worker keys every chat-db read on the scoped
    ``user_id:session_id``. Wired to the bare session id it mined one
    belief in three months while logging the benign-looking "no recent
    user turns", so a mismatch has to be loud and distinguishable from an
    idle window."""

    def test_a_key_that_names_no_session_is_reported_as_a_fault(self) -> None:
        worker, store, ollama, _rl = _build_world(
            session_key="session-1",  # unscoped: the shipped bug
            user_messages=[],
        )
        worker._chat_db.add_message(
            session_id="u1:session-1", role="user", content="I love Tokyo.",
        )
        out = worker.run()
        self.assertEqual(out.get("reason"), "unknown_session")
        self.assertEqual(ollama.chat_calls, [])

    def test_an_idle_window_is_not_reported_as_a_fault(self) -> None:
        worker, _store, _ollama, _rl = _build_world()
        worker._snapshot_transcript = lambda **_kw: ""
        self.assertEqual(worker.run().get("reason"), "no_user_turns")

    def test_the_demand_probe_reads_the_same_key_the_run_does(self) -> None:
        worker, _store, _ollama, _rl = _build_world()
        signal = worker.demand(now=datetime.now(timezone.utc), last_run_at=None)
        assert signal is not None
        self.assertGreater(signal.pressure, 0.0)
        self.assertNotEqual(
            worker._snapshot_transcript(
                session_key="u1:session-1", lookback_turns=12,
            ),
            "",
        )


class ExtractionTests(unittest.TestCase):
    def test_run_upserts_beliefs_from_llm(self) -> None:
        payload = json.dumps([
            {
                "kind": "mood",
                "topic": "tokyo trip",
                "predicted_state": "excited",
                "confidence": 0.8,
            },
            {
                "kind": "opinion",
                "topic": "rust language",
                "predicted_state": "overhyped",
                "confidence": 0.6,
            },
        ])
        worker, store, ollama, _ = _build_world(responses=[payload])
        result = worker.run()
        self.assertEqual(result["upserted"], 2)
        self.assertEqual(len(ollama.chat_calls), 1)
        beliefs = store.list_active(user_id="u1")
        topics = {b.topic for b in beliefs}
        self.assertIn("tokyo trip", topics)
        self.assertIn("rust language", topics)

    def test_invalid_kind_dropped(self) -> None:
        payload = json.dumps([
            {
                "kind": "bogus",
                "topic": "x",
                "predicted_state": "y",
                "confidence": 0.5,
            },
            {
                "kind": "mood",
                "topic": "ok",
                "predicted_state": "y",
                "confidence": 0.5,
            },
        ])
        worker, store, _, _ = _build_world(responses=[payload])
        result = worker.run()
        # Only the valid one lands.
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(len(store.list_active(user_id="u1")), 1)

    def test_empty_array_no_upserts(self) -> None:
        worker, store, _, _ = _build_world(responses=["[]"])
        result = worker.run()
        self.assertEqual(result["upserted"], 0)
        self.assertEqual(len(store.list_active(user_id="u1")), 0)

    def test_unparseable_response_returns_skipped(self) -> None:
        worker, _, _, _ = _build_world(responses=["not json at all"])
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "llm_unparseable")

    def test_object_wrapped_array_is_the_shape_we_ask_for(self) -> None:
        # ``format: "json"`` constrains output to an object, so the prompt
        # asks for {"beliefs": [...]}. The old parser demanded a bare array
        # and reported 26 of these as llm-unparseable.
        payload = json.dumps({
            "beliefs": [
                {
                    "kind": "opinion",
                    "topic": "rust tooling",
                    "predicted_state": "worth the learning curve",
                    "confidence": 0.7,
                },
            ],
        })
        worker, store, _, _ = _build_world(responses=[payload])
        result = worker.run()
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(len(store.list_active(user_id="u1")), 1)

    def test_bare_empty_object_means_no_beliefs(self) -> None:
        worker, store, _, _ = _build_world(responses=["{}"])
        result = worker.run()
        self.assertEqual(result.get("upserted"), 0)
        self.assertEqual(len(store.list_active(user_id="u1")), 0)

    def test_empty_answer_is_a_failure_not_zero_beliefs(self) -> None:
        # An answer with no tokens at all means the reasoning trace ate
        # the budget, not that the transcript was quiet.
        worker, store, _, _ = _build_world(responses=[""])
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "llm_unparseable")
        self.assertEqual(len(store.list_active(user_id="u1")), 0)

    def test_the_user_prompt_renders_without_a_format_error(self) -> None:
        # The literal ``{"beliefs": [...]}`` in the template must be
        # brace-escaped or ``str.format`` raises KeyError on every run.
        worker, _, ollama, _ = _build_world(responses=['{"beliefs": []}'])
        worker.run()
        prompt = ollama.chat_calls[0]["messages"][-1]["content"]
        self.assertIn('{"beliefs": [...]}', prompt)


class RateLimitTests(unittest.TestCase):
    def test_rate_limited_skip(self) -> None:
        worker, store, ollama, limiter = _build_world(
            cap_hour=1, cap_day=1,
            responses=["[]"],
        )
        # Burn the only token so the second call defers.
        self.assertTrue(limiter.allow(datetime.now(timezone.utc)))
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "rate_limited")
        # Worker shouldn't have called the LLM.
        self.assertEqual(len(ollama.chat_calls), 0)


class DemandTests(unittest.TestCase):
    """The P44 probe: has any new transcript arrived to mine.

    This worker has no backlog. Left on a pure interval it would spend
    a generation re-deriving the same beliefs from an unchanged
    window, so the signal is new material rather than pending work.
    """

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def test_unmined_transcript_reports_pressure(self) -> None:
        worker, _store, _ollama, _limiter = _build_world()
        signal = worker.demand(now=self._now(), last_run_at=None)
        self.assertGreater(signal.pressure, 0.0)
        self.assertTrue(signal.needs_llm)

    def test_nothing_new_since_the_last_run_is_zero(self) -> None:
        worker, _store, _ollama, _limiter = _build_world()
        # Everything in the fixture predates this watermark.
        later = self._now() + timedelta(hours=1)
        signal = worker.demand(now=later, last_run_at=later)
        self.assertEqual(signal.pressure, 0.0)
        self.assertEqual(signal.reason, "no new turns")

    def test_spent_budget_reports_nothing(self) -> None:
        worker, _store, _ollama, limiter = _build_world(
            cap_hour=1, cap_day=1,
        )
        self.assertTrue(limiter.allow(self._now()))
        signal = worker.demand(now=self._now(), last_run_at=None)
        self.assertEqual(signal.pressure, 0.0)
        self.assertFalse(worker.is_ready(now=self._now(), last_run_at=None))

    def test_probe_neither_spends_a_token_nor_calls_the_model(self) -> None:
        worker, store, ollama, limiter = _build_world()
        before = limiter.snapshot(self._now())["hour_used"]
        now = self._now()
        worker.demand(now=now, last_run_at=None)
        worker.is_ready(now=now, last_run_at=None)
        self.assertEqual(limiter.snapshot(now)["hour_used"], before)
        self.assertEqual(len(ollama.chat_calls), 0)
        self.assertEqual(len(store.list_active(user_id="u1")), 0)


class PrivacyScrubTests(unittest.TestCase):
    def test_url_only_message_blocks_extraction_when_scrub_enabled(self) -> None:
        # With scrubbing opted in, a message that's basically just a
        # URL/email -> the privacy scrubber bails and the worker never
        # calls the LLM.
        worker, _, ollama, _ = _build_world(
            user_messages=["https://example.com/dashboard?token=abcdef123"],
            responses=["[]"],
            agent=_StubAgent(belief_worker_scrub_transcript=True),
        )
        result = worker.run()
        self.assertTrue(result.get("skipped"))
        self.assertEqual(result.get("reason"), "privacy_blocked")
        self.assertEqual(len(ollama.chat_calls), 0)

    def test_scrub_off_by_default_keeps_pronouns_and_names(self) -> None:
        # Default (no scrub): the local extractor must SEE the deictic
        # signal -- the user's name prefix and first/second person.
        worker, _, ollama, _ = _build_world(
            user_messages=["I really think you nailed my Tokyo plan."],
            responses=["[]"],
        )
        result = worker.run()
        self.assertNotEqual(result.get("reason"), "privacy_blocked")
        self.assertEqual(len(ollama.chat_calls), 1)
        prompt = ollama.chat_calls[0]["messages"][-1]["content"]
        # User turns are attributed by name and pronouns survive.
        self.assertIn("Jacob:", prompt)
        self.assertIn("you", prompt)
        self.assertIn("my", prompt)

    def test_scrub_on_strips_pronouns_and_names(self) -> None:
        # Opt-in scrub removes the name prefix + first/second person, as
        # the outbound web-search gate is designed to.
        worker, _, ollama, _ = _build_world(
            user_messages=["I really think you nailed my Tokyo plan today."],
            responses=["[]"],
            agent=_StubAgent(belief_worker_scrub_transcript=True),
        )
        worker.run()
        self.assertEqual(len(ollama.chat_calls), 1)
        prompt = ollama.chat_calls[0]["messages"][-1]["content"]
        self.assertNotIn("Jacob:", prompt)
        self.assertNotIn("today", prompt)


class LookbackTests(unittest.TestCase):
    def test_lookback_window_caps_user_turns(self) -> None:
        # 30 short user turns; lookback_turns=12 means only the last
        # 12 hit the prompt.
        msgs = [f"belief about topic_{i}" for i in range(30)]
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            user_messages=msgs,
        )
        worker.run()
        self.assertEqual(len(ollama.chat_calls), 1)
        prompt = ollama.chat_calls[0]["messages"][-1]["content"]
        # Last 12 topics should be present; earlier ones absent.
        self.assertIn("topic_29", prompt)
        self.assertIn("topic_18", prompt)
        self.assertNotIn("topic_5", prompt)


class SelfTagGuardTests(unittest.TestCase):
    def test_self_tag_wins_over_lower_confidence_worker(self) -> None:
        # Seed a high-confidence self-tag belief; worker returns the
        # same topic at lower confidence -> should be skipped.
        worker, store, _, _ = _build_world(
            responses=[json.dumps([
                {
                    "kind": "mood",
                    "topic": "tokyo trip",
                    "predicted_state": "nervous",
                    "confidence": 0.4,
                },
            ])],
        )
        existing = store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", confidence=0.85,
            source="self_tag",
        )
        assert existing is not None
        result = worker.run()
        self.assertEqual(result["skipped_self_tag"], 1)
        self.assertEqual(result["upserted"], 0)
        # The high-confidence self-tag belief is preserved.
        latest = store.get(existing.id)
        self.assertEqual(latest.predicted_state, "excited")
        self.assertEqual(latest.confidence, 0.85)


class _InterestEntry:
    """Mimics topic_graph.InterestEntry (has .label / .size)."""

    def __init__(self, label: str, size: int) -> None:
        self.label = label
        self.size = size


class CoerceLabelsTests(unittest.TestCase):
    def test_accepts_strings_tuples_and_objects(self) -> None:
        from app.core.relationship.belief_worker import _coerce_labels

        out = _coerce_labels([
            "bare label",
            ("tuple label", 7),
            _InterestEntry("object label", 4),
        ])
        self.assertEqual(out, ["bare label", "tuple label", "object label"])

    def test_dedupes_and_drops_blanks(self) -> None:
        from app.core.relationship.belief_worker import _coerce_labels

        out = _coerce_labels(["Cats", "", "  ", "cats", "Dogs"])
        self.assertEqual(out, ["Cats", "Dogs"])

    def test_empty_and_non_iterable(self) -> None:
        from app.core.relationship.belief_worker import _coerce_labels

        self.assertEqual(_coerce_labels(None), [])
        self.assertEqual(_coerce_labels([]), [])
        self.assertEqual(_coerce_labels(123), [])


class InterestBiasTests(unittest.TestCase):
    def _prompt(self, ollama: _StubOllama) -> str:
        return ollama.chat_calls[0]["messages"][-1]["content"]

    def test_interest_hint_lands_in_prompt(self) -> None:
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            interest_map=[
                _InterestEntry("tokyo travel", 9),
                _InterestEntry("rust programming", 5),
            ],
        )
        worker.run()
        prompt = self._prompt(ollama)
        self.assertIn("keeps returning to", prompt)
        self.assertIn("tokyo travel", prompt)
        self.assertIn("rust programming", prompt)

    def test_no_provider_is_legacy_prompt(self) -> None:
        # interest_map=None -> provider not wired -> byte-identical legacy
        # prompt with no interest hint section.
        worker, _, ollama, _ = _build_world(responses=["[]"])
        worker.run()
        prompt = self._prompt(ollama)
        self.assertNotIn("keeps returning to", prompt)
        self.assertNotIn("re-check whether", prompt)

    def test_master_switch_off_suppresses_hint(self) -> None:
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            agent=_StubAgent(belief_interest_bias_enabled=False),
            interest_map=[_InterestEntry("tokyo travel", 9)],
        )
        worker.run()
        self.assertNotIn("keeps returning to", self._prompt(ollama))

    def test_reconsider_includes_stale_active_belief_on_hot_interest(self) -> None:
        worker, store, ollama, _ = _build_world(
            responses=["[]"],
            interest_map=[_InterestEntry("tokyo travel", 9)],
        )
        # Active belief whose topic shares the word "tokyo" with the
        # interest label -> nominated for a re-check.
        store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", confidence=0.6, source="worker",
        )
        worker.run()
        prompt = self._prompt(ollama)
        self.assertIn("re-check whether", prompt)
        self.assertIn("tokyo trip", prompt)

    def test_reconsider_skips_unrelated_active_belief(self) -> None:
        worker, store, ollama, _ = _build_world(
            responses=["[]"],
            interest_map=[_InterestEntry("tokyo travel", 9)],
        )
        store.upsert(
            user_id="u1", kind=KIND_OPINION, topic="database indexing",
            predicted_state="tedious", confidence=0.6, source="worker",
        )
        worker.run()
        prompt = self._prompt(ollama)
        # No topical overlap -> no reconsider block at all.
        self.assertNotIn("re-check whether", prompt)

    def test_reconsider_cap_respected(self) -> None:
        worker, store, ollama, _ = _build_world(
            responses=["[]"],
            interest_map=[_InterestEntry("tokyo travel", 9)],
        )
        for i in range(6):
            store.upsert(
                user_id="u1", kind=KIND_MOOD, topic=f"tokyo plan {i}",
                predicted_state="keen", confidence=0.5, source="worker",
            )
        worker.run()
        prompt = self._prompt(ollama)
        # reconsider_max defaults to 3 -> at most 3 topics enumerated.
        mentioned = sum(1 for i in range(6) if f"tokyo plan {i}" in prompt)
        self.assertLessEqual(mentioned, 3)
        self.assertGreaterEqual(mentioned, 1)

    def test_pii_only_label_scrubbed_out(self) -> None:
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            interest_map=[
                _InterestEntry("test@example.com", 9),
                _InterestEntry("weekend hiking", 5),
            ],
        )
        worker.run()
        prompt = self._prompt(ollama)
        self.assertNotIn("test@example.com", prompt)
        self.assertIn("weekend hiking", prompt)

    def test_still_single_llm_call(self) -> None:
        # The whole point of K65b: the re-check rides the SAME extraction
        # call, no extra LLM spend.
        worker, store, ollama, _ = _build_world(
            responses=["[]"],
            interest_map=[_InterestEntry("tokyo travel", 9)],
        )
        store.upsert(
            user_id="u1", kind=KIND_MOOD, topic="tokyo trip",
            predicted_state="excited", confidence=0.6, source="worker",
        )
        worker.run()
        self.assertEqual(len(ollama.chat_calls), 1)


class ConceptBiasTests(unittest.TestCase):
    """L28: the durable layer as a third prior on what to look for.

    K65b's two hints are both *topic* signals. Neither says what he is
    like, so the extractor reads a passing mood with no model of the
    person it is inferring about.
    """

    def _prompt(self, ollama: _StubOllama) -> str:
        return ollama.chat_calls[0]["messages"][-1]["content"]

    def test_concepts_reach_the_extraction_prompt(self) -> None:
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            view=_StubView([_StubConcept("goes quiet when overloaded")]),
        )
        worker.run()
        self.assertIn("goes quiet when overloaded", self._prompt(ollama))

    def test_the_prompt_says_the_transcript_decides(self) -> None:
        # A prior that reads as evidence would let the extractor confirm
        # the concept layer from a transcript that contradicts it.
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            view=_StubView([_StubConcept("goes quiet when overloaded")]),
        )
        worker.run()
        prompt = self._prompt(ollama)
        self.assertIn("not evidence", prompt)
        self.assertIn("may well contradict", prompt)

    def test_it_reads_the_declared_diet(self) -> None:
        view = _StubView([_StubConcept("goes quiet when overloaded")])
        worker, _, _, _ = _build_world(responses=["[]"], view=view)
        worker.run()
        self.assertEqual(view.consumers, ["belief_inference"])

    def test_the_hint_is_capped(self) -> None:
        # The diet's token budget governs how much is read; this cap is
        # how much is worth spending prompt on, so a long list cannot
        # start competing with the transcript that decides.
        words = [
            "alpha", "bravo", "charlie", "delta", "echo", "foxtrot",
            "golf", "hotel", "india", "juliet", "kilo", "lima",
        ]
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            view=_StubView([_StubConcept(f"likes {w}") for w in words]),
        )
        worker.run()
        prompt = self._prompt(ollama)
        mentioned = sum(1 for w in words if f"likes {w}" in prompt)
        self.assertEqual(mentioned, 5)

    def test_a_pii_only_label_is_scrubbed_out(self) -> None:
        # Concept labels are free text about a named person and the worker
        # may be routed to an untrusted endpoint, so they go through the
        # same gate as the interest labels.
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            view=_StubView([
                _StubConcept("test@example.com"),
                _StubConcept("prefers mornings"),
            ]),
        )
        worker.run()
        prompt = self._prompt(ollama)
        self.assertNotIn("test@example.com", prompt)
        self.assertIn("prefers mornings", prompt)

    def test_no_view_is_the_legacy_prompt(self) -> None:
        worker, _, ollama, _ = _build_world(responses=["[]"])
        worker.run()
        self.assertNotIn("durably hold", self._prompt(ollama))

    def test_a_cold_or_broken_view_is_not_a_failed_run(self) -> None:
        for view in (_StubView(enabled=False), _StubView(raises=True)):
            worker, _, ollama, _ = _build_world(responses=["[]"], view=view)
            self.assertEqual(worker.run().get("upserted"), 0)
            self.assertNotIn("durably hold", self._prompt(ollama))

    def test_it_still_rides_the_same_single_llm_call(self) -> None:
        worker, _, ollama, _ = _build_world(
            responses=["[]"],
            interest_map=[_InterestEntry("tokyo travel", 9)],
            view=_StubView([_StubConcept("goes quiet when overloaded")]),
        )
        worker.run()
        self.assertEqual(len(ollama.chat_calls), 1)

    def test_nothing_is_written_back_to_the_concept_layer(self) -> None:
        # K2 stays transient in both directions: the durable layer shapes
        # what the extractor looks for and learns nothing from the result.
        view = _StubView([_StubConcept("goes quiet when overloaded")])
        payload = json.dumps([{
            "kind": "mood", "topic": "work", "predicted_state": "flat",
            "confidence": 0.7,
        }])
        worker, _, _, _ = _build_world(responses=[payload], view=view)
        worker.run()
        self.assertEqual(view.consumers, ["belief_inference"])
        self.assertFalse(hasattr(view, "upsert"))


if __name__ == "__main__":
    unittest.main()
