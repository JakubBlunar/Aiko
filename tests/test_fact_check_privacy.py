"""Privacy-gate tests for the F1 background fact-checker.

Two layers are exercised:

1. :func:`classify_memory_for_fact_check` — the enqueue-time gate that
   decides whether a memory may *ever* leak claims out of the box.

2. :func:`scrub_claim_for_search` — the search-time gate that produces
   the redacted query string we actually hand to DuckDuckGo, or
   refuses the claim outright when no safe variant exists.

3. :func:`web_safe_probe` — the same rules used as a yes/no gate by
   callers that keep the original text, so a local-only check no longer
   leaves a ``REDACT`` audit line behind.

Plus an end-to-end check that :class:`IdleFactChecker` honours the
search-time gate (a name-leaking claim never hits the stub web tool).
"""
from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.core.infra.chat_database import ChatDatabase
from app.core.memory.fact_check_privacy import (
    _dropped_summary,
    classify_memory_for_fact_check,
    scrub_claim_for_search,
    web_safe_probe,
)
from app.core.memory.fact_check_queue import FactCheckQueue
from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
from app.core.memory.idle_fact_checker import IdleFactChecker
from app.core.memory.knowledge_gap_extractor import KnowledgeGapStore
from app.core.memory.memory_store import MemoryStore
from app.core.session.memory_facade_mixin import MemoryFacadeMixin


# ── classify_memory_for_fact_check ─────────────────────────────────────


class TestClassifyPersonalKinds(unittest.TestCase):
    def test_self_kind_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="self", content="Python 3.12 was released in 2023",
        )
        self.assertTrue(d.personal)
        self.assertIn("personal_kind", d.reason)

    def test_self_tagged_kind_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="self_tagged", content="something neutral 2023",
        )
        self.assertTrue(d.personal)

    def test_promise_kind_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="promise", content="will call back at 3pm",
        )
        self.assertTrue(d.personal)

    def test_shared_moment_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="shared_moment", content="the day we went hiking",
        )
        self.assertTrue(d.personal)

    def test_fact_kind_passes_when_content_is_neutral(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact", content="Python 3.12 was released in 2023",
        )
        self.assertFalse(d.personal)


class TestClassifyByContent(unittest.TestCase):
    def test_first_person_pronoun_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="I think the Eiffel Tower was finished in 1889",
        )
        self.assertTrue(d.personal)
        self.assertEqual(d.reason, "first_person_pronoun")

    def test_second_person_pronoun_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="You live in Berlin since 2019",
        )
        self.assertTrue(d.personal)

    def test_user_name_match_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="Jacob practices violin twice a week",
            user_names=["Jacob"],
        )
        self.assertTrue(d.personal)
        self.assertEqual(d.reason, "user_name")

    def test_user_name_case_insensitive(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="JACOB is reading a book",
            user_names=["Jacob"],
        )
        self.assertTrue(d.personal)

    def test_user_name_substring_does_not_trigger(self) -> None:
        # ``Jacobian`` contains ``Jacob`` as a prefix but isn't the
        # name. Word-boundary matching keeps this safe.
        d = classify_memory_for_fact_check(
            kind="fact",
            content="The Jacobian matrix was introduced in the 19th century",
            user_names=["Jacob"],
        )
        self.assertFalse(d.personal)

    def test_assistant_name_match_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="Aiko enjoyed the conversation",
            assistant_name="Aiko",
        )
        self.assertTrue(d.personal)
        self.assertEqual(d.reason, "assistant_name")

    def test_email_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact", content="reach me at me@example.com",
        )
        self.assertTrue(d.personal)
        self.assertEqual(d.reason, "email")

    def test_url_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="see https://example.com/foo for context",
        )
        self.assertTrue(d.personal)
        self.assertEqual(d.reason, "url")

    def test_phone_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="call +1 415 555 0123 for help",
        )
        self.assertTrue(d.personal)
        self.assertEqual(d.reason, "phone")

    def test_street_address_is_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="meet at 123 Main Street downtown",
        )
        self.assertTrue(d.personal)
        self.assertEqual(d.reason, "street_address")

    def test_coordinates_are_personal(self) -> None:
        d = classify_memory_for_fact_check(
            kind="fact",
            content="the cabin is at 47.6062, -122.3321",
        )
        self.assertTrue(d.personal)


