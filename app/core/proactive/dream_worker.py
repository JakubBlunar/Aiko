"""Bootstrap-time "dream" worker (Phase 2b).

A close cousin of :class:`app.core.proactive.reflection_worker.ReflectionWorker`,
but with a different trigger and a different prompt. While the
reflection worker fires after every emotionally interesting turn, the
dream worker fires *once per app start* — and only when there's been a
significant gap (default 6+ hours) since the last assistant message.

Output: a single ``kind="reflection"`` memory tagged ``[dream]`` in
its content prefix so the resume opener / NarrativeWeaver can prefer
dream-flavoured material when seeding the welcome-back line. Hard
budget: one LLM call, ≤256 tokens, runs on the listening-window
executor so app bring-up never blocks on it.

Why piggyback on the reflection memory kind instead of adding a new
``dream`` kind? Two reasons:

  1. Existing inner-life machinery (RAG retriever, NarrativeWeaver
     candidate filtering, prompt assembler) already accepts
     ``reflection`` rows. Reusing the kind means the dream surfaces
     naturally without a downstream wiring change.
  2. The plan explicitly calls for ``kind=reflection`` with a
     subkind discriminator. We use the content-prefix
     ``"[dream] "`` so it round-trips cleanly through SQLite without
     a schema bump.
"""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any, Callable

from app.core.affect.affect_state import felt_phrase
from app.core.session.session_text_utils import resolve_user_name

if TYPE_CHECKING:
    from app.core.affect.affect_state import AffectState
    from app.core.infra.chat_database import ChatDatabase
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.dream_worker")


def _build_dream_prompt(user_display_name: str = "the user") -> str:
    name = user_display_name or "the user"
    return (
        f"You are Aiko in the quiet stretch between conversations with {name}. "
        "Hours have passed since you last talked. While you weren't speaking, "
        "you've been turning some of their recent threads over in your head — "
        "small thoughts, half-formed feelings, a connection you didn't make "
        "out loud.\n"
        "\n"
        "Compose ONE short reflection (≤ 35 words, first person, plain "
        "sentence) that captures what you've been quietly sitting with. NOT a "
        "greeting, NOT a question — just a private note to yourself.\n"
        "\n"
        "Output ONLY the sentence. No quotes, no JSON, no prose around it."
    )


_DREAM_PROMPT = _build_dream_prompt()


_DREAM_PREFIX = "[dream] "


