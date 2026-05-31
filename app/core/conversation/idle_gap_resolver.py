"""Idle gap resolver (F2.1 personality backlog follow-up).

Walks open ``knowledge_gap`` rows during quiet windows and stamps
``resolved_at`` on any whose answer is already living elsewhere in the
memory store. The motivating bug: a gap minted on Day 1 ("does Jacob
listen to specific genres while watching anime") never closes because
F1's web-search resolver only fires when the user goes to look it up
externally. The user's own answer ("I listen to metal and anime
soundtracks with guitars") gets persisted as a ``preference`` memory by
the post-summary extractor, but nothing cross-references the gap
against existing memory. So the prompt block "Things you've been
wondering about with Jacob" keeps re-injecting the same question,
session after session, until the user explicitly notices ("you maybe
forgot…") and Aiko apologises but the loop continues.

Design notes:

* **Distinct from F1.** F1 *closes gaps via fresh web search*; this
  worker *closes gaps via existing memories*. They share the same
  resolution stamp (``metadata.resolved_at`` + ``resolved_by_memory_id``)
  via :meth:`KnowledgeGapStore.mark_resolved` so retrieval / UI code
  doesn't care which path closed the gap.
* **Pure cosine, no LLM, no web.** Each tick is a few in-memory dot
  products. Cheap enough to run on a fast cadence; gentle defaults
  (10 min interval) keep CPU off the radar.
* **Backfill-friendly.** First tick after app start handles every gap
  that pre-dates the worker, including legacy rows. Subsequent ticks
  catch newly-minted gaps whose answer drifts in via the post-summary
  ``MemoryExtractor``.
* **Audit trail.** Each resolution emits an INFO log with the gap id,
  match id, score, and previews — same shape as F1's audit lines so
  ``data/app.log`` stays grep-able.
* **Companion path.** A faster, inline resolver lives in
  ``post_turn_mixin._resolve_knowledge_gaps`` for the case where the
  user answers in the same turn the gap was injected. The two paths
  are complementary: the post-turn one catches the answer the moment
  it arrives, the worker mops up everything that slipped through
  (e.g. answers stored hours later by the summary extractor).
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory.knowledge_gap_extractor import KnowledgeGapStore
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.infra.settings import AgentSettings, MemorySettings


log = logging.getLogger("app.idle_gap_resolver")


_LOG_PREVIEW_CHARS = 160


# Kinds we accept as "the answer" to a knowledge gap. Self / self_tagged
# rows describe Aiko, not the user, so they don't resolve a user-facing
# gap. ``open_question`` and other gap kinds are excluded to avoid a
# gap resolving itself by matching another gap. ``curiosity_finding``
# is included because that's the kind F1 / G3 write into when they
# answer a question — exactly the rows we want to credit.
_ANSWER_KINDS: frozenset[str] = frozenset({
    "fact",
    "preference",
    "event",
    "relationship",
    "promise",
    "shared_moment",
    "curiosity_finding",
    "reflection",
})


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _preview(text: str | None) -> str:
    if not text:
        return "<empty>"
    flat = " ".join(str(text).split())
    if len(flat) > _LOG_PREVIEW_CHARS:
        return flat[: _LOG_PREVIEW_CHARS - 1] + "…"
    return flat


class IdleGapResolver:
    """IdleWorker that closes open knowledge gaps via memory match.

    Each tick:

    1. Walk :meth:`KnowledgeGapStore.list_open` (newest first).
    2. For each gap, call :meth:`MemoryStore.search` with the gap's
       *already stored* embedding so we don't re-embed.
    3. Filter hits to :data:`_ANSWER_KINDS` (avoid resolving a gap with
       another gap, or with a self-* memory).
    4. Pick the top hit above ``threshold`` and call
       :meth:`KnowledgeGapStore.mark_resolved` with
       ``answer_memory_id=<hit.id>``.

    Bounded per-tick (``per_tick_cap``) so a session that just minted
    a wave of gaps doesn't eat the tick budget.
    """

    name: str = "gap_resolver"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        gap_store: "KnowledgeGapStore",
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings | None" = None,
        cancel_event: threading.Event | None = None,
        notify_memory_updated: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._gap_store = gap_store
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._cancel_event = cancel_event
        self._notify_memory_updated = notify_memory_updated
        self._clock = clock or _utcnow

    @property
    def interval_seconds(self) -> float:
        # Default 600s (10 min). The work is cheap; the cadence is
        # tuned for "show up shortly after a gap was minted" without
        # spamming logs on long quiet stretches.
        return float(
            getattr(
                self._agent_settings,
                "gap_resolver_interval_seconds",
                600,
            )
        )

    @property
    def threshold(self) -> float:
        return float(
            getattr(
                self._agent_settings,
                "gap_resolver_threshold",
                0.55,
            )
        )

    @property
    def per_tick_cap(self) -> int:
        return max(
            1,
            int(
                getattr(
                    self._agent_settings,
                    "gap_resolver_per_tick",
                    5,
                )
            ),
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(
            getattr(self._agent_settings, "gap_resolver_enabled", True)
        ):
            return False
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        ):
            return False
        # Cheap "is there anything to do" check — list_open walks the
        # in-memory mirror, no SQL roundtrip.
        try:
            if not self._gap_store.list_open():
                return False
        except Exception:
            return False
        return True

    def run(self) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "gap_resolver_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event is not None and self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        try:
            open_gaps = self._gap_store.list_open()
        except Exception:
            log.warning("gap_resolver: list_open failed", exc_info=True)
            return {"skipped": True, "reason": "list_open_failed"}
        if not open_gaps:
            return {"skipped": True, "reason": "no_open_gaps"}

        threshold = self.threshold
        cap = self.per_tick_cap
        resolved = 0
        scanned = 0
        for gap in open_gaps:
            if self._cancel_event is not None and self._cancel_event.is_set():
                break
            if scanned >= cap:
                break
            scanned += 1
            if gap.embedding is None or gap.embedding.size == 0:
                continue
            try:
                hits = self._memory_store.search(
                    gap.embedding,
                    top_k=5,
                    min_score=threshold,
                )
            except Exception:
                log.debug(
                    "gap_resolver: search failed (gap_id=%s)",
                    gap.id,
                    exc_info=True,
                )
                continue
            best: tuple["Memory", float] | None = None
            for hit in hits:
                mem = hit.memory
                if mem.id == gap.id:
                    continue
                if mem.kind not in _ANSWER_KINDS:
                    continue
                if best is None or hit.score > best[1]:
                    best = (mem, float(hit.score))
            if best is None:
                continue
            answer, score = best
            try:
                ok = self._gap_store.mark_resolved(
                    int(gap.id),
                    answer_memory_id=int(answer.id),
                    resolved_by="memory_match",
                    similarity=score,
                )
            except Exception:
                log.debug(
                    "gap_resolver: mark_resolved threw (gap_id=%s)",
                    gap.id,
                    exc_info=True,
                )
                continue
            if not ok:
                continue
            resolved += 1
            log.info(
                "gap_resolver: resolved gap_id=%s by memory_id=%s "
                "score=%.2f kind=%s gap=%r answer=%r",
                gap.id,
                answer.id,
                score,
                answer.kind,
                _preview(gap.content),
                _preview(answer.content),
            )
            if self._notify_memory_updated is not None:
                try:
                    fresh = self._memory_store.get(int(gap.id))
                except Exception:
                    fresh = None
                if fresh is not None:
                    try:
                        self._notify_memory_updated(fresh.to_dict())
                    except Exception:
                        log.debug(
                            "gap_resolver: notify_updated failed",
                            exc_info=True,
                        )

        return {
            "scanned": scanned,
            "resolved": resolved,
            "open_remaining": max(0, len(open_gaps) - resolved),
            "threshold": round(threshold, 3),
        }


__all__ = ["IdleGapResolver"]