# ── scrub_claim_for_search ──────────────────────────────────────────────


class TestScrubClaim(unittest.TestCase):
    def test_neutral_year_claim_passes_through(self) -> None:
        # Bare years are rejected by the alphabetic-survivor rule;
        # add a verifiable noun so the claim has fact-checkable
        # surface.
        cleaned = scrub_claim_for_search(
            "Python 3.12 was released in 2023",
        )
        self.assertIsNotNone(cleaned)
        assert cleaned is not None
        self.assertIn("Python", cleaned)

    def test_drops_user_name_token(self) -> None:
        cleaned = scrub_claim_for_search(
            "Jacob practices violin since 2010",
            user_names=["Jacob"],
        )
        self.assertIsNotNone(cleaned)
        assert cleaned is not None
        self.assertNotIn("Jacob", cleaned)
        self.assertIn("violin", cleaned)
        self.assertIn("2010", cleaned)

    def test_rejects_when_only_name_remains(self) -> None:
        cleaned = scrub_claim_for_search(
            "Jacob Smith",
            user_names=["Jacob Smith"],
        )
        self.assertIsNone(cleaned)

    def test_rejects_email(self) -> None:
        self.assertIsNone(
            scrub_claim_for_search("contact me@example.com asap"),
        )

    def test_rejects_phone(self) -> None:
        self.assertIsNone(
            scrub_claim_for_search("call +1 415 555 0123 now"),
        )

    def test_rejects_url(self) -> None:
        self.assertIsNone(
            scrub_claim_for_search(
                "see https://intranet.example.org/secret",
            ),
        )

    def test_drops_first_person_pronouns(self) -> None:
        cleaned = scrub_claim_for_search(
            "I think Python was released in 1991",
        )
        self.assertIsNotNone(cleaned)
        assert cleaned is not None
        self.assertNotIn(" I ", f" {cleaned} ")
        self.assertIn("Python", cleaned)

    def test_drops_private_time_tokens(self) -> None:
        cleaned = scrub_claim_for_search(
            "Yesterday the meteor passed Earth",
        )
        self.assertIsNotNone(cleaned)
        assert cleaned is not None
        self.assertNotIn("yesterday", cleaned.lower())
        self.assertIn("meteor", cleaned)

    def test_rejects_bare_year_after_redaction(self) -> None:
        # The proper_noun extractor often pulls "Jacob Smith" as a
        # claim. After scrubbing, nothing alphabetic survives so the
        # gate must refuse it.
        cleaned = scrub_claim_for_search(
            "Jacob Smith",
            user_names=["Jacob", "Smith"],
        )
        self.assertIsNone(cleaned)

    def test_rejects_empty_claim(self) -> None:
        self.assertIsNone(scrub_claim_for_search(""))
        self.assertIsNone(scrub_claim_for_search("   "))


