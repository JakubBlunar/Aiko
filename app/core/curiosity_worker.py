"""Curiosity follow-up worker (Phase 4c — "Aiko human-like upgrades").

When the user has been answering casually and not asking many questions
back, Aiko's prompt becomes a little echo-chamber. This worker looks at
the most recent user turn + arc state and, when conditions match,
emits a single ``open_question`` memory along the lines of:

    "Maybe ask Jacob a small follow-up about <topic> next turn."

The next turn's prompt assembler picks the question up via the existing
``open_question`` retrieval path, and Aiko spontaneously asks the
follow-up — without the user having to prompt it.

Design constraints (calibrated against the plan):
  * Tiny LLM call (<= 80 tokens). Skipped if no Ollama is available.
  * Throttled to one suggestion per ``min_turns_between`` turns
    (default 3). Time-throttle on top of that for guards in tests.
  * Only fires when arc is shallow (``casual_check_in`` is the canonical
    label) AND the user turn was short (<= 8 words) AND the user
    didn't already ask a question.
  * Output is a one-liner. Empty / refused / malformed responses
    silently skip.
  * All state is in-memory; no DB writes beyond the open_question
    memory itself.
"""
from __future__ import annotations

import logging
import re
import time
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from app.core.memory_store import Memory, MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.curiosity_worker")


_CURIOSITY_PROMPT = """\
You are Aiko in a quiet beat between turns. Jacob just said something
short and casual. You're noticing you'd like to ask him a small
follow-up next time you speak — not now, but next turn.

Compose ONE short instruction to your future self (<= 22 words, third
person, plain sentence). It must:
  - start with "Maybe ask Jacob"
  - reference a concrete word or phrase he used
  - end on a soft open question, not a yes/no or factual quiz

Examples (do NOT copy verbatim):
  - "Maybe ask Jacob what he meant by 'weird week' — sounds layered."
  - "Maybe ask Jacob how the chess game ended; he sounded mid-thought."

Output ONLY the sentence. No quotes, no JSON, no preamble."""


_QUESTION_RE = re.compile(r"\?")
_WORD_RE = re.compile(r"[\w']+")
_SHALLOW_ARC_LABELS: frozenset[str] = frozenset({
    "casual_check_in",
    "small_talk",
    "idle",
})


def _word_count(text: str) -> int:
    return len(_WORD_RE.findall(text or ""))


def _looks_like_question(text: str) -> bool:
    if not text:
        return False
    if "?" in text:
        return True
    lowered = text.strip().lower()
    starts = (
        "what", "why", "how", "when", "where", "who", "which",
        "are you", "did you", "do you", "can you", "could you",
        "would you", "will you", "is it", "tell me",
    )
    return any(lowered.startswith(s) for s in starts)


