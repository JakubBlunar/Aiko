"""Pre-thought / counterfactual cache idle worker (K11 personality backlog).

A cousin of the K9 :class:`CuriositySeedWorker`. Where that worker
mints *new topics Aiko is curious about*, this one drafts *what Aiko
would say if the user asked her something*, and caches the draft so the
first real response lands smoother — no web access, no live-turn LLM
latency.

Two-stage, both calls on the local worker model:

1. **Generate questions.** Ask the LLM for a handful of plausible
   near-future user questions, grounded in the rolling summary +
   persona traits. JSON ``{"questions": [...]}``; falls through
   silently on a parse failure.
2. **Draft replies.** For up to ``pre_thought_max_per_run`` of the
   survivors (deduped against existing pre-thoughts by question
   embedding), build the K10 minimal-persona eval prompt
   (``persona_messages_builder``) and draft Aiko's in-persona reply.
   The drafted text is meta-tag-stripped and stored.

Each surviving draft is written via :meth:`MemoryStore.add` with kind
``pre_thought`` on the ``scratchpad`` tier. Crucially the EMBEDDING is
computed on the hypothetical *question* (not the combined content), so
the pre-thought surfaces through ordinary cosine RAG when the user
later asks something similar. It carries ``{question, thought,
generated_at, source}`` in ``metadata`` and ages out naturally if it
never gets used.

Opt-out via ``agent.pre_thought_enabled`` and bounded by
``pre_thought_max_active`` (``is_ready`` short-circuits at the cap and
``run`` prunes the oldest beyond it). LLM spend is bounded by a
:class:`FactCheckRateLimiter` (one budget unit per tick).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready
from app.core.services.response_text_service import strip_all_meta_tags

if TYPE_CHECKING:
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.chat_client import ChatClient
    from app.llm.embedder import Embedder


log = logging.getLogger("app.pre_thought_worker")


_SYSTEM_PROMPT = (
    "You are an inner-life worker for an AI companion named "
    "{assistant_name}. Predict a handful of questions {user_name} is "
    "plausibly about to ask {assistant_name} soon, grounded in the "
    "recent conversation. Favour concrete, answerable questions in "
    "{user_name}'s own voice (\"what do you think of X\", \"how do I "
    "Y\", \"did you ever Z\") over abstract or rhetorical ones. Avoid "
    "questions already clearly answered in the recent conversation. "
    "Reply with ONE JSON object on a single line and nothing else. "
    "Schema: {{\"questions\": [\"<= 160 chars\", ...]}}. Return between "
    "1 and {max_questions} entries."
)


_USER_TEMPLATE = (
    "PERSONA TRAITS:\n{persona}\n\n"
    "RECENT CONVERSATION (rolling summary):\n{summary}\n\n"
    "ALREADY-DRAFTED QUESTIONS (avoid anything close to these):\n"
    "{active}\n\n"
    "Predict {user_name}'s likely next questions now."
)


_MAX_TOKENS_QUESTIONS = 320
_MAX_TOKENS_DRAFT = 320
_MAX_QUESTION_CHARS = 200
_MAX_THOUGHT_CHARS = 600
_MAX_PERSONA_CHARS = 800
_MAX_SUMMARY_CHARS = 900
_MAX_ACTIVE_LIST = 8

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _trim(text: str | None, *, max_chars: int) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip(",;: ") + "\u2026"


def parse_questions(raw: str, *, max_questions: int = 5) -> list[str]:
    """Parse the stage-1 ``{"questions": [...]}`` JSON object.

    Tolerant: pulls the first ``{...}`` span out of the raw text, skips
    non-string / blank entries, trims, dedupes case-insensitively, and
    caps at ``max_questions``. Returns ``[]`` on any parse failure.
    """
    text = (raw or "").strip()
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return []
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, dict):
        return []
    questions = parsed.get("questions")
    if not isinstance(questions, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for entry in questions:
        if not isinstance(entry, str):
            continue
        q = _trim(entry, max_chars=_MAX_QUESTION_CHARS)
        if not q:
            continue
        key = q.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(q)
        if len(out) >= max(1, max_questions):
            break
    return out


def clean_thought(text: str) -> str:
    """Strip meta tags from a drafted reply and trim it for storage."""
    cleaned = strip_all_meta_tags(str(text or "")).strip()
    return _trim(cleaned, max_chars=_MAX_THOUGHT_CHARS)


def build_pre_thought_content(question: str, thought: str, user_name: str) -> str:
    """Compose the stored ``content`` for a pre-thought row.

    Human-readable so it renders sensibly as a RAG bullet, while the
    retrieval embedding is computed on ``question`` alone by the caller.
    """
    who = (user_name or "they").strip() or "they"
    return f"If {who} asks: \u201c{question}\u201d \u2014 I'd say: {thought}"


def _extract_persona_traits(raw: str) -> str:
    if not raw:
        return ""
    return _trim(raw, max_chars=_MAX_PERSONA_CHARS)


class PreThoughtWorker:
    """IdleWorker that drafts + caches Aiko's replies to likely questions."""

    name = "pre_thought"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        embedder: "Embedder",
        ollama: "ChatClient",
        chat_model: str,
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        rate_limiter: "FactCheckRateLimiter",
        persona_messages_builder: Callable[[str], list[dict[str, Any]]],
        persona_provider: Callable[[], str] | None = None,
        rolling_summary_provider: Callable[[], str] | None = None,
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
        notify_memory_added: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._rate_limiter = rate_limiter
        self._persona_messages_builder = persona_messages_builder
        self._persona_provider = persona_provider
        self._rolling_summary_provider = rolling_summary_provider
        self._user_display_name_provider = user_display_name_provider
        self._assistant_display_name_provider = assistant_display_name_provider
        self._notify_memory_added = notify_memory_added
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ───────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings, "pre_thought_interval_seconds", 3600,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(getattr(self._agent_settings, "pre_thought_enabled", True)):
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
        max_active = self._max_active()
        try:
            if len(self._active_pre_thoughts()) >= max_active:
                return False
        except Exception:
            log.debug("pre_thought: active count failed", exc_info=True)
            return False
        return True

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent_settings, "pre_thought_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        now = self._clock()
        if not self._rate_limiter.allow(now):
            return {"skipped": True, "reason": "rate_limited"}

        max_active = self._max_active()
        max_per_run = max(
            1, int(getattr(self._agent_settings, "pre_thought_max_per_run", 2)),
        )
        candidate_cap = max(
            1, int(getattr(self._agent_settings, "pre_thought_candidates", 4)),
        )
        novelty_threshold = float(
            getattr(self._agent_settings, "pre_thought_min_novelty", 0.85),
        )

        active = self._active_pre_thoughts()
        if len(active) >= max_active:
            return {"skipped": True, "reason": "max_active", "active": len(active)}

        persona_text = self._persona_block()
        summary_text = self._summary_block()
        active_text = self._active_block(active)

        t0 = time.monotonic()
        try:
            questions = self._generate_questions(
                persona_text=persona_text,
                summary_text=summary_text,
                active_text=active_text,
                candidate_cap=candidate_cap,
            )
        except Exception:
            log.warning("pre_thought question call raised", exc_info=True)
            return {"errored": True, "reason": "question_call"}
        if self._cancel_event.is_set():
            return {"cancelled": True}
        if not questions:
            llm_ms = (time.monotonic() - t0) * 1000.0
            log.info("pre_thought: no questions parsed (llm_ms=%.0f)", llm_ms)
            return {"checked": 0, "wrote": 0, "reason": "no_questions"}

        existing_vecs = [
            m.embedding for m in active
            if m.embedding is not None and getattr(m.embedding, "size", 0) > 0
        ]
        wrote: list[int] = []
        rejected_novelty = 0
        rejected_dup = 0
        rejected_empty = 0
        user_name = self._resolve_user_name()

        for question in questions:
            if len(wrote) >= max_per_run:
                break
            if self._cancel_event.is_set():
                break
            try:
                q_vec = self._embedder.embed(question)
            except Exception:
                log.debug("pre_thought embed failed (q=%r)", question, exc_info=True)
                continue

            if self._is_duplicate(q_vec, existing_vecs, novelty_threshold):
                rejected_novelty += 1
                continue

            thought = self._draft_reply(question)
            if not thought:
                rejected_empty += 1
                continue

            mem = self._write_pre_thought(
                question=question,
                thought=thought,
                user_name=user_name,
                embedding=q_vec,
                now=now,
            )
            if mem is None:
                rejected_dup += 1
                continue
            wrote.append(int(mem.id))
            existing_vecs.append(q_vec)
            if self._notify_memory_added is not None:
                try:
                    self._notify_memory_added(mem.to_dict())
                except Exception:
                    log.debug("pre_thought notify_added failed", exc_info=True)

        pruned = self._prune_to_cap(max_active)
        llm_ms = (time.monotonic() - t0) * 1000.0
        log.info(
            "pre_thought run done: wrote=%d questions=%d "
            "rejected(novelty=%d empty=%d dedupe=%d) pruned=%d llm_ms=%.0f",
            len(wrote), len(questions), rejected_novelty, rejected_empty,
            rejected_dup, pruned, llm_ms,
        )
        return {
            "checked": len(questions),
            "wrote": len(wrote),
            "memory_ids": wrote,
            "rejected_novelty": rejected_novelty,
            "rejected_empty": rejected_empty,
            "rejected_dedupe": rejected_dup,
            "pruned": pruned,
            "llm_ms": int(llm_ms),
        }

    # ── helpers ───────────────────────────────────────────────────────

    def _max_active(self) -> int:
        return max(
            1, int(getattr(self._agent_settings, "pre_thought_max_active", 12)),
        )

    @staticmethod
    def _is_duplicate(
        vec: Any, existing: list[Any], threshold: float,
    ) -> bool:
        for other in existing:
            try:
                sim = float((vec * other).sum())
            except Exception:
                sim = 0.0
            if sim >= threshold:
                return True
        return False

    def _active_pre_thoughts(self) -> list["Memory"]:
        try:
            rows = self._memory_store.iter_by_kind("pre_thought")
        except Exception:
            log.debug("iter_by_kind pre_thought failed", exc_info=True)
            return []
        return [m for m in rows if m.tier != "archive"]

    def _prune_to_cap(self, max_active: int) -> int:
        """Delete the oldest active pre-thoughts beyond the cap."""
        rows = self._active_pre_thoughts()
        if len(rows) <= max_active:
            return 0
        rows.sort(key=lambda m: (m.created_at or "", m.id))
        victims = rows[: len(rows) - max_active]
        pruned = 0
        for victim in victims:
            try:
                if self._memory_store.delete(int(victim.id)):
                    pruned += 1
            except Exception:
                log.debug(
                    "pre_thought prune delete failed id=%s",
                    victim.id, exc_info=True,
                )
        return pruned

    # ── context pack ──────────────────────────────────────────────────

    def _persona_block(self) -> str:
        if self._persona_provider is None:
            return ""
        try:
            raw = self._persona_provider() or ""
        except Exception:
            log.debug("persona provider raised", exc_info=True)
            return ""
        return _extract_persona_traits(raw)

    def _summary_block(self) -> str:
        if self._rolling_summary_provider is None:
            return ""
        try:
            raw = self._rolling_summary_provider() or ""
        except Exception:
            log.debug("summary provider raised", exc_info=True)
            return ""
        return _trim(raw, max_chars=_MAX_SUMMARY_CHARS)

    def _active_block(self, active: list["Memory"]) -> str:
        if not active:
            return "(none)"
        lines: list[str] = []
        for mem in active[:_MAX_ACTIVE_LIST]:
            metadata = mem.metadata or {}
            question = (metadata.get("question") or "").strip()
            if not question:
                continue
            lines.append(f"- {_trim(question, max_chars=120)}")
        return "\n".join(lines) if lines else "(none)"

    # ── LLM: stage 1 (questions) ──────────────────────────────────────

    def _generate_questions(
        self,
        *,
        persona_text: str,
        summary_text: str,
        active_text: str,
        candidate_cap: int,
    ) -> list[str]:
        system = _SYSTEM_PROMPT.format(
            assistant_name=self._resolve_assistant_name(),
            user_name=self._resolve_user_name(),
            max_questions=candidate_cap,
        )
        user_payload = _USER_TEMPLATE.format(
            persona=persona_text or "(persona unavailable)",
            summary=summary_text or "(no recent summary)",
            active=active_text or "(none)",
            user_name=self._resolve_user_name(),
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_payload},
        ]
        raw = self._ollama.chat(
            messages,
            options={"num_predict": _MAX_TOKENS_QUESTIONS, "temperature": 0.8},
            model=self._chat_model,
            surface="pre_thought_worker",
        )
        return parse_questions(raw or "", max_questions=candidate_cap)

    # ── LLM: stage 2 (draft reply) ────────────────────────────────────

    def _draft_reply(self, question: str) -> str:
        try:
            messages = self._persona_messages_builder(question)
        except Exception:
            log.debug("pre_thought persona builder raised", exc_info=True)
            return ""
        if not messages:
            return ""
        try:
            raw = self._ollama.chat(
                messages,
                options={"num_predict": _MAX_TOKENS_DRAFT},
                model=self._chat_model,
                surface="pre_thought_draft",
            )
        except Exception:
            log.warning("pre_thought draft call raised", exc_info=True)
            return ""
        return clean_thought(raw or "")

    # ── memory write ──────────────────────────────────────────────────

    def _write_pre_thought(
        self,
        *,
        question: str,
        thought: str,
        user_name: str,
        embedding: Any,
        now: datetime,
    ) -> "Memory | None":
        try:
            return self._memory_store.add(
                content=build_pre_thought_content(question, thought, user_name),
                kind="pre_thought",
                embedding=embedding,
                salience=0.4,
                confidence=0.5,
                tier="scratchpad",
                metadata={
                    "question": question,
                    "thought": thought,
                    "source": "pre_thought_worker",
                    "generated_at": now.isoformat(),
                },
            )
        except Exception:
            log.debug("pre_thought write failed", exc_info=True)
            return None

    # ── name resolution ───────────────────────────────────────────────

    def _resolve_user_name(self) -> str:
        if self._user_display_name_provider is None:
            return "the user"
        try:
            return (self._user_display_name_provider() or "the user") or "the user"
        except Exception:
            return "the user"

    def _resolve_assistant_name(self) -> str:
        if self._assistant_display_name_provider is None:
            return "the assistant"
        try:
            return (
                self._assistant_display_name_provider() or "the assistant"
            ) or "the assistant"
        except Exception:
            return "the assistant"


__all__ = [
    "PreThoughtWorker",
    "build_pre_thought_content",
    "clean_thought",
    "parse_questions",
]
