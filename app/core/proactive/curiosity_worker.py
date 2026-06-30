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

from app.core.session.session_text_utils import resolve_user_name

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.curiosity_worker")


def _build_curiosity_prompt(
    user_display_name: str = "the user",
    *,
    quiet_interest: str | None = None,
) -> str:
    """Curiosity-worker prompt, templated on the user's display name.

    The required ``"Maybe ask <name>"`` prefix is asserted by
    :func:`_clean_curiosity_output`, which accepts whatever name is
    threaded in here -- so renaming the user mid-session takes effect
    on the next sweep without invalidating the produced text.

    K65c: when ``quiet_interest`` is supplied (a known-but-dormant K9
    cluster the user cares about but hasn't raised in a while), the prompt
    steers the follow-up to *circle back* to that interest instead of
    echoing the user's literal last words. With no quiet interest it falls
    back to the legacy "reference a word they just used" prompt.
    """
    name = user_display_name or "the user"
    if quiet_interest:
        return (
            f"You are Aiko in a quiet beat between turns. {name} has been "
            "making small talk. You realise there's a topic they genuinely "
            f"care about but haven't brought up in a while: \"{quiet_interest}\". "
            "You'd like to gently circle back to it next time you speak — not "
            "now, but next turn.\n"
            "\n"
            "Compose ONE short instruction to your future self (<= 22 words, "
            "third person, plain sentence). It must:\n"
            f"  - start with \"Maybe ask {name}\"\n"
            f"  - gently reconnect to {quiet_interest} (reshape the wording "
            "naturally; don't quote it robotically)\n"
            "  - end on a soft open question, not a yes/no or factual quiz\n"
            "\n"
            "Examples (do NOT copy verbatim):\n"
            f"  - \"Maybe ask {name} if they're still into {quiet_interest} "
            "lately — it's been a while since it came up.\"\n"
            "\n"
            "Output ONLY the sentence. No quotes, no JSON, no preamble."
        )
    return (
        f"You are Aiko in a quiet beat between turns. {name} just said "
        "something short and casual. You're noticing you'd like to ask "
        "them a small follow-up next time you speak — not now, but next "
        "turn.\n"
        "\n"
        "Compose ONE short instruction to your future self (<= 22 words, "
        "third person, plain sentence). It must:\n"
        f"  - start with \"Maybe ask {name}\"\n"
        "  - reference a concrete word or phrase they used\n"
        "  - end on a soft open question, not a yes/no or factual quiz\n"
        "\n"
        "Examples (do NOT copy verbatim):\n"
        f"  - \"Maybe ask {name} what they meant by 'weird week' — sounds layered.\"\n"
        f"  - \"Maybe ask {name} how the chess game ended; they sounded mid-thought.\"\n"
        "\n"
        "Output ONLY the sentence. No quotes, no JSON, no preamble."
    )


_CURIOSITY_PROMPT = _build_curiosity_prompt()


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
        user_display_name_provider: "Callable[[], str] | None" = None,
        interest_provider: "Callable[[], Any] | None" = None,
        cluster_anchor_enabled: bool = True,
        quiet_min_days: float = 7.0,
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
        self._user_display_name_provider = user_display_name_provider
        # K65c: returns the K9 cluster-activity rows (objects with .label /
        # .days_since, e.g. topic_graph.InterestActivity). ``None`` / missing
        # keeps the legacy literal-last-words anchoring.
        self._interest_provider = interest_provider
        self._cluster_anchor_enabled = bool(cluster_anchor_enabled)
        self._quiet_min_days = max(0.0, float(quiet_min_days))
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
            "anchored_on_interest": 0,
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

        # K65c: prefer a known-but-quiet interest as the anchor; fall back
        # to the legacy literal-last-words prompt when none is available.
        quiet_interest = self._pick_quiet_interest()
        if quiet_interest:
            self._stats["anchored_on_interest"] += 1

        prompt_user = self._compose_user_payload(
            user_text=user_text,
            assistant_text=assistant_text,
            arc_label=arc,
            quiet_interest=quiet_interest,
        )
        user_name = resolve_user_name(self._user_display_name_provider)
        try:
            t0 = time.monotonic()
            raw = self._ollama.chat(
                [
                    {
                        "role": "system",
                        "content": _build_curiosity_prompt(
                            user_name, quiet_interest=quiet_interest,
                        ),
                    },
                    {"role": "user", "content": prompt_user},
                ],
                options={
                    "temperature": 0.6,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                surface="curiosity_worker",
            )
            llm_ms = (time.monotonic() - t0) * 1000.0
        except Exception:
            log.debug("curiosity LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None

        cleaned = _clean_curiosity_output(raw, user_display_name=user_name)
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
        quiet_interest: str | None = None,
    ) -> str:
        user_name = resolve_user_name(self._user_display_name_provider)
        parts = [
            f"Conversation arc: {arc_label}",
            f"{user_name} just said: \"{(user_text or '').strip()[:400]}\"",
            f"You replied: \"{(assistant_text or '').strip()[:400]}\"",
        ]
        if quiet_interest:
            parts.append(
                f"A topic {user_name} cares about but hasn't raised in a "
                f"while: \"{quiet_interest}\""
            )
        return "\n".join(parts)

    def _pick_quiet_interest(self) -> str | None:
        """Pick the most-dormant known interest, or ``None``.

        Reads the K9 cluster-activity rows from ``interest_provider`` and
        returns the label of the *quietest* established cluster (largest
        ``days_since`` that still clears ``quiet_min_days``). A row with an
        unknown ``days_since`` is treated as very dormant. Disabled / no
        provider / empty graph → ``None`` (legacy anchoring).
        """
        if not self._cluster_anchor_enabled or self._interest_provider is None:
            return None
        try:
            rows = self._interest_provider()
        except Exception:
            log.debug("curiosity interest_provider raised", exc_info=True)
            return None
        best_label: str | None = None
        best_days = -1.0
        for item in rows or []:
            label = str(getattr(item, "label", "") or "").strip()
            if not label:
                continue
            days_raw = getattr(item, "days_since", None)
            days = float(days_raw) if days_raw is not None else 1.0e9
            if days < self._quiet_min_days:
                continue
            if days > best_days:
                best_days = days
                best_label = label
        return best_label


def _clean_curiosity_output(
    raw: str,
    *,
    user_display_name: str = "Jacob",
) -> str:
    """Tidy up the LLM's one-liner: strip quotes, collapse whitespace,
    enforce the required prefix. Returns ``""`` when the output doesn't
    look like a usable instruction.

    The required prefix is ``"Maybe ask <user_display_name>"`` (case
    insensitive). When the LLM emits a slightly different prefix
    (``"Ask <name>..."``) we salvage it by prepending ``"Maybe "``.
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
    name = (user_display_name or "the user").strip().lower() or "the user"
    expected_prefix = f"maybe ask {name}"
    ask_phrase = f"ask {name}"
    if not text.lower().startswith(expected_prefix):
        if ask_phrase in text.lower():
            idx = text.lower().find(ask_phrase)
            text = "Maybe " + text[idx:]
        else:
            return ""
    if len(text) > 220:
        text = text[:220].rsplit(" ", 1)[0].rstrip(",;:") + "..."
    return text


__all__ = ["CuriosityWorker"]
