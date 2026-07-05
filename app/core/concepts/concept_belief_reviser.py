"""L15 belief revision -- concept -> supporting-memory re-examination.

When L9 flips an identity concept to ``contradicted``, the doubt should
flow *back down* to the memories that supported the (now-disproven)
belief -- closing the loop into a proper belief-revision network. This
reviser walks the concept's ``evidence`` memories and, for each one that
itself conflicts with the disproving counter-evidence, arbitrates one of
three resolutions:

- **(a) memory inaccurate** (bad extraction / misremembered) -> lower its
  ``confidence`` (plasticity-damped by the concept's stickiness, floored);
- **(b) memory accurate but superseded** (true then, stale now) ->
  reclassify it to a ``past_event`` with a fresh ``relevance_until``,
  **not** a confidence cut -- the fact still happened;
- **(c) memory fine, the concept was a bad inference** -> no memory write
  (the concept was already penalised by L9).

Design guardrails (see ``docs/personality-backlog/concepts.md`` L15):

- **Trigger, not a blind write.** A concept's confidence never directly
  overwrites a memory's confidence -- that back-edge is an undamped loop
  (memory confidence feeds concept confidence in L3). Each supporting
  memory is arbitrated on its own merits against the counter-evidence.
- **Observations outrank inferences.** Pinned memories are never touched;
  the (a) confidence cut is a damped step clamped to a floor.
- **Bounded like L2 / L3 / L9.** The L3 worker caps how many concepts get
  revised per tick (``concept_belief_revision_batch_size``) and how many
  supporting memories each (``concept_belief_revision_max_evidence``);
  the (a)/(b)/(c) arbitration spends a rate-limited maintenance-LLM call
  (its own hour/day budget via ``FactCheckRateLimiter`` with
  ``state_key='concept_belief_revision.rate_state'``) and *defers* the
  memory when the budget is spent -- the cheap ``classify_pair`` gate
  still runs, so only genuine conflicts ever reach the LLM.

The **cheap gate** reuses F5's :func:`classify_pair` over each supporting
memory vs the disproving snippet: a ``no`` verdict means the memory is
compatible with the counter-evidence (so it isn't the problem -- left
alone); ``definite`` / ``borderline`` means the memory conflicts and
needs the 3-way arbitration to decide inaccurate vs superseded vs
bad-inference.

L3 stays the single writer of *concept* state; this reviser only writes
*memory* state (confidence / temporal fields), exactly like F1 / F5.
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

from app.core.concepts.concept_lifecycle import apply_contradiction_penalty
from app.core.memory.conflict_heuristics import (
    HEURISTIC_NO,
    classify_pair,
)

if TYPE_CHECKING:
    from app.core.concepts.concept_store import Concept, ConceptStore
    from app.core.concepts.concept_contradiction import ContradictionVerdict
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.concept_belief_reviser")


_LOG_PREVIEW_CHARS = 160
_ARBITER_MAX_TOKENS = 96
_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)

# Resolution vocabulary the arbiter LLM returns. ``KEEP`` folds both
# "the memory is fine" and "the concept was a bad inference" (c): in
# either case there is no memory write.
_RES_INACCURATE = "INACCURATE"
_RES_SUPERSEDED = "SUPERSEDED"
_RES_KEEP = "KEEP"
_VALID_RESOLUTIONS = {_RES_INACCURATE, _RES_SUPERSEDED, _RES_KEEP}

_SYSTEM_PROMPT = (
    "A BELIEF Aiko held about a person has just been contradicted by new "
    "COUNTER-EVIDENCE. You are re-examining one older SUPPORTING memory "
    "that helped form the belief. Decide how to revise it. Answer with "
    "ONE JSON object on a single line and nothing else. Schema: "
    "{\"resolution\": \"INACCURATE\" | \"SUPERSEDED\" | \"KEEP\", "
    "\"reason\": \"<= 80 chars\"}. "
    "INACCURATE = the supporting memory was wrong (misremembered / bad "
    "extraction). "
    "SUPERSEDED = the supporting memory was true when recorded but is now "
    "out of date (the person changed). "
    "KEEP = the supporting memory is still fine; the belief was just an "
    "over-reach. "
    "Be conservative: prefer KEEP when uncertain, and never mark a "
    "specific dated observation INACCURATE just because the person later "
    "changed."
)

_USER_TEMPLATE = (
    "BELIEF: {belief}\nCOUNTER-EVIDENCE: {counter}\nSUPPORTING: {support}"
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


@dataclass(slots=True)
class _ArbiterVerdict:
    resolution: str  # INACCURATE | SUPERSEDED | KEEP
    reason: str


@dataclass(slots=True)
class RevisionOutcome:
    """Per-concept tally of a belief-revision pass."""

    checked: int = 0
    lowered: int = 0
    superseded: int = 0
    kept: int = 0
    skipped_pinned: int = 0
    deferred_rate_limit: int = 0


class ConceptBeliefReviser:
    """Re-examines a contradicted concept's supporting memories (L15).

    Read-mostly: the only writes are to *memory* state via
    ``MemoryStore.update`` / ``MemoryStore.reclassify``. Concept state is
    owned exclusively by the L3 lifecycle worker.
    """

    def __init__(
        self,
        *,
        concept_store: "ConceptStore",
        memory_store: "MemoryStore",
        ollama: "OllamaClient",
        chat_model: str,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event | None = None,
        max_evidence: int = 6,
        confidence_penalty: float = 0.2,
        confidence_floor: float = 0.2,
        superseded_relevance_days: float = 7.0,
        notify_memory_updated: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._concept_store = concept_store
        self._memory_store = memory_store
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._max_evidence = max(1, int(max_evidence))
        self._confidence_penalty = max(0.0, float(confidence_penalty))
        self._confidence_floor = min(1.0, max(0.0, float(confidence_floor)))
        self._superseded_relevance_days = max(
            0.0, float(superseded_relevance_days)
        )
        self._notify_memory_updated = notify_memory_updated
        self._clock = clock or _utcnow

    # ── public API ───────────────────────────────────────────────────

    def revise(
        self,
        concept: "Concept",
        verdict: "ContradictionVerdict",
        *,
        now: datetime | None = None,
    ) -> RevisionOutcome:
        """Re-examine ``concept``'s supporting memories against the
        counter-evidence in ``verdict`` and apply per-memory resolutions.

        Returns a :class:`RevisionOutcome` tally; never raises for a
        single-memory failure (best-effort, logged at debug)."""
        out = RevisionOutcome()
        when = now or self._clock()
        counter_text = self._counter_text(verdict)
        belief_text = self._belief_text(concept)
        if not counter_text or not belief_text:
            return out
        for mem in self._supporting_memories(concept):
            if self._cancelled():
                break
            if out.checked >= self._max_evidence:
                break
            out.checked += 1
            if bool(getattr(mem, "pinned", False)):
                # Observations Aiko was told to always keep are never
                # auto-revised -- the user curated them.
                out.skipped_pinned += 1
                continue
            content = (getattr(mem, "content", "") or "").strip()
            if not content:
                continue
            # Cheap gate: a supporting memory that doesn't conflict with
            # the counter-evidence isn't the problem -- leave it alone.
            heuristic = classify_pair(content, counter_text)
            if heuristic.label == HEURISTIC_NO:
                out.kept += 1
                continue
            # Real conflict -> arbitrate (a)/(b)/(c). One LLM call per
            # memory, gated by the shared budget; defer when spent.
            if not self._rate_limiter.allow(when):
                out.deferred_rate_limit += 1
                log.info(
                    "belief-revision defer (rate-limited): concept_id=%s "
                    "mem_id=%s",
                    getattr(concept, "concept_id", "?"),
                    getattr(mem, "id", "?"),
                )
                continue
            arb = self._arbitrate(belief_text, counter_text, content)
            if arb is None:
                out.kept += 1
                continue
            self._apply(concept, mem, arb, when, out)
        if out.lowered or out.superseded:
            log.info(
                "belief-revision applied: concept_id=%s checked=%d "
                "lowered=%d superseded=%d kept=%d skipped_pinned=%d "
                "deferred=%d",
                getattr(concept, "concept_id", "?"),
                out.checked,
                out.lowered,
                out.superseded,
                out.kept,
                out.skipped_pinned,
                out.deferred_rate_limit,
            )
        return out

    # ── resolution application (memory writes only) ──────────────────

    def _apply(
        self,
        concept: "Concept",
        mem: "Memory",
        arb: _ArbiterVerdict,
        now: datetime,
        out: RevisionOutcome,
    ) -> None:
        mem_id = int(getattr(mem, "id", 0) or 0)
        if mem_id <= 0:
            return
        if arb.resolution == _RES_INACCURATE:
            self._lower_confidence(concept, mem, arb, now, out)
        elif arb.resolution == _RES_SUPERSEDED:
            self._mark_superseded(concept, mem, arb, now, out)
        else:
            out.kept += 1

    def _lower_confidence(
        self,
        concept: "Concept",
        mem: "Memory",
        arb: _ArbiterVerdict,
        now: datetime,
        out: RevisionOutcome,
    ) -> None:
        """(a) The supporting memory was wrong -> plasticity-damped,
        floored confidence cut. Never touches accurate-but-stale facts.

        L16: the cut is scaled by the *concept's* plasticity (same
        damping as the L9 disproof step): a sticky, low-plasticity belief
        resists revising its own evidence, so it applies a gentler cut
        than a fluid, high-plasticity one for the same base penalty."""
        mem_id = int(mem.id)
        current = float(getattr(mem, "confidence", 0.7))
        new_conf = apply_contradiction_penalty(
            current,
            penalty=self._confidence_penalty,
            plasticity=float(getattr(concept, "plasticity", 0.5)),
            floor=self._confidence_floor,
        )
        if new_conf >= current:
            # Already at/under the floor -- nothing to lower.
            out.kept += 1
            return
        try:
            updated = self._memory_store.update(
                mem_id,
                confidence=new_conf,
                metadata={
                    "belief_revised_by": int(concept.concept_id),
                    "belief_revised_at": now.isoformat(),
                    "belief_revision": "inaccurate",
                    "belief_revision_reason": arb.reason,
                },
                metadata_merge=True,
            )
        except Exception:
            log.debug(
                "belief-revision lower failed (mem_id=%s)", mem_id,
                exc_info=True,
            )
            return
        if updated is None:
            return
        out.lowered += 1
        self._notify(mem_id)

    def _mark_superseded(
        self,
        concept: "Concept",
        mem: "Memory",
        arb: _ArbiterVerdict,
        now: datetime,
        out: RevisionOutcome,
    ) -> None:
        """(b) The supporting memory was true then, stale now -> reclassify
        to ``past_event`` with a short ``relevance_until`` window so it
        stops surfacing in normal RAG but stays for retrospective use.
        Confidence is untouched: the fact still happened."""
        mem_id = int(mem.id)
        until = (
            now + timedelta(days=self._superseded_relevance_days)
        ).isoformat()
        try:
            reclassified = self._memory_store.reclassify(
                mem_id,
                temporal_type="past_event",
                relevance_until=until,
            )
        except Exception:
            log.debug(
                "belief-revision supersede failed (mem_id=%s)", mem_id,
                exc_info=True,
            )
            return
        if reclassified is None:
            return
        # Stamp provenance without disturbing the temporal fields.
        try:
            self._memory_store.update(
                mem_id,
                metadata={
                    "belief_revised_by": int(concept.concept_id),
                    "belief_revised_at": now.isoformat(),
                    "belief_revision": "superseded",
                    "belief_revision_reason": arb.reason,
                },
                metadata_merge=True,
            )
        except Exception:
            log.debug(
                "belief-revision supersede stamp failed (mem_id=%s)", mem_id,
                exc_info=True,
            )
        out.superseded += 1
        self._notify(mem_id)

    def _notify(self, memory_id: int) -> None:
        if self._notify_memory_updated is None:
            return
        try:
            self._notify_memory_updated({"memory_id": int(memory_id)})
        except Exception:
            log.debug("belief-revision notify failed", exc_info=True)

    # ── supporting-memory collection ─────────────────────────────────

    def _supporting_memories(self, concept: "Concept") -> list["Memory"]:
        """The concept's ``evidence`` memories (memory -> concept edges),
        resolved through the mirror, in ``ordinal`` order. Cluster / other
        evidence node types are skipped -- only memories are revisable."""
        try:
            edges = self._concept_store.evidence_of(int(concept.concept_id))
        except Exception:
            log.debug(
                "belief-revision evidence lookup failed (id=%s)",
                getattr(concept, "concept_id", "?"),
                exc_info=True,
            )
            return []
        out: list["Memory"] = []
        seen: set[int] = set()
        for edge in edges:
            if edge.src_type != "memory":
                continue
            try:
                mem_id = int(edge.src_id)
            except (TypeError, ValueError):
                continue
            if mem_id in seen:
                continue
            seen.add(mem_id)
            mem = self._memory_store.get(mem_id)
            if mem is not None:
                out.append(mem)
        return out

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _counter_text(verdict: "ContradictionVerdict") -> str:
        return (getattr(verdict, "snippet", "") or "").strip()

    @staticmethod
    def _belief_text(concept: "Concept") -> str:
        label = (getattr(concept, "label", "") or "").strip()
        rationale = (getattr(concept, "rationale", "") or "").strip()
        if label and rationale:
            return f"{label}. {rationale}"
        return label or rationale

    def _cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    # ── LLM arbiter (mirrors F5's verifier) ──────────────────────────

    def _arbitrate(
        self, belief_text: str, counter_text: str, support_text: str
    ) -> _ArbiterVerdict | None:
        user_content = _USER_TEMPLATE.format(
            belief=belief_text or "",
            counter=counter_text or "",
            support=support_text or "",
        )
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        chunks: list[str] = []
        t0 = time.monotonic()
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _ARBITER_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                think=True,
                surface="concept_belief_reviser",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("belief-revision arbiter call raised", exc_info=True)
            return None
        if self._cancelled():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return None
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "belief-revision arbiter raw: chars=%d elapsed_ms=%.0f "
                "preview=%r",
                len(raw),
                (time.monotonic() - t0) * 1000.0,
                _preview(raw),
            )
        return self._parse_verdict(raw)

    @staticmethod
    def _parse_verdict(raw: str) -> _ArbiterVerdict | None:
        match = _JSON_OBJECT_RE.search(raw or "")
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        resolution = str(parsed.get("resolution", "")).strip().upper()
        if resolution not in _VALID_RESOLUTIONS:
            return None
        reason = str(parsed.get("reason", "")).strip()
        if len(reason) > 200:
            reason = reason[:197] + "\u2026"
        return _ArbiterVerdict(resolution=resolution, reason=reason)


__all__ = ["ConceptBeliefReviser", "RevisionOutcome"]