class WebSafeProbeTests(unittest.TestCase):
    """The yes/no gate for callers that never publish the scrubbed text.

    The promise worker, the belief worker and the claim enqueue path all
    keep the *original* text and only need the refusal signal. Routing
    them through ``scrub_claim_for_search`` made each run emit a ``REDACT
    in=… out=… dropped=[…]`` audit line naming ~90 stripped tokens, as if
    a search had just gone out — 268 lines describing events that never
    happened.
    """

    def test_a_normal_transcript_passes(self) -> None:
        self.assertTrue(
            web_safe_probe(
                "Jacob: I can eat a lot and not have a stomachache",
                user_names=["Jacob"],
            ),
        )

    def test_hard_pii_is_refused(self) -> None:
        self.assertFalse(web_safe_probe("see https://intranet.example.org/x"))
        self.assertFalse(web_safe_probe("mail me at jacob@example.com"))
        self.assertFalse(web_safe_probe("call +1 415 555 0123 now"))

    def test_text_that_collapses_to_nothing_is_refused(self) -> None:
        self.assertFalse(
            web_safe_probe("Jacob me my I you", user_names=["Jacob"]),
        )

    def test_it_agrees_with_the_query_builder(self) -> None:
        # One implementation, two log policies: the gate must never accept
        # something the query builder would refuse, or the two could drift
        # into a real leak.
        samples = [
            ("Python 3.12 was released in 2023", None),
            ("Jacob practices violin since 2010", ["Jacob"]),
            ("contact me@example.com", None),
            ("Jacob Smith", ["Jacob", "Smith"]),
            ("", None),
            ("the cabin is at 47.6062, -122.3321", None),
        ]
        for text, names in samples:
            with self.subTest(text=text):
                built = scrub_claim_for_search(text, user_names=names)
                self.assertEqual(
                    web_safe_probe(text, user_names=names),
                    built is not None,
                )

    def test_a_passing_probe_logs_no_redact_line(self) -> None:
        # The whole point. Nothing is going to a search engine here, so
        # the audit trail must not claim a query was redacted.
        with self.assertLogs("app.fact_check_privacy", level="DEBUG") as cap:
            web_safe_probe(
                "[today] Jacob: I think you nailed my Tokyo plan",
                user_names=["Jacob"],
                assistant_name="Aiko",
            )
        messages = [r.getMessage() for r in cap.records]
        self.assertFalse(
            any("REDACT" in m for m in messages),
            f"probe must not log REDACT, got {messages!r}",
        )
        self.assertTrue(
            any("probe PASS" in m for m in messages),
            f"expected a DEBUG probe PASS line, got {messages!r}",
        )

    def test_a_passing_probe_is_silent_at_info(self) -> None:
        with self.assertNoLogs("app.fact_check_privacy", level="INFO"):
            web_safe_probe("Python 3.12 was released in 2023")

    def test_a_refusal_still_logs_at_info_with_a_reason(self) -> None:
        # Refusals are rare (3 in the whole corpus) and meaningful, so
        # they stay visible without opting in to DEBUG.
        with self.assertLogs("app.fact_check_privacy", level="INFO") as cap:
            web_safe_probe("see https://example.com/secret")
        self.assertTrue(
            any(
                "probe BLOCK" in r.getMessage() and "url" in r.getMessage()
                for r in cap.records
            ),
            f"expected a probe BLOCK url line, got {[r.getMessage() for r in cap.records]}",
        )


class DroppedSummaryTests(unittest.TestCase):
    """``dropped=`` must stay readable.

    The raw list carried one entry per occurrence, so scrubbing a whole
    transcript produced a 91-element wall of repeated pronouns that pushed
    the actual in/out previews out of view.
    """

    def test_it_dedupes_and_counts(self) -> None:
        summary = _dropped_summary(["i", "you", "i", "i", "you", "jacob"])
        self.assertIn("6 occurrences", summary)
        self.assertIn("i", summary)
        self.assertIn("jacob", summary)
        # Six occurrences, three distinct names in the rendering.
        self.assertEqual(summary.count(","), 2)

    def test_it_caps_the_distinct_list(self) -> None:
        summary = _dropped_summary([f"tok{n}" for n in range(40)])
        self.assertIn("40 occurrences", summary)
        self.assertIn("more", summary)
        self.assertLess(len(summary), 200)

    def test_empty(self) -> None:
        self.assertEqual(_dropped_summary([]), "none")

    def test_a_transcript_scrub_log_line_stays_short(self) -> None:
        # Regression on the actual reported line: a 3.8k-char transcript
        # with ~90 dropped tokens.
        transcript = " ".join(
            ["[today] Jacob: I think you and I should tell me my plan"] * 40
        )
        with self.assertLogs("app.fact_check_privacy", level="INFO") as cap:
            scrub_claim_for_search(
                transcript, user_names=["Jacob"], assistant_name="Aiko",
            )
        redacts = [r.getMessage() for r in cap.records if "REDACT" in r.getMessage()]
        self.assertTrue(redacts, "expected a REDACT line for the query path")
        self.assertLess(
            len(redacts[0]),
            700,
            f"REDACT line is still a wall of tokens: {redacts[0]!r}",
        )


