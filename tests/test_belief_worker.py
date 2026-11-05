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


def _reading(**overrides):
    item = {
        "kind": "mood", "topic": "tokyo trip preparations",
        "interpretation": ("Jacob may welcome sharing ideas for the Tokyo trip, "
                           "while the amount of planning he wants is unclear."),
        "reason": "He describes excitement about the trip.",
        "situation": "Discussing preparations for the Tokyo trip.",
        "uncertainty": "Excitement alone does not establish a request for planning help.",
        "revise_when": "He asks for quiet company or wants to plan independently.",
        "scope": "situation",
        "novelty": "A possible conversational opening, rather than a general personality claim.",
        "confidence": 0.65,
        "evidence": [{"message_id": 1, "quote": "excited about the Tokyo trip"}],
    }
    return {**item, **overrides}


def _answer(*items):
    return json.dumps({"beliefs": list(items)})


class InterpretationTests(unittest.TestCase):
    def test_a_new_idea_needs_no_concept_and_retains_its_unknowns(self):
        worker, store, _, _ = _build_world(responses=[_answer(_reading())])
        result = worker.run()
        self.assertEqual(result["upserted"], 1)
        row = store.list_active(user_id="u1")[0]
        self.assertEqual(row.predicted_state, _reading()["interpretation"])
        self.assertIn("does not establish", row.metadata["uncertainty"])
        self.assertEqual(row.source_message_id, 1)  # not newest message 3
        self.assertEqual(row.metadata["evidence"][0]["message_id"], 1)
        self.assertEqual(row.status, "active")

    def test_replaying_a_window_survives_worker_restart_without_spending_a_call(self):
        worker, store, ollama, _ = _build_world(responses=[_answer(_reading())])
        worker.run()
        # Intake boundary belongs to SQLite, not the worker instance.
        clone = object.__new__(BeliefInferenceWorker)
        clone.__dict__.update(worker.__dict__)
        self.assertEqual(clone.run()["reason"], "no_new_user_turns")
        self.assertEqual(len(ollama.chat_calls), 1)
        self.assertEqual(store.list_active(user_id="u1")[0].metadata["observations"], 1)

    def test_an_empty_result_also_consumes_the_window(self):
        worker, _, ollama, _ = _build_world(responses=[_answer()])
        self.assertEqual(worker.run()["upserted"], 0)
        self.assertFalse(worker.is_ready(now=datetime.now(timezone.utc), last_run_at=None))
        self.assertEqual(worker.run()["reason"], "no_new_user_turns")
        self.assertEqual(len(ollama.chat_calls), 1)

    def test_an_unusable_object_is_not_confused_with_deliberate_abstention(self):
        worker, store, _, _ = _build_world(responses=[
            _answer(_reading(reason=["not a string"])), _answer(_reading()),
        ])
        first = worker.run()
        self.assertEqual(first["upserted"], 0)
        self.assertTrue(first["retryable_format"])
        self.assertEqual(worker.run()["upserted"], 1)
        self.assertEqual(len(store.list_active(user_id="u1")), 1)

    def test_failed_generation_remains_retryable(self):
        worker, store, ollama, _ = _build_world(responses=["", _answer(_reading())])
        self.assertEqual(worker.run()["reason"], "llm_unparseable")
        self.assertEqual(worker.run()["upserted"], 1)
        self.assertEqual(len(ollama.chat_calls), 2)
        self.assertEqual(len(store.list_active(user_id="u1")), 1)

    def test_adding_a_turn_does_not_make_old_quotes_new_evidence(self):
        worker, store, ollama, _ = _build_world(
            responses=[_answer(_reading()), _answer(_reading())])
        worker.run()
        worker._chat_db.add_message(session_id="u1:session-1", role="user",
                                    content="An unrelated new message.")
        result = worker.run()
        self.assertEqual(result["dropped_invalid"], 1)
        self.assertEqual(store.list_active(user_id="u1")[0].metadata["observations"], 1)
        self.assertEqual(len(ollama.chat_calls), 2)

    def test_a_failed_attempt_remains_ready_after_its_scheduler_timestamp(self):
        worker, _, _, _ = _build_world(responses=[""])
        worker.run()
        now = datetime.now(timezone.utc)
        self.assertTrue(worker.is_ready(now=now, last_run_at=now))

    def test_two_labels_for_the_exact_same_meaning_do_not_make_two_rows(self):
        worker, store, _, _ = _build_world(responses=[_answer(
            _reading(), _reading(topic="travel enthusiasm"))])
        worker.run()
        rows = store.list_active(user_id="u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].metadata["observations"], 1)

    def test_a_changed_qualifier_cannot_be_attached_as_corroboration(self):
        worker, store, ollama, _ = _build_world(responses=[_answer(_reading())])
        worker.run()
        prior = store.list_active(user_id="u1")[0]
        mid = worker._chat_db.add_message(session_id="u1:session-1", role="user",
                                          content="I am excited about the Tokyo trip, still.")
        if hasattr(mid, "id"):
            mid = mid.id
        ollama.responses.append(_answer(_reading(
            existing_id=prior.id, uncertainty="He definitely wants Aiko to plan everything.",
            evidence=[{"message_id": mid, "quote": "excited about the Tokyo trip"}])))
        self.assertEqual(worker.run()["dropped_invalid"], 1)
        self.assertEqual(store.get(prior.id).metadata["uncertainty"],
                         _reading()["uncertainty"])

    def test_switching_sessions_during_generation_discards_the_old_result(self):
        worker, store, ollama, _ = _build_world(responses=[_answer(_reading())])
        original = ollama.chat_stream
        def switch(*args, **kwargs):
            yield from original(*args, **kwargs)
            worker._session_key_provider = lambda: "u1:other-session"
        ollama.chat_stream = switch
        self.assertEqual(worker.run()["reason"], "context_changed")
        self.assertEqual(store.list_active(user_id="u1"), [])

    def test_unquoted_or_assistant_evidence_does_not_create_a_belief(self):
        for evidence in ([{"message_id": 2, "quote": "assistant invents a feeling"}],
                         [{"message_id": 100, "quote": "a different conversation"}],
                         [{"message_id": 1, "quote": "I want extensive planning help"}]):
            with self.subTest(evidence=evidence):
                worker, store, _, _ = _build_world(responses=[_answer(_reading(evidence=evidence))])
                self.assertEqual(worker.run()["upserted"], 0)
                self.assertEqual(store.list_active(user_id="u1"), [])

    def test_only_budgeted_source_words_can_be_cited(self):
        worker, store, _, _ = _build_world(responses=[_answer(_reading())])
        # Simulate the packer omitting the older message, without mocking the
        # validator: generation returns a quote from the fetched, unoffered row.
        original = worker._snapshot_transcript
        def recent_only(**kwargs):
            full = original(**kwargs)
            return "\n".join(line for line in full.splitlines() if "message_id=1;" not in line)
        worker._snapshot_transcript = recent_only
        self.assertEqual(worker.run()["upserted"], 0)
        self.assertEqual(store.list_active(user_id="u1"), [])

    def test_old_format_and_incomplete_subjects_are_rejected(self):
        for item in ({"kind": "mood", "topic": "relationship with her",
                      "predicted_state": "deeply affectionate"},
                     _reading(topic="caring for")):
            worker, store, _, _ = _build_world(responses=[_answer(item)])
            self.assertEqual(worker.run()["upserted"], 0)
            self.assertEqual(store.list_active(user_id="u1"), [])

    def test_one_invalid_item_does_not_hide_a_complete_reading(self):
        worker, store, _, _ = _build_world(responses=[_answer(
            _reading(reason=["not a string"]), _reading())])
        result = worker.run()
        self.assertEqual(result["upserted"], 1)
        self.assertEqual(result["dropped_shape"], 1)

    def test_authored_relative_dates_are_refused_but_source_quotes_are_verbatim(self):
        for key in ("interpretation", "reason", "situation", "uncertainty", "novelty"):
            worker, _, _, _ = _build_world(responses=[_answer(_reading(**{key: "Tired today"}))])
            self.assertEqual(worker.run()["upserted"], 0)
        worker, store, _, _ = _build_world(
            user_messages=["I am tired tonight, but stay with me."],
            responses=[_answer(_reading(evidence=[
                {"message_id": 1, "quote": "I am tired tonight"}]))])
        self.assertEqual(worker.run()["upserted"], 1)
        quote = store.list_active(user_id="u1")[0].metadata["evidence"][0]["quote"]
        self.assertIn("tonight", quote)

    def test_source_times_are_compared_as_instants_across_offsets(self):
        worker, store, _, _ = _build_world(
            user_messages=["I am excited about the Tokyo trip.",
                           "I remain excited about the Tokyo trip."],
            responses=[_answer(_reading(evidence=[
                {"message_id": 1, "quote": "excited about the Tokyo trip"},
                {"message_id": 3, "quote": "excited about the Tokyo trip"},
            ]))])
        worker._clock = lambda: datetime(2026, 11, 5, 10, 5, tzinfo=timezone.utc)
        worker._chat_db.execute_commit("UPDATE messages SET created_at = ? WHERE id = 1",
                                      ("2026-11-05T10:30:00+01:00",))
        worker._chat_db.execute_commit("UPDATE messages SET created_at = ? WHERE id = 3",
                                      ("2026-11-05T10:00:00+00:00",))
        self.assertEqual(worker.run()["upserted"], 1)
        self.assertEqual(store.list_active(user_id="u1")[0].observed_at,
                         "2026-11-05T10:00:00+00:00")

    def test_support_age_not_run_age_controls_expiry(self):
        worker, store, _, _ = _build_world(responses=[_answer(_reading(scope="exchange"))])
        stamp = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
        worker._chat_db.execute_commit("UPDATE messages SET created_at = ?", (stamp,))
        self.assertEqual(worker.run()["upserted"], 0)
        self.assertEqual(store.list_active(user_id="u1"), [])

    def test_paraphrase_reuses_offered_full_reading_without_confirming(self):
        worker, store, ollama, _ = _build_world(responses=[_answer(_reading())])
        worker.run()
        prior = store.list_active(user_id="u1")[0]
        message = worker._chat_db.add_message(session_id="u1:session-1", role="user",
                                              content="I am excited about the Tokyo trip, still.")
        mid = message.id if hasattr(message, "id") else message
        ollama.responses.append(_answer(_reading(
            topic="travel enthusiasm", existing_id=prior.id,
            evidence=[{"message_id": mid, "quote": "excited about the Tokyo trip"}])))
        self.assertEqual(worker.run()["upserted"], 1)
        rows = store.list_active(user_id="u1")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].id, prior.id)
        self.assertEqual(rows[0].metadata["observations"], 2)
        self.assertEqual(rows[0].status, "active")

    def test_unoffered_merge_id_does_not_overwrite_a_reading(self):
        worker, store, _, _ = _build_world(responses=[_answer(_reading(existing_id=999))])
        self.assertEqual(worker.run()["upserted"], 0)
        self.assertEqual(store.list_active(user_id="u1"), [])

    def test_empty_and_malformed_output_have_different_meanings(self):
        for response in ('{"beliefs": []}', '{}', '[]'):
            worker, _, _, _ = _build_world(responses=[response])
            self.assertEqual(worker.run()["upserted"], 0)
        worker, _, _, _ = _build_world(responses=["not json"])
        self.assertEqual(worker.run()["reason"], "llm_unparseable")


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
        # Successful processing persists the intake boundary; a scheduler's
        # last attempt timestamp is deliberately not evidence of processing.
        worker.run()
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

    def test_budget_sheds_optional_hints_before_recent_evidence(self) -> None:
        worker, _, ollama, _ = _build_world(responses=["[]"])
        worker._context_window = lambda: 3000
        lines = [
            "oldest: " + "old " * 100,
            "middle: " + "mid " * 100,
            "newest: " + "new " * 100,
        ]
        worker._extract_with_llm(
            "\n".join(lines),
            interest_hint="interest " * 80,
            reconsider_block="reconsider " * 80,
            concept_hint="concept " * 80,
        )
        prompt = self._prompt(ollama)
        self.assertIn("newest:", prompt)
        self.assertIn("oldest:", prompt)
        self.assertNotIn("reconsider reconsider", prompt)

    def test_multiline_message_stays_attributed_to_one_speaker(self) -> None:
        worker, _, _, _ = _build_world(user_messages=["I like tea.\nI also like coffee."])
        transcript = worker._snapshot_transcript(
            session_key="u1:session-1", lookback_turns=12,
        )
        self.assertIn("Jacob: I like tea. I also like coffee.", transcript)
        self.assertEqual(sum("Jacob:" in line for line in transcript.splitlines()), 1)

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
        section = prompt.split("Also re-check whether")[-1]
        mentioned = sum(1 for i in range(6) if f"tokyo plan {i}" in section)
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


class InterpretationPromptTests(unittest.TestCase):
    def test_prompt_preserves_new_thinking_without_concept_prerequisite(self):
        from app.core.relationship.belief_worker import _build_system_prompt
        prompt = _build_system_prompt("Sample User")
        self.assertIn("Sample User", prompt)
        self.assertIn("concept is not a prerequisite", prompt)
        self.assertIn("Zero is the default", prompt)
        self.assertIn("Preserve material differences", prompt)
        self.assertIn("existing_id", prompt)

    def test_reading_and_storage_have_their_separate_time_contracts(self):
        from app.core.infra import timephrase
        worker, _, ollama, _ = _build_world(responses=[_answer()])
        worker.run()
        prompt = ollama.chat_calls[0]["messages"]
        self.assertIn(timephrase.STORED_TEXT_TIME_RULE, prompt[0]["content"])
        self.assertIn("Today is", prompt[0]["content"])
        self.assertIn("[message_id=1; NEW]", prompt[1]["content"])
        self.assertIn("[message_id=2; context only]", prompt[1]["content"])
