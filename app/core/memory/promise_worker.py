"""Promise extraction worker (reworked Phase 3c).

The original promise extractor had two tracks: a post-turn regex that
captured the bare verb fragment after "I'll" / "I need to" (so
"I'll never know" became the memory "Jacob promised: never know") and
a speaking-window LLM pass that only fired in voice mode. The regex
track wrote context-free fragments straight to ``tier="long_term"`` at
high confidence, which polluted the memory store with unusable rows.

This worker replaces both. It runs on the :class:`IdleWorkerScheduler`
during quiet windows (so it never blocks the brain), reads the last
few turns of conversation for *context*, and asks the worker LLM to
extract **self-contained** promises -- pronouns and vague objects
resolved against the transcript -- as a JSON array. Output is gated
(idiom stop-list + min content words), deduped against existing open
promises, and written as ``kind="promise"`` memories with the same
lifecycle contract consumed by :mod:`app.core.memory.promise_lifecycle`
/ :class:`PromiseFollowthroughWorker` / :class:`FollowUpWorker`.

Pipeline (one ``run`` call):

1. Snapshot the last ``promise_worker_lookback_turns`` turns (both
   user and assistant lines) via :meth:`ChatDatabase.get_messages`,
   capped by ``promise_worker_max_msg_chars`` /
   ``promise_worker_max_transcript_chars``.
2. Privacy-scrub via :func:`fact_check_privacy.scrub_claim_for_search`.
3. Spend one LLM call through the dedicated
   :class:`FactCheckRateLimiter` (``state_key='promise_worker.rate_state'``)
   asking for a JSON array of ``{who, what, deadline}`` objects.
4. Quality-gate + dedupe each promise, then persist via
   :meth:`MemoryStore.add`.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.memory.conflict_heuristics import _content_words, _tokenize
from app.core.memory.fact_check_privacy import scrub_claim_for_search
from app.core.memory.promise_extractor import Promise
from app.core.memory.promise_lifecycle import (
    ACTIVE_STATUSES,
    promise_status,
    promise_what,
)
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import MemoryStore
    from app.llm.chat_client import ChatClient
    from app.llm.embedder import Embedder


log = logging.getLogger("app.promise_worker")


# Cap on how much of any text we render in a single log line.
_LOG_PREVIEW_CHARS = 200

# Cap on the LLM response so a malformed answer can't run away with the
# budget. ~5 promises * ~60 chars each, plus JSON scaffolding.
_EXTRACT_MAX_TOKENS = 400

# A promise body must carry at least this many content words to be
# usable. "resolve them" has one ("resolve"); "fix the deploy script"
# has three ("fix", "deploy", "script"). This is the backstop behind
# the LLM's own self-contained instruction.
_MIN_CONTENT_WORDS = 2

# Pronouns that don't count as a real object. A promise whose only
# "content" words are a verb + a pronoun ("resolve them", "fix it")
# isn't self-contained, so it fails the gate even though it tokenizes
# to two words.
_PRONOUNS: frozenset[str] = frozenset(
    {
        "it",
        "them",
        "they",
        "that",
        "this",
        "those",
        "these",
        "you",
        "him",
        "her",
        "us",
        "we",
        "stuff",
        "thing",
        "things",
        "something",
        "someone",
    }
)

# Idiomatic heads / whole-phrases that read as commitments to the regex
# but are figures of speech. The LLM is told to skip these; this is the
# belt-and-suspenders gate for when it doesn't.
_IDIOM_FIRST_TOKENS: frozenset[str] = frozenset(
    {"never", "bet", "guess", "doubt", "wonder", "suppose", "dunno"}
)
_IDIOM_WHOLE_PHRASES: frozenset[str] = frozenset(
    {
        "never know",
        "see",
        "we will see",
        "see about that",
        "hope so",
        "think so",
        "guess so",
        "bet",
        "find out eventually",
    }
)


_SYSTEM_PROMPT = (
    "You read a short chat transcript between a user and the assistant "
    "(Aiko) and extract concrete promises or commitments either party "
    "made. Return ONE JSON array (no prose, no markdown) of zero or more "
    "promise objects. Each object has these fields:\n"
    "  - who: 'user' (the human committed) or 'assistant' (Aiko "
    "committed).\n"
    "  - what: a SELF-CONTAINED action phrase, 4-160 chars, that names "
    "its object so it stands on its own. Resolve pronouns and vague "
    "references using the transcript -- write 'bring Jacob some tea', "
    "not 'bring you some'; 'fix the deploy script', not 'fix it'.\n"
    "  - deadline: a specific time or day if one was stated, else null.\n"
    "Rules:\n"
    "- A promise is a concrete intent to DO, find out, follow up on, or "
    "remember something specific. Idioms and figures of speech are NOT "
    "promises ('I'll never know', \"we'll see\", 'I bet', 'I guess', "
    "'I hope so').\n"
    "- Skip vague feelings with no action, and skip anything you cannot "
    "make self-contained from the transcript.\n"
    "- Paraphrase to the action; do not echo the literal sentence.\n"
    "- 0-5 items max. Return an empty array [] when nothing qualifies.\n"
    "- Output the JSON array and nothing else."
)


_USER_TEMPLATE = (
    "Transcript (most recent turns):\n{transcript}\n\n"
    "Return one JSON array of promises."
)


_JSON_ARRAY_RE = re.compile(r"\[.*\]", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


def _is_low_quality(what: str) -> bool:
    """True when a promise body is an idiom or too thin to be usable."""
    norm = (what or "").strip().lower().strip(" \"'.,;:!?")
    if len(norm) < 4:
        return True
    if norm in _IDIOM_WHOLE_PHRASES:
        return True
    tokens = _tokenize(norm)
    if tokens and tokens[0] in _IDIOM_FIRST_TOKENS:
        return True
    # Content words minus pronouns: a verb + a bare pronoun object
    # ("resolve them") isn't a self-contained promise.
    meaningful = _content_words(tokens) - _PRONOUNS
    if len(meaningful) < _MIN_CONTENT_WORDS:
        return True
    return False


class PromiseExtractionWorker:
    """IdleWorker that mines recent turns for concrete promises."""

    name = "promise_worker"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        chat_db: "ChatDatabase",
        embedder: "Embedder",
        ollama: "ChatClient",
        chat_model: str,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        session_id_provider: Callable[[], str | None],
        user_display_name_provider: Callable[[], str] | None = None,
        user_names_provider: Callable[[], list[str]] | None = None,
        assistant_name_provider: Callable[[], str | None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._chat_db = chat_db
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._session_id_provider = session_id_provider
        self._user_display_name_provider = user_display_name_provider
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ──────────────────────────────────────────

    def update_runtime(self, *, model: str | None = None) -> None:
        """Hot-swap the worker LLM model (model-cascade hook)."""
        if model is not None:
            self._chat_model = model

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "promise_worker_interval_seconds",
                600,
            )
        )

    def _enabled(self) -> bool:
        return bool(
            getattr(self._agent_settings, "promise_worker_enabled", True)
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled():
            return False
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        ):
            return False
        snapshot = self._rate_limiter.snapshot(now)
        if snapshot["hour_used"] >= snapshot["hour_cap"]:
            return False
        if snapshot["day_used"] >= snapshot["day_cap"]:
            return False
        return True

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        session_id = (
            self._session_id_provider() if self._session_id_provider else None
        )
        if not session_id:
            return {"skipped": True, "reason": "no_session"}

        lookback_turns = int(
            getattr(self._memory_settings, "promise_worker_lookback_turns", 12)
        )
        if lookback_turns <= 0:
            return {"skipped": True, "reason": "lookback_zero"}

        now = self._clock()
        transcript = self._snapshot_transcript(
            session_id=session_id, lookback_turns=lookback_turns,
        )
        if not transcript:
            log.info(
                "promise-worker skip: no recent turns session=%s", session_id,
            )
            return {"skipped": True, "reason": "no_turns"}

        if not self._rate_limiter.allow(now):
            log.info(
                "promise-worker skip: rate-limited session=%s", session_id,
            )
            return {"skipped": True, "reason": "rate_limited"}

        # Privacy gate. Unlike the belief worker (which mines coarse
        # topics), the promise worker needs names + pronouns intact so
        # the LLM can resolve "bring you some" -> "bring Jacob some tea".
        # So we use the scrubber only as a *detector*: if it bails (the
        # transcript is hard-PII like a URL/email/address, or collapses
        # to nothing once personal tokens are removed) we skip the run;
        # otherwise the ORIGINAL transcript goes to the local worker LLM.
        user_names = (
            self._user_names_provider() if self._user_names_provider else None
        )
        assistant_name = (
            self._assistant_name_provider()
            if self._assistant_name_provider
            else None
        )
        safe_probe = scrub_claim_for_search(
            transcript,
            user_names=user_names,
            assistant_name=assistant_name,
        )
        if not safe_probe:
            log.info(
                "promise-worker skip: privacy-blocked transcript session=%s "
                "raw_chars=%d",
                session_id,
                len(transcript),
            )
            return {"skipped": True, "reason": "privacy_blocked"}

        log.info(
            "promise-worker start: session=%s lookback_turns=%d raw_chars=%d "
            "preview=%r",
            session_id,
            lookback_turns,
            len(transcript),
            _preview(transcript),
        )

        t0 = time.monotonic()
        promises = self._extract_with_llm(transcript)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        if promises is None:
            log.info(
                "promise-worker llm-unparseable elapsed_ms=%.0f session=%s",
                elapsed_ms,
                session_id,
            )
            return {
                "skipped": True,
                "reason": "llm_unparseable",
                "llm_ms": round(elapsed_ms, 1),
            }

        log.info(
            "promise-worker llm done: promises=%d elapsed_ms=%.0f",
            len(promises),
            elapsed_ms,
        )

        max_per_run = max(
            1,
            int(
                getattr(self._memory_settings, "promise_worker_max_per_run", 5)
            ),
        )
        existing = self._existing_promise_word_sets()
        persisted = 0
        dropped_dup = 0
        dropped_quality = 0
        for p in promises[:max_per_run]:
            if self._cancel_event.is_set():
                break
            if _is_low_quality(p.text):
                dropped_quality += 1
                continue
            body_words = _content_words(_tokenize(p.text))
            if self._is_duplicate(body_words, existing):
                dropped_dup += 1
                continue
            if self._persist(p, session_key=session_id):
                persisted += 1
                # Keep the in-run dedupe set fresh so two near-identical
                # promises in one batch don't both land.
                existing.append(body_words)

        result = {
            "promises_returned": len(promises),
            "persisted": persisted,
            "dropped_duplicate": dropped_dup,
            "dropped_low_quality": dropped_quality,
            "llm_ms": round(elapsed_ms, 1),
        }
        log.info("promise-worker done: %s", result)
        return result

    # ── transcript snapshot ──────────────────────────────────────────

    def _snapshot_transcript(
        self,
        *,
        session_id: str,
        lookback_turns: int,
    ) -> str:
        """Join the last N turns (both sides) into one prompt block.

        Unlike the belief worker (user-only), promises come from both
        Aiko and the user, so assistant lines are kept. We render from
        the most recent message backward and stop once the overall
        ``promise_worker_max_transcript_chars`` budget is hit so a long
        history can't blow the worker-LLM token budget.
        """
        max_msg_chars = max(
            200,
            int(
                getattr(
                    self._memory_settings,
                    "promise_worker_max_msg_chars",
                    2000,
                )
            ),
        )
        max_transcript_chars = max(
            500,
            int(
                getattr(
                    self._memory_settings,
                    "promise_worker_max_transcript_chars",
                    8000,
                )
            ),
        )
        user_name = (
            (self._user_display_name_provider() or "").strip()
            if self._user_display_name_provider
            else ""
        ) or "the user"
        try:
            rows = self._chat_db.get_messages(
                session_id, limit=lookback_turns * 2
            )
        except Exception:
            log.debug("promise-worker get_messages failed", exc_info=True)
            return ""
        rows = [r for r in rows if r.role in ("user", "assistant")]
        if not rows:
            return ""
        lines: list[str] = []
        total = 0
        for row in reversed(rows):
            text = (row.content or "").strip()
            if not text:
                continue
            if len(text) > max_msg_chars:
                text = text[: max_msg_chars - 1] + "\u2026"
            speaker = user_name if row.role == "user" else "Aiko"
            line = f"{speaker}: {text}"
            if total + len(line) > max_transcript_chars and lines:
                break
            lines.append(line)
            total += len(line) + 1
        lines.reverse()
        return "\n".join(lines)

    # ── LLM extractor ────────────────────────────────────────────────

    def _extract_with_llm(self, scrubbed_transcript: str) -> list[Promise] | None:
        user_content = _USER_TEMPLATE.format(transcript=scrubbed_transcript)
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "promise-worker extract prompt: model=%s prompt_chars=%d "
                "user_payload=%r",
                self._chat_model,
                len(user_content) + len(_SYSTEM_PROMPT),
                _preview(user_content),
            )
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _EXTRACT_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                # Distinguishing a real commitment ("I'll look into X")
                # from idle phrasing is a judgement call reasoning helps.
                # Headroom for the trace is added client-side.
                think=True,
                surface="promise_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("promise-worker extract call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return []
        log.debug(
            "promise-worker extract raw: chars=%d preview=%r",
            len(raw),
            _preview(raw),
        )
        return self._parse_promises(raw)

    @staticmethod
    def _parse_promises(raw: str) -> list[Promise] | None:
        """Parse the LLM JSON-array response into typed promises.

        Returns ``None`` only when the response is fundamentally
        un-parseable (no JSON array at all). An empty array returns
        ``[]`` -- a valid "nothing to report" turn.
        """
        match = _JSON_ARRAY_RE.search(raw or "")
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, list):
            return None
        out: list[Promise] = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            who_raw = str(item.get("who") or "").strip().lower()
            who = (
                "assistant"
                if who_raw in {"assistant", "aiko"}
                else ("user" if who_raw in {"user", "jacob"} else "user")
            )
            what = str(item.get("what") or "").strip()
            if len(what) < 4:
                continue
            deadline = item.get("deadline")
            deadline_str = ""
            if isinstance(deadline, str):
                deadline_str = deadline.strip()
            body = what
            if deadline_str and deadline_str.lower() not in {"null", "none", ""}:
                body = f"{what} (by {deadline_str})"
            out.append(
                Promise(
                    who=who,
                    text=body[:200],
                    source="llm",
                    confidence=0.8,
                )
            )
        return out

    # ── dedupe + persistence ─────────────────────────────────────────

    def _existing_promise_word_sets(self) -> list[set[str]]:
        """Content-word sets of existing still-active promise bodies."""
        out: list[set[str]] = []
        try:
            rows = self._memory_store.iter_by_kind("promise")
        except Exception:
            log.debug("promise-worker iter_by_kind failed", exc_info=True)
            return out
        for mem in rows:
            try:
                if promise_status(mem) not in ACTIVE_STATUSES:
                    continue
                words = _content_words(_tokenize(promise_what(mem)))
            except Exception:
                continue
            if words:
                out.append(words)
        return out

    @staticmethod
    def _is_duplicate(
        body_words: set[str], existing: list[set[str]], *, min_overlap: int = 3
    ) -> bool:
        if not body_words:
            return True
        for prior in existing:
            needed = min(int(min_overlap), len(body_words))
            if needed <= 0:
                continue
            if len(body_words & prior) >= needed:
                return True
        return False

    def _persist(self, promise: Promise, *, session_key: str | None) -> bool:
        store = self._memory_store
        embedder = self._embedder
        if store is None or embedder is None:
            return False
        display_name = (
            (self._user_display_name_provider() or "Jacob")
            if self._user_display_name_provider
            else "Jacob"
        )
        content = promise.to_memory_content(user_display_name=display_name)
        try:
            emb = embedder.embed(content)
        except Exception:
            log.debug("promise-worker embed failed", exc_info=True)
            return False
        try:
            mem = store.add(
                content=content,
                kind="promise",
                embedding=emb,
                salience=0.6,
                source_session=session_key,
                source_message_id=promise.source_turn_id,
                metadata={
                    "promise_who": promise.who,
                    "promise_status": "open",
                },
                tier="long_term",
                confidence=0.85,
            )
        except Exception:
            log.debug("promise-worker insert failed", exc_info=True)
            return False
        if mem is not None:
            log.info(
                "promise-worker upsert: id=%s who=%s content=%r",
                getattr(mem, "id", "?"),
                promise.who,
                _preview(content),
            )
        return mem is not None

    # ── debug surface ────────────────────────────────────────────────

    def debug_state(self) -> dict[str, Any]:
        """Snapshot for the MCP ``get_promise_stats`` tool."""
        now = self._clock()
        return {
            "enabled": self._enabled(),
            "interval_seconds": self.interval_seconds,
            "lookback_turns": int(
                getattr(
                    self._memory_settings, "promise_worker_lookback_turns", 12
                )
            ),
            "max_msg_chars": int(
                getattr(
                    self._memory_settings, "promise_worker_max_msg_chars", 2000
                )
            ),
            "max_transcript_chars": int(
                getattr(
                    self._memory_settings,
                    "promise_worker_max_transcript_chars",
                    8000,
                )
            ),
            "rate_limit": self._rate_limiter.snapshot(now),
        }


__all__ = ["PromiseExtractionWorker"]