# ── end-to-end gate behaviour ──────────────────────────────────────────


@dataclass
class _StubWebSearch:
    payload: dict[str, Any] = field(
        default_factory=lambda: {
            "results": [
                {
                    "title": "test",
                    "url": "https://example.org/x",
                    "snippet": "Python 3.12 released October 2023",
                },
            ],
        },
    )
    calls: list[dict[str, Any]] = field(default_factory=list)

    def run(self, args: dict[str, Any]) -> str:
        self.calls.append(dict(args))
        return json.dumps(self.payload)


@dataclass
class _StubOllamaClient:
    verdict_json: dict[str, Any] = field(
        default_factory=lambda: {
            "verdict": "support",
            "delta": 0.1,
            "rewrite": None,
        }
    )
    chat_calls: list[dict[str, Any]] = field(default_factory=list)

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
        self.chat_calls.append(
            {"messages": [dict(m) for m in messages], "model": model},
        )
        yield json.dumps(self.verdict_json)


@dataclass
class _StubAgentSettings:
    fact_checker_enabled: bool = True
    fact_checker_per_hour_cap: int = 10
    fact_checker_per_day_cap: int = 50


@dataclass
class _StubMemorySettings:
    fact_checker_interval_seconds: int = 300


class _DeterministicEmbedder:
    """Token-slot embedder. Uses md5 instead of ``hash()`` so the same
    token always maps to the same slot regardless of ``PYTHONHASHSEED``.
    """

    DIM = 16

    @staticmethod
    def _slot(token: str) -> int:
        digest = hashlib.md5(token.encode("utf-8")).digest()
        return int.from_bytes(digest[:4], "little") % _DeterministicEmbedder.DIM

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            vec[self._slot(token)] += 1.0
        n = float(np.linalg.norm(vec))
        if n > 0.0:
            vec /= n
        return vec


def _build_world(
    *,
    user_names: list[str] | None = None,
    assistant_name: str | None = None,
) -> dict[str, Any]:
    d = tempfile.mkdtemp()
    path = Path(d) / "mem.db"
    chat_db = ChatDatabase(path)
    memory_store = MemoryStore(path)
    embedder = _DeterministicEmbedder()
    queue = FactCheckQueue(chat_db)
    rate_limiter = FactCheckRateLimiter(chat_db, per_hour_cap=10, per_day_cap=50)
    web_search = _StubWebSearch()
    ollama = _StubOllamaClient()
    cancel_event = threading.Event()
    gap_store = KnowledgeGapStore(memory_store=memory_store, embedder=embedder)

    def _names() -> list[str]:
        return list(user_names or [])

    def _assistant() -> str | None:
        return assistant_name

    worker = IdleFactChecker(
        queue=queue,
        memory_store=memory_store,
        agent_settings=_StubAgentSettings(),
        memory_settings=_StubMemorySettings(),
        ollama=ollama,
        chat_model="stub-model",
        web_search_tool=web_search,
        rate_limiter=rate_limiter,
        cancel_event=cancel_event,
        knowledge_gap_store=gap_store,
        embedder=embedder,
        user_names_provider=_names,
        assistant_name_provider=_assistant,
    )
    return {
        "path": path,
        "chat_db": chat_db,
        "memory_store": memory_store,
        "embedder": embedder,
        "queue": queue,
        "web_search": web_search,
        "ollama": ollama,
        "worker": worker,
    }