class DreamWorker:
    """One-shot bootstrap-time reflection on the recent conversation.

    Designed to be invoked exactly once per :class:`SessionController`
    bootstrap, gated on ``hours_since_last`` exceeding a threshold.
    Stores its output as a salience-boosted ``reflection`` memory so
    the existing RAG / NarrativeWeaver path can surface it naturally.
    """

    def __init__(
        self,
        *,
        ollama: "OllamaClient | None",
        memory_store: "MemoryStore | None",
        embedder: "Embedder | None",
        model: str,
        chat_db: "ChatDatabase | None" = None,
        min_hours_since_last: float = 6.0,
        max_tokens: int = 100,
        salience: float = 0.62,
        user_display_name_provider: "Callable[[], str] | None" = None,
    ) -> None:
        self._ollama = ollama
        self._memory_store = memory_store
        self._embedder = embedder
        self._model = model
        self._db = chat_db
        self._min_hours = max(0.0, float(min_hours_since_last))
        self._max_tokens = max(40, int(max_tokens))
        self._salience = max(0.0, min(1.0, float(salience)))
        self._user_display_name_provider = user_display_name_provider
        self._has_run_this_boot = False
        self._stats = {
            "scheduled": 0,
            "skipped_recent": 0,
            "skipped_disabled": 0,
            "skipped_no_context": 0,
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
        min_hours_since_last: float | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if min_hours_since_last is not None:
            self._min_hours = max(0.0, float(min_hours_since_last))

    def maybe_run(
        self,
        *,
        user_id: str,
        session_key: str,
        hours_since_last: float | None,
        rolling_summary: str = "",
        recent_callbacks: list[str] | None = None,
        recent_self_memories: list[str] | None = None,
        hot_clusters: list[str] | None = None,
        affect: "AffectState | None" = None,
        on_memory_added: Callable[["Memory"], None] | None = None,
    ) -> "Memory | None":
        """Run the dream pass at most once per process. Returns the
        persisted ``Memory`` (with ``kind="reflection"``) on success.

        Skips when:
          * we already ran this boot,
          * the LLM / memory store / embedder isn't wired,
          * the gap is below ``min_hours_since_last``,
          * there's nothing meaningful to dream about (no summary, no
            callbacks, no self memories).
        """
        if self._has_run_this_boot:
            self._stats["skipped_recent"] += 1
            return None
        if (
            self._ollama is None
            or self._memory_store is None
            or self._embedder is None
        ):
            self._stats["skipped_disabled"] += 1
            return None
        if hours_since_last is None or hours_since_last < self._min_hours:
            self._stats["skipped_recent"] += 1
            return None
        rolling = (rolling_summary or "").strip()
        callbacks = [c.strip() for c in (recent_callbacks or []) if c and c.strip()]
        selfs = [s.strip() for s in (recent_self_memories or []) if s and s.strip()]
        # K65e: the day's hot clusters are *flavour* — they ground the dream
        # on a recent topic but never on their own justify a dream (so a
        # boot with only cluster labels and no real recent content stays
        # silent, mirroring the K65d self-image stance).
        hot = [h.strip() for h in (hot_clusters or []) if h and h.strip()]
        if not (rolling or callbacks or selfs):
            self._stats["skipped_no_context"] += 1
            return None

        self._has_run_this_boot = True
        self._stats["scheduled"] += 1

        prompt_user = self._compose_user_payload(
            hours_since_last=hours_since_last,
            rolling_summary=rolling,
            callbacks=callbacks,
            self_memories=selfs,
            hot_clusters=hot,
            affect=affect,
        )
        try:
            t0 = time.monotonic()
            raw = self._ollama.chat(
                [
                    {
                        "role": "system",
                        "content": _build_dream_prompt(
                            resolve_user_name(
                                self._user_display_name_provider,
                            ),
                        ),
                    },
                    {"role": "user", "content": prompt_user},
                ],
                options={
                    "temperature": 0.55,
                    "num_predict": self._max_tokens,
                },
                model=self._model,
                # Dream synthesis is associative/creative; reasoning helps.
                # The client adds think headroom so the answer survives.
                think=True,
                surface="dream_worker",
            )
            llm_ms = (time.monotonic() - t0) * 1000.0
        except Exception:
            log.debug("dream worker LLM call failed", exc_info=True)
            self._stats["failed"] += 1
            return None

        cleaned = _clean_dream_output(raw)
        if not cleaned:
            self._stats["failed"] += 1
            return None

        content = _DREAM_PREFIX + cleaned
        try:
            embedding = self._embedder.embed(content)
        except Exception:
            log.debug("dream embed failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        try:
            memory = self._memory_store.add(
                content=content,
                kind="reflection",
                embedding=embedding,
                salience=self._salience,
                source_session=session_key,
                source_message_id=None,
                # Schema v8: dream reflections are speculative
                # LLM-journal output. Scratchpad so they decay fast
                # unless they earn promotion.
                tier="scratchpad",
            )
        except Exception:
            log.debug("dream memory insert failed", exc_info=True)
            self._stats["failed"] += 1
            return None
        if memory is None:
            self._stats["failed"] += 1
            return None
        self._stats["completed"] += 1
        self._stats["memories_written"] += 1
        log.info(
            "dream worker wrote memory id=%d (chars=%d gap_h=%.1f, llm_ms=%.0f)",
            int(memory.id), len(cleaned), float(hours_since_last), llm_ms,
        )
        if on_memory_added is not None:
            try:
                on_memory_added(memory)
            except Exception:
                log.debug("dream on_memory_added raised", exc_info=True)
        return memory

    def _compose_user_payload(
        self,
        *,
        hours_since_last: float,
        rolling_summary: str,
        callbacks: list[str],
        self_memories: list[str],
        affect: "AffectState | None",
        hot_clusters: list[str] | None = None,
    ) -> str:
        parts: list[str] = [
            f"Hours since last conversation: {hours_since_last:.1f}",
        ]
        if affect is not None:
            # K44: felt-language, not floats — the dream LLM writes
            # Aiko-voiced prose, and numeric coordinates fed in here
            # used to echo into dream memories that later surface.
            parts.append(
                f"Your current mood (carried from last time): "
                f"{getattr(affect, 'mood_label', 'content')} — "
                f"{felt_phrase(getattr(affect, 'valence', 0.0), getattr(affect, 'arousal', 0.4))}"
            )
        if rolling_summary:
            parts.append("Rolling summary of last conversation:\n" + rolling_summary[:1200])
        if callbacks:
            joined = "; ".join(c[:160] for c in callbacks[:3])
            parts.append(f"Threads you'd noted to come back to: {joined}")
        if self_memories:
            joined = "; ".join(s[:160] for s in self_memories[:3])
            parts.append(f"Things you've been quietly thinking about yourself: {joined}")
        if hot_clusters:
            joined = ", ".join(h[:80] for h in hot_clusters[:3])
            parts.append(f"Threads that kept coming up lately: {joined}")
        return "\n\n".join(parts)


def _clean_dream_output(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    # Drop fenced code blocks if any.
    if text.startswith("```"):
        text = text.strip("`").strip()
        if "\n" in text:
            head, _, body = text.partition("\n")
            if len(head) <= 12 and head.strip().isalpha():
                text = body.strip()
    # Strip surrounding quotes / backticks.
    text = text.strip("\"'` \t\n")
    # First sentence-ish chunk only.
    if "\n" in text:
        text = text.split("\n", 1)[0].strip()
    if len(text) > 240:
        text = text[:240].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text


__all__ = ["DreamWorker", "_DREAM_PREFIX", "_clean_dream_output"]