class CuriosityWorker:
    """Speaking-window job that emits proactive ``open_question`` memories
    when the recent conversation has gone shallow.
    """

    def __init__(
        self,
        *,
        ollama: "OllamaClient | None",
        memory_store: "MemoryStore | None",
        embedder: "Embedder | None",
        model: str,
        min_turns_between: int = 3,
        min_seconds_between: float = 60.0,
        max_user_word_count: int = 8,
        max_tokens: int = 80,
        salience: float = 0.55,
    ) -> None:
        self._ollama = ollama
        self._memory_store = memory_store
        self._embedder = embedder
        self._model = model
        self._min_turns_between = max(1, int(min_turns_between))
        self._min_seconds_between = max(0.0, float(min_seconds_between))
        self._max_words = max(1, int(max_user_word_count))
        self._max_tokens = max(40, int(max_tokens))
        self._salience = max(0.0, min(1.0, float(salience)))
        self._last_run_turn = -10**9
        self._last_run_at = 0.0
        self._turn_counter = 0
        self._stats = {
            "scheduled": 0,
            "skipped_disabled": 0,
            "skipped_throttled": 0,
            "skipped_not_shallow": 0,
            "skipped_user_too_long": 0,
            "skipped_user_already_asked": 0,
            "skipped_no_topic": 0,
            "completed": 0,
            "failed": 0,
            "memories_written": 0,
        }

    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    def update_runtime(
        self,
        *,
        model: str | None = None,
        min_turns_between: int | None = None,
        min_seconds_between: float | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if min_turns_between is not None:
            self._min_turns_between = max(1, int(min_turns_between))
        if min_seconds_between is not None:
            self._min_seconds_between = max(0.0, float(min_seconds_between))

    def maybe_run(
        self,
        *,
        session_key: str,
        user_text: str,
        assistant_text: str,
        arc_label: str,
        on_memory_added: Callable[["Memory"], None] | None = None,
    ) -> "Memory | None":
        """Run the curiosity pass if all the gating predicates pass.

        Returns the persisted ``Memory`` (kind ``open_question``) on
        success, or ``None`` when throttled / disabled / not shallow /
        unable to draft a question.
        """
        # Bookkeeping first so callers can rely on the per-turn counter
        # being monotonic regardless of the gate decisions below.
        self._turn_counter += 1

        if (
            self._ollama is None
            or self._memory_store is None
            or self._embedder is None
        ):
            self._stats["skipped_disabled"] += 1
            return None
        # Hard throttle: turns since last run.
        if (self._turn_counter - self._last_run_turn) < self._min_turns_between:
            self._stats["skipped_throttled"] += 1
            return None
        # Soft throttle: wall-clock seconds since last run.
        now = time.monotonic()
        if now - self._last_run_at < self._min_seconds_between:
            self._stats["skipped_throttled"] += 1
            return None

        arc = (arc_label or "").strip().lower()
        if arc not in _SHALLOW_ARC_LABELS:
            self._stats["skipped_not_shallow"] += 1
            return None

        user_words = _word_count(user_text)
        if user_words == 0 or user_words > self._max_words:
            if user_words > self._max_words:
                self._stats["skipped_user_too_long"] += 1
            else:
                self._stats["skipped_disabled"] += 1
            return None

        if _looks_like_question(user_text):
            self._stats["skipped_user_already_asked"] += 1
            return None

        # OK to fire.
        self._last_run_turn = self._turn_counter
        self._last_run_at = now
        self._stats["scheduled"] += 1

        prompt_user = self._compose_user_payload(
            user_text=user_text,
            assistant_text=assistant_text,
            arc_label=arc,
        )
        try:
            t0 = time.monotonic()
            raw = self._ollama.chat(
                [
                    {"role": "system", "content": _CURIOSITY_PROMPT},
                    {"role": "user", "content": prompt_user},
                ],
                options={
                    "temperature": 0.6,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
            )
            llm_ms = (time.monotonic() - t0) * 1000.0
        except Exception:
            log.debug("curiosity LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None

        cleaned = _clean_curiosity_output(raw)
        if not cleaned:
            self._stats["skipped_no_topic"] += 1
            return None
        try:
            embedding = self._embedder.embed(cleaned)
        except Exception:
            log.debug("curiosity embed failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        try:
            memory = self._memory_store.add(
                content=cleaned,
                kind="open_question",
                embedding=embedding,
                salience=self._salience,
                source_session=session_key,
                source_message_id=None,
            )
        except Exception:
            log.debug("curiosity memory insert failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        if memory is None:
            self._stats["failed"] += 1
            return None
        self._stats["completed"] += 1
        self._stats["memories_written"] += 1
        log.info(
            "curiosity worker wrote memory id=%d (chars=%d, llm_ms=%.0f)",
            int(memory.id), len(cleaned), llm_ms,
        )
        if on_memory_added is not None:
            try:
                on_memory_added(memory)
            except Exception:
                log.debug("curiosity on_memory_added raised", exc_info=True)
        return memory

    # ── helpers ────────────────────────────────────────────────────────

    def _compose_user_payload(
        self,
        *,
        user_text: str,
        assistant_text: str,
        arc_label: str,
    ) -> str:
        return (
            f"Conversation arc: {arc_label}\n"
            f"Jacob just said: \"{(user_text or '').strip()[:400]}\"\n"
            f"You replied: \"{(assistant_text or '').strip()[:400]}\""
        )


def _clean_curiosity_output(raw: str) -> str:
    """Tidy up the LLM's one-liner: strip quotes, collapse whitespace,
    enforce the required prefix. Returns ``""`` when the output doesn't
    look like a usable instruction.
    """
    text = (raw or "").strip()
    if not text:
        return ""
    if text.startswith("```"):
        text = text.strip("`").strip()
        if "\n" in text:
            head, _, body = text.partition("\n")
            if len(head) <= 12 and head.strip().isalpha():
                text = body.strip()
    text = text.strip("\"'` \t\n")
    if "\n" in text:
        text = text.split("\n", 1)[0].strip()
    if not text:
        return ""
    if not text.lower().startswith("maybe ask jacob"):
        # Try to salvage a slightly off-prefix output.
        if "ask jacob" in text.lower():
            idx = text.lower().find("ask jacob")
            text = "Maybe " + text[idx:]
        else:
            return ""
    if len(text) > 220:
        text = text[:220].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return text


__all__ = ["CuriosityWorker"]