class TestIdleFactCheckerHonoursPrivacyGate(unittest.TestCase):
    def test_claim_with_only_user_name_is_blocked(self) -> None:
        world = _build_world(user_names=["Jacob"])
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        # Note: this would normally be skipped at enqueue time too,
        # but we bypass that gate to assert the search-time gate
        # *also* protects when something slips through.
        emb = embedder.embed("Jacob Smith")
        mem = memory_store.add(
            "Jacob Smith",  # raw content
            "fact",
            emb,
            salience=0.5,
        )
        assert mem is not None
        world["queue"].enqueue(
            memory_id=int(mem.id),
            claim_text="Jacob Smith",
            claim_kind="proper_noun",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("reason"), "privacy_gate")
        # Confirm the stub web search was never called.
        self.assertEqual(len(world["web_search"].calls), 0)

    def test_the_sentence_is_scrubbed_before_it_reaches_the_model(self) -> None:
        """The verified sentence goes through the same gate as the query.

        The sentence carries more context than the span by construction,
        so it gets scrubbed too. The model is local and the threat model
        already trusts it with this content -- this keeps the boundary
        uniform so there is exactly one place that sees raw claim text.
        """
        world = _build_world(user_names=["Jacob"])
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        emb = embedder.embed("Trine 2 was developed by Frozen Byte")
        mem = memory_store.add(
            "Trine 2 was developed by Frozen Byte", "fact", emb, salience=0.5,
        )
        assert mem is not None
        world["queue"].enqueue(
            memory_id=int(mem.id),
            claim_text="Frozen Byte",
            claim_kind="proper_noun",
            claim_sentence="Jacob says Trine 2 was developed by Frozen Byte.",
        )
        world["worker"].run()
        prompt = world["ollama"].chat_calls[0]["messages"][-1]["content"]
        self.assertNotIn("Jacob", prompt)
        self.assertIn("Frozen Byte", prompt)

    def test_the_sentence_never_reaches_the_search_engine(self) -> None:
        """Outbound surface is unchanged by the sentence work.

        Only the span is searched. The sentence is strictly richer, so
        sending it outbound would have widened the leak surface that
        this whole module exists to keep narrow.
        """
        world = _build_world(user_names=["Jacob"])
        embedder = world["embedder"]
        emb = embedder.embed("Trine 2 was developed by Frozen Byte")
        mem = world["memory_store"].add(
            "Trine 2 was developed by Frozen Byte", "fact", emb, salience=0.5,
        )
        assert mem is not None
        world["queue"].enqueue(
            memory_id=int(mem.id),
            claim_text="Frozen Byte",
            claim_kind="proper_noun",
            claim_sentence="Trine 2 was developed by Frozen Byte in 2011.",
        )
        world["worker"].run()
        query = world["web_search"].calls[0]["query"]
        self.assertEqual(query, "Frozen Byte")

    def test_neutral_claim_is_sent_with_redaction(self) -> None:
        world = _build_world(user_names=["Jacob"])
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        emb = embedder.embed("Python 3.12 was released in 2023")
        mem = memory_store.add(
            "Python 3.12 was released in 2023",
            "fact",
            emb,
            salience=0.5,
        )
        assert mem is not None
        world["queue"].enqueue(
            memory_id=int(mem.id),
            claim_text="Python 3.12 was released in 2023",
            claim_kind="proper_noun",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("verdict"), "support")
        self.assertEqual(len(world["web_search"].calls), 1)
        query = world["web_search"].calls[0]["query"]
        # The scrubber is a no-op on this claim (no PII to strip),
        # so the query matches the claim text. We assert the user's
        # name absolutely doesn't appear.
        self.assertNotIn("Jacob", query)

    def test_claim_with_name_in_middle_is_redacted(self) -> None:
        world = _build_world(user_names=["Jacob"])
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        emb = embedder.embed("Jacob practices violin since 2010")
        mem = memory_store.add(
            "Jacob practices violin since 2010",
            "fact",
            emb,
            salience=0.5,
        )
        assert mem is not None
        world["queue"].enqueue(
            memory_id=int(mem.id),
            claim_text="Jacob practices violin since 2010",
            claim_kind="proper_noun",
        )
        result = world["worker"].run()
        self.assertEqual(result.get("verdict"), "support")
        self.assertEqual(len(world["web_search"].calls), 1)
        query = world["web_search"].calls[0]["query"]
        self.assertNotIn("Jacob", query)
        # The rest of the claim should still be searchable.
        self.assertIn("violin", query)

    def test_distil_call_also_sees_scrubbed_claim(self) -> None:
        world = _build_world(user_names=["Jacob"])
        memory_store: MemoryStore = world["memory_store"]
        embedder = world["embedder"]
        emb = embedder.embed("Jacob practices violin since 2010")
        mem = memory_store.add(
            "Jacob practices violin since 2010",
            "fact",
            emb,
            salience=0.5,
        )
        assert mem is not None
        world["queue"].enqueue(
            memory_id=int(mem.id),
            claim_text="Jacob practices violin since 2010",
            claim_kind="proper_noun",
        )
        world["worker"].run()
        chat_calls = world["ollama"].chat_calls
        self.assertEqual(len(chat_calls), 1)
        user_msg = next(
            m for m in chat_calls[0]["messages"] if m["role"] == "user"
        )
        self.assertNotIn("Jacob", user_msg["content"])


