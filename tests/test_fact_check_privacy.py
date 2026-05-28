"""Privacy-gate tests for the F1 background fact-checker.

Two layers are exercised:

1. :func:`classify_memory_for_fact_check` — the enqueue-time gate that
   decides whether a memory may *ever* leak claims out of the box.

2. :func:`scrub_claim_for_search` — the search-time gate that produces
   the redacted query string we actually hand to DuckDuckGo, or
   refuses the claim outright when no safe variant exists.

Plus an end-to-end check that :class:`IdleFactChecker` honours the
search-time gate (a name-leaking claim never hits the stub web tool).
"""
from __future__ import annotations

import json
import tempfile
import threading
import unittest
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

import numpy as np

from app.core.chat_database import ChatDatabase
from app.core.fact_check_privacy import (
    classify_memory_for_fact_check,
    scrub_claim_for_search,
)
from app.core.fact_check_queue import FactCheckQueue
from app.core.fact_check_rate_limiter import FactCheckRateLimiter
from app.core.idle_fact_checker import IdleFactChecker
from app.core.knowledge_gap_extractor import KnowledgeGapStore
from app.core.memory_store import MemoryStore


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
    DIM = 16

    def embed(self, text: str) -> np.ndarray:
        vec = np.zeros(self.DIM, dtype=np.float32)
        for token in text.lower().split():
            vec[hash(token) % self.DIM] += 1.0
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
