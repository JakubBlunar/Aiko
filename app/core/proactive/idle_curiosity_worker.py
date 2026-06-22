"""Idle curiosity worker (G3 personality backlog).

Picks one of Aiko's existing ``open_question`` memories at a time
during quiet windows, runs it through the privacy gate that already
guards F1, web-searches the scrubbed query, distils a short answer
via the local LLM, and stores the result as a high-confidence
``curiosity_finding`` memory linked back to the originating question.

The persona file decides *how* Aiko surfaces these later (the typical
shape is "I was reading about X — turns out…" rather than reciting
the fact bare). We deliberately stop short of writing a prepared
nudge: the answer just lives in the memory pool and normal RAG
retrieval brings it forward when the conversation drifts that way.

Design notes:

* **Distinct from F1.** F1 *checks Aiko's claims*; G3 *fills her
  knowledge gaps*. They share the same web-search tool but each owns
  its own hour/day budget so a chatty fact-checking pass can't
  starve curiosity (and vice versa).
* **Idempotent.** Once a question has either resolved or been
  marked inconclusive within the cooldown, subsequent ticks skip it.
  No retry storms even if the queue is large.
* **Privacy first.** Open-questions are produced by the speaking-
  window ``CuriosityWorker`` from the *user's* conversation, so they
  often contain pronouns or names. The same scrubber + classifier
  that protects F1 protects G3, and a question that won't scrub is
  stamped with ``metadata.curiosity_skipped='privacy'`` so it stops
  consuming ticks.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.idle_curiosity_worker")


# ── prompt template ────────────────────────────────────────────────────

_SYSTEM_PROMPT = (
    "You answer one short factual question using web search excerpts. "
    "Reply with ONE JSON object on a single line and nothing else. "
    "Schema: {\"answer\": \"<= 200 chars, plain prose, no quotes\", "
    "\"confidence\": <number in [0, 1]>}. "
    "If the excerpts are off-topic or contradictory, set confidence "
    "below 0.6 and keep the answer short or empty. Never invent facts "
    "the excerpts don't support."
)

_USER_TEMPLATE = (
    "QUESTION: {question}\n"
    "EXCERPTS:\n{excerpts}"
)


# Caps on the prompt so a long search result can't blow up the
# context. Mirrors the F1 fact-checker tuning.
_MAX_SNIPPET_CHARS = 400
_MAX_EXCERPTS = 3
_DISTIL_MAX_TOKENS = 160


# Cap how much of any text we render in a single log line. The
# privacy module already truncates its own previews; this cap is for
# the worker-side audit trail.
_LOG_PREVIEW_CHARS = 200


# Memory-confidence cap on the written ``curiosity_finding``. We
# never stamp anything as "verified fact" — there's always a chance
# the snippets misled the distil. The cap leaves room for F1 to
# nudge it up to 0.95 later if the user repeats it back and the
# fact-checker confirms.
_MAX_FINDING_CONFIDENCE = 0.9


# How long to skip a question after a privacy-skip or inconclusive
# pass before retrying. 7 days lets a future scrubber update or a
# new search result eventually win without burning ticks today.
_SKIP_COOLDOWN = timedelta(days=7)


_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _preview(text: str | None) -> str:
    if not text:
        return "<empty>"
    flat = " ".join(str(text).split())
    if len(flat) > _LOG_PREVIEW_CHARS:
        return flat[: _LOG_PREVIEW_CHARS - 1] + "…"
    return flat


@dataclass(frozen=True)
class CuriosityAnswer:
    """Parsed distil output."""

    answer: str
    confidence: float


class IdleCuriosityWorker:
    """IdleWorker that turns ``open_question`` memories into findings."""

    name = "idle_curiosity"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        embedder: "Embedder",
        ollama: "OllamaClient",
        chat_model: str,
        web_search_tool: Any,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        user_names_provider: Callable[[], list[str]] | None = None,
        assistant_name_provider: Callable[[], str | None] | None = None,
        notify_memory_added: Callable[[dict[str, Any]], None] | None = None,
        notify_memory_updated: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
        query_reformulator: Callable[[str], str | None] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._web_search = web_search_tool
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._user_names_provider = user_names_provider
        self._assistant_name_provider = assistant_name_provider
        self._notify_memory_added = notify_memory_added
        self._notify_memory_updated = notify_memory_updated
        self._clock = clock or _utcnow
        self._query_reformulator = query_reformulator

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "idle_curiosity_interval_seconds",
                1800,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(
            getattr(self._agent_settings, "idle_curiosity_enabled", True)
        ):
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
        # Cheap "is there anything to do" check. ``iter_by_kind`` is
        # a mirror walk; no SQL roundtrip.
        if self._pick_next_question(now=now) is None:
            return False
        return True

    def run(self) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "idle_curiosity_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}
        now = self._clock()
        question = self._pick_next_question(now=now)
        if question is None:
            return {"skipped": True, "reason": "no_unresolved_question"}

        if not self._rate_limiter.allow(now):
            log.info(
                "curiosity skip: rate limited (memory_id=%s question=%r)",
                question.id,
                _preview(question.content),
            )
            return {"skipped": True, "reason": "rate_limited"}

        log.info(
            "curiosity start: memory_id=%s question=%r",
            question.id,
            _preview(question.content),
        )

        safe_query = self._scrub(question.content)
        if safe_query is None:
            log.info(
                "curiosity skip: privacy gate dropped question "
                "memory_id=%s",
                question.id,
            )
            self._mark_skipped(question, reason="privacy", when=now)
            return {"skipped": True, "reason": "privacy_gate"}
        log.info(
            "curiosity scrubbed: memory_id=%s safe_query=%r",
            question.id,
            _preview(safe_query),
        )

        search_t0 = time.monotonic()
        try:
            snippets = self._search(safe_query)
        except Exception:
            search_ms = (time.monotonic() - search_t0) * 1000.0
            log.warning(
                "curiosity search failed: memory_id=%s elapsed_ms=%.0f",
                question.id,
                search_ms,
                exc_info=True,
            )
            return {"errored": True, "reason": "search_failed"}
        search_ms = (time.monotonic() - search_t0) * 1000.0
        result_summary = [
            {
                "title": (s.get("title") or "")[:80],
                "url": (s.get("url") or "")[:120],
            }
            for s in snippets
        ]
        log.info(
            "curiosity search done: memory_id=%s elapsed_ms=%.0f "
            "result_count=%d top=%s",
            question.id,
            search_ms,
            len(snippets),
            result_summary,
        )
        if log.isEnabledFor(logging.DEBUG):
            for idx, s in enumerate(snippets):
                log.debug(
                    "curiosity snippet[%d]: title=%r url=%s body=%r",
                    idx,
                    (s.get("title") or "")[:120],
                    (s.get("url") or "")[:160],
                    _preview(s.get("snippet")),
                )
        if self._cancel_event.is_set():
            log.info(
                "curiosity cancelled mid-search: memory_id=%s",
                question.id,
            )
            return {"cancelled": True}

        if not snippets:
            self._mark_inconclusive(question, when=now, reason="no_results")
            return {
                "checked": 1,
                "memory_id": question.id,
                "outcome": "inconclusive",
                "reason": "no_results",
            }

        distil_t0 = time.monotonic()
        answer = self._distil(safe_query, snippets)
        distil_ms = (time.monotonic() - distil_t0) * 1000.0
        if answer is None:
            log.info(
                "curiosity distil cancel/parse-fail: memory_id=%s "
                "elapsed_ms=%.0f",
                question.id,
                distil_ms,
            )
            return {"cancelled": True}
        log.info(
            "curiosity distil done: memory_id=%s elapsed_ms=%.0f "
            "confidence=%.2f answer=%r",
            question.id,
            distil_ms,
            answer.confidence,
            _preview(answer.answer),
        )

        if not answer.answer or answer.confidence < 0.6:
            self._mark_inconclusive(
                question,
                when=now,
                reason=(
                    "low_confidence"
                    if answer.answer
                    else "empty_answer"
                ),
            )
            return {
                "checked": 1,
                "memory_id": question.id,
                "outcome": "inconclusive",
                "confidence": float(answer.confidence),
            }

        wrote_id = self._write_finding(
            question=question,
            answer=answer,
            safe_query=safe_query,
            now=now,
        )
        if wrote_id is None:
            return {
                "checked": 1,
                "memory_id": question.id,
                "outcome": "write_failed",
            }
        self._mark_resolved(
            question,
            when=now,
            answer_memory_id=wrote_id,
        )
        log.info(
            "curiosity apply done: memory_id=%s wrote_memory_id=%s "
            "confidence=%.2f",
            question.id,
            wrote_id,
            min(answer.confidence, _MAX_FINDING_CONFIDENCE),
        )
        return {
            "checked": 1,
            "memory_id": question.id,
            "outcome": "resolved",
            "answer_memory_id": int(wrote_id),
            "confidence": float(
                min(answer.confidence, _MAX_FINDING_CONFIDENCE),
            ),
        }

    # ── question selection ───────────────────────────────────────────

    def _pick_next_question(
        self, *, now: datetime,
    ) -> "Memory | None":
        """Oldest unresolved ``open_question`` not in cooldown."""
        try:
            candidates = self._memory_store.iter_by_kind("open_question")
        except Exception:
            log.debug(
                "curiosity: iter_by_kind raised", exc_info=True,
            )
            return None
        # ``iter_by_kind`` returns a mirror snapshot in arbitrary order.
        # Sort by ``created_at`` so the *oldest* question is tried
        # first; that gives every question a chance over time even
        # when the speaking-window CuriosityWorker keeps minting new
        # ones.
        candidates_sorted = sorted(
            candidates,
            key=lambda m: m.created_at or "",
        )
        for mem in candidates_sorted:
            metadata = mem.metadata or {}
            if metadata.get("curiosity_resolved_at"):
                continue
            skipped_at = _parse_iso(metadata.get("curiosity_skipped_at"))
            if skipped_at is not None and now - skipped_at < _SKIP_COOLDOWN:
                continue
            inconclusive_at = _parse_iso(
                metadata.get("curiosity_inconclusive_at"),
            )
            if (
                inconclusive_at is not None
                and now - inconclusive_at < _SKIP_COOLDOWN
            ):
                continue
            content = (mem.content or "").strip()
            if not content:
                continue
            return mem
        return None

    # ── pieces ───────────────────────────────────────────────────────

    def _scrub(self, question_text: str) -> str | None:
        """Privacy-scrub the question. Late-binds user / assistant names.

        Reuses the same gate the F1 fact-checker uses, which already
        logs every decision at INFO. We only have to log the
        worker-side outcome here.
        """
        from app.core.memory.fact_check_privacy import scrub_claim_for_search

        user_names: list[str] | None = None
        if self._user_names_provider is not None:
            try:
                provided = self._user_names_provider()
                if provided:
                    user_names = list(provided)
            except Exception:
                user_names = None
        assistant_name: str | None = None
        if self._assistant_name_provider is not None:
            try:
                assistant_name = self._assistant_name_provider() or None
            except Exception:
                assistant_name = None
        if self._query_reformulator is not None:
            from app.core.memory.query_reformulation import (
                reformulate_query_for_search,
            )

            return reformulate_query_for_search(
                question_text,
                reformulate_fn=self._query_reformulator,
                user_names=user_names,
                assistant_name=assistant_name,
            )
        return scrub_claim_for_search(
            question_text,
            user_names=user_names,
            assistant_name=assistant_name,
        )

    def _search(self, safe_query: str) -> list[dict[str, str]]:
        if self._web_search is None:
            return []
        result_text = self._web_search.run(
            {"query": safe_query, "max_results": _MAX_EXCERPTS},
        )
        try:
            parsed = json.loads(result_text)
        except (json.JSONDecodeError, TypeError):
            return []
        results = parsed.get("results", []) if isinstance(parsed, dict) else []
        out: list[dict[str, str]] = []
        for item in results[:_MAX_EXCERPTS]:
            if not isinstance(item, dict):
                continue
            snippet = str(item.get("snippet") or item.get("body") or "").strip()
            if not snippet:
                continue
            out.append({
                "title": str(item.get("title", ""))[:120],
                "url": str(item.get("url", ""))[:200],
                "snippet": snippet[:_MAX_SNIPPET_CHARS],
            })
        return out

    def _distil(
        self,
        safe_query: str,
        snippets: list[dict[str, str]],
    ) -> CuriosityAnswer | None:
        excerpts_text = "\n".join(
            f"- {s['title']} ({s['url']}): {s['snippet']}"
            for s in snippets[:_MAX_EXCERPTS]
        )
        user_content = _USER_TEMPLATE.format(
            question=safe_query,
            excerpts=excerpts_text,
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "curiosity distil prompt: model=%s prompt_chars=%d "
                "user_payload=%r",
                self._chat_model,
                len(user_content) + len(_SYSTEM_PROMPT),
                _preview(user_content),
            )
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _DISTIL_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="idle_curiosity_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("curiosity distil call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return None
        log.debug(
            "curiosity distil raw: chars=%d preview=%r",
            len(raw),
            _preview(raw),
        )
        return self._parse_answer(raw)

    @staticmethod
    def _parse_answer(raw: str) -> CuriosityAnswer | None:
        text = raw.strip()
        match = _JSON_OBJECT_RE.search(text)
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        answer = str(parsed.get("answer", "")).strip()
        if len(answer) > 240:
            answer = answer[:237].rsplit(" ", 1)[0] + "…"
        try:
            conf = float(parsed.get("confidence", 0.0))
        except (TypeError, ValueError):
            conf = 0.0
        conf = max(0.0, min(1.0, conf))
        return CuriosityAnswer(answer=answer, confidence=conf)

    # ── memory writes ────────────────────────────────────────────────

    def _write_finding(
        self,
        *,
        question: "Memory",
        answer: CuriosityAnswer,
        safe_query: str,
        now: datetime,
    ) -> int | None:
        try:
            embedding = self._embedder.embed(answer.answer)
        except Exception:
            log.warning("curiosity embed failed", exc_info=True)
            return None
        confidence = min(_MAX_FINDING_CONFIDENCE, float(answer.confidence))
        try:
            new_mem = self._memory_store.add(
                content=answer.answer,
                kind="curiosity_finding",
                embedding=embedding,
                salience=0.65,
                confidence=confidence,
                tier="long_term",
                metadata={
                    "source_open_question_id": int(question.id),
                    "source_query": safe_query[:200],
                    "discovered_at": now.isoformat(),
                },
            )
        except Exception:
            log.warning("curiosity finding write failed", exc_info=True)
            return None
        if new_mem is None:
            log.info(
                "curiosity finding deduped against existing memory "
                "for question id=%s",
                question.id,
            )
            return None
        if self._notify_memory_added is not None:
            try:
                self._notify_memory_added(new_mem.to_dict())
            except Exception:
                log.debug("curiosity notify added failed", exc_info=True)
        return int(new_mem.id)

    def _mark_resolved(
        self,
        question: "Memory",
        *,
        when: datetime,
        answer_memory_id: int,
    ) -> None:
        try:
            updated = self._memory_store.update(
                question.id,
                metadata={
                    "curiosity_resolved_at": when.isoformat(),
                    "curiosity_answer_memory_id": int(answer_memory_id),
                },
                metadata_merge=True,
            )
        except Exception:
            log.debug(
                "curiosity mark_resolved failed for id=%s",
                question.id,
                exc_info=True,
            )
            return
        if updated is not None and self._notify_memory_updated is not None:
            try:
                self._notify_memory_updated(updated.to_dict())
            except Exception:
                log.debug(
                    "curiosity notify updated failed", exc_info=True,
                )

    def _mark_inconclusive(
        self,
        question: "Memory",
        *,
        when: datetime,
        reason: str,
    ) -> None:
        try:
            self._memory_store.update(
                question.id,
                metadata={
                    "curiosity_inconclusive_at": when.isoformat(),
                    "curiosity_inconclusive_reason": reason,
                },
                metadata_merge=True,
            )
        except Exception:
            log.debug(
                "curiosity mark_inconclusive failed for id=%s",
                question.id,
                exc_info=True,
            )

    def _mark_skipped(
        self,
        question: "Memory",
        *,
        reason: str,
        when: datetime,
    ) -> None:
        try:
            self._memory_store.update(
                question.id,
                metadata={
                    "curiosity_skipped": reason,
                    "curiosity_skipped_at": when.isoformat(),
                },
                metadata_merge=True,
            )
        except Exception:
            log.debug(
                "curiosity mark_skipped failed for id=%s",
                question.id,
                exc_info=True,
            )


__all__ = ["IdleCuriosityWorker", "CuriosityAnswer"]