# ── enqueue payload shape ──────────────────────────────────────────────


class _EnqueueHarness(MemoryFacadeMixin):
    """Just enough of ``SessionController`` to drive ``_maybe_enqueue_claims``."""

    def __init__(self, queue: FactCheckQueue) -> None:
        self._fact_check_queue = queue

    def _fact_check_user_names(self) -> list[str]:
        return ["Jacob"]

    def _fact_check_assistant_name(self) -> str | None:
        return "Aiko"


@dataclass
class _MemoryRow:
    """The object shape the turn path and REST facade pass to the hook."""

    id: int
    kind: str
    content: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "content": self.content,
            "metadata": dict(self.metadata),
        }


class TestEnqueueAcceptsBothPayloadShapes(unittest.TestCase):
    """``_notify_memory_added`` is called with a ``Memory`` *and* with its
    dict form -- the knowledge, topic-digest, pre-thought and K1-goal
    workers all pass ``mem.to_dict()``. Reading the payload with
    ``getattr`` alone silently dropped every dict caller, which is why the
    fact-checker never queued a single knowledge claim."""

    def setUp(self) -> None:
        d = tempfile.mkdtemp()
        self.chat_db = ChatDatabase(Path(d) / "mem.db")
        self.queue = FactCheckQueue(self.chat_db)
        self.harness = _EnqueueHarness(self.queue)

    def _row(self) -> _MemoryRow:
        return _MemoryRow(
            id=42,
            kind="knowledge",
            content="Trine 2 was developed by Frozen Byte and published in 2011.",
        )

    def test_object_payload_enqueues(self) -> None:
        self.harness._maybe_enqueue_claims(self._row())
        self.assertGreater(len(self.queue.peek_all()), 0)

    def test_dict_payload_enqueues_the_same_claims(self) -> None:
        self.harness._maybe_enqueue_claims(self._row())
        from_object = [c.claim_text for c in self.queue.peek_all()]

        queue2 = FactCheckQueue(ChatDatabase(Path(tempfile.mkdtemp()) / "m.db"))
        _EnqueueHarness(queue2)._maybe_enqueue_claims(self._row().to_dict())
        from_dict = [c.claim_text for c in queue2.peek_all()]

        self.assertGreater(len(from_dict), 0)
        self.assertEqual(from_object, from_dict)

    def test_dict_payload_still_honours_the_privacy_gate(self) -> None:
        personal = _MemoryRow(
            id=7, kind="self", content="I felt calm in 2024",
        ).to_dict()
        self.harness._maybe_enqueue_claims(personal)
        self.assertEqual(len(self.queue.peek_all()), 0)

    def test_dict_knowledge_gap_reads_the_question_from_metadata(self) -> None:
        gap = _MemoryRow(
            id=9,
            kind="knowledge_gap",
            content="fallback text",
            metadata={"question": "when was the Voyager 1 probe launched"},
        ).to_dict()
        self.harness._maybe_enqueue_claims(gap)
        items = self.queue.peek_all()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].claim_kind, "knowledge_gap")
        self.assertIn("Voyager", items[0].claim_text)

    def test_a_payload_without_an_id_is_ignored(self) -> None:
        self.harness._maybe_enqueue_claims({"kind": "knowledge", "content": "x 2011"})
        self.assertEqual(len(self.queue.peek_all()), 0)


# ── audit logging ──────────────────────────────────────────────────────


class TestPrivacyAuditLogging(unittest.TestCase):
    """The privacy gate must emit one audit-friendly log line per
    decision so ``data/app.log`` carries the trail needed to tighten
    the rules later."""

    def test_classify_block_logs_at_info_with_reason_and_preview(self) -> None:
        with self.assertLogs("app.fact_check_privacy", level="INFO") as cm:
            classify_memory_for_fact_check(
                kind="self",
                content="I really like coffee",
            )
        self.assertTrue(
            any(
                "BLOCK" in r.getMessage()
                and "personal_kind:self" in r.getMessage()
                for r in cm.records
            ),
            msg=f"expected BLOCK log line, got: {[r.getMessage() for r in cm.records]}",
        )

    def test_classify_allow_logs_at_debug_only(self) -> None:
        # DEBUG level enabled → ALLOW line must appear; INFO level
        # alone must not (high-volume path).
        with self.assertLogs("app.fact_check_privacy", level="DEBUG") as cm:
            classify_memory_for_fact_check(
                kind="fact",
                content="Python 3.12 was released in 2023",
            )
        msgs = [r.getMessage() for r in cm.records]
        allow_lines = [m for m in msgs if "ALLOW" in m]
        self.assertEqual(
            len(allow_lines),
            1,
            msg=f"expected exactly one ALLOW line, got: {msgs}",
        )
        self.assertEqual(allow_lines[0].split()[0], "privacy")

    def test_scrub_block_email_logs_at_info(self) -> None:
        with self.assertLogs("app.fact_check_privacy", level="INFO") as cm:
            scrub_claim_for_search("contact me at jacob@example.com")
        self.assertTrue(
            any(
                "BLOCK" in r.getMessage() and "email" in r.getMessage()
                for r in cm.records
            ),
            msg=f"expected scrub BLOCK email line, got: {[r.getMessage() for r in cm.records]}",
        )

    def test_scrub_redact_logs_dropped_tokens(self) -> None:
        with self.assertLogs("app.fact_check_privacy", level="INFO") as cm:
            cleaned = scrub_claim_for_search(
                "Jacob practices violin since 2010",
                user_names=["Jacob"],
            )
        self.assertIsNotNone(cleaned)
        # The audit line must include both the dropped tokens and the
        # before/after preview so a rule-tightening pass can identify
        # patterns in the wild.
        redact_lines = [
            r.getMessage() for r in cm.records if "REDACT" in r.getMessage()
        ]
        self.assertEqual(len(redact_lines), 1)
        line = redact_lines[0]
        self.assertIn("jacob", line.lower())
        self.assertIn("violin", line)

    def test_scrub_block_too_short_includes_dropped_tokens(self) -> None:
        # The whole claim is name + first-person → after redaction the
        # remainder is too short. The block log should record both the
        # reason AND the tokens we dropped, so the audit can spot
        # patterns where the gate is firing too aggressively.
        with self.assertLogs("app.fact_check_privacy", level="INFO") as cm:
            scrub_claim_for_search(
                "Jacob me my I",
                user_names=["Jacob"],
            )
        block_lines = [
            r.getMessage()
            for r in cm.records
            if "BLOCK" in r.getMessage()
            and "too_short_after_redaction" in r.getMessage()
        ]
        self.assertEqual(len(block_lines), 1)
        line = block_lines[0]
        self.assertIn("dropped=", line)


if __name__ == "__main__":
    unittest.main()
