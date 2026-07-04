"""L9 concept contradiction detector -- a read-only counter-evidence probe.

The L3 lifecycle worker stays the **single writer** of concept
``confidence`` / ``status``; this detector only *reads* and returns a
verdict. Given one active identity concept, it looks for a memory that
disproves the belief and, if it finds one, hands the worker a
:class:`ContradictionVerdict` so L3 can apply the plasticity-damped
penalty (:func:`app.core.concepts.concept_lifecycle.apply_contradiction_penalty`)
and, once confidence falls far enough, step the concept into the
revivable ``contradicted`` status.

**Reuses F5's three-tier gate** (see
:mod:`app.core.memory.memory_conflict_worker`), just concept-vs-memory
instead of memory-vs-memory:

1. **Cosine band.** Pull the concept's nearest memories
   (:meth:`MemoryStore.search`) and keep only those inside
   ``[similarity_min, similarity_max)``. The band is *wider* than F5's
   because the concept side is an abstract label -- it is only a
   candidate filter; whether a near memory *agrees* or *contradicts* is
   decided below.
2. **Heuristic** (:func:`app.core.memory.conflict_heuristics.classify_pair`,
   run over ``memory.content`` vs ``"{label}. {rationale}"``):
   ``definite`` (a clean negation flip / preference-verb antonym --
   exactly the vocabulary of identity beliefs) confirms without an LLM
   call; ``no`` is dropped (a near memory with no opposition signal is
   supporting / neutral, not counter-evidence); ``borderline`` escalates.
3. **LLM** for borderlines only, gated by a
   :class:`FactCheckRateLimiter` with its own
   ``state_key='concept_contradiction.rate_state'`` budget. Same
   one-line-JSON ``YES`` / ``NO`` / ``UNRELATED`` verdict F5 uses; only
   ``YES`` confirms.

The first confirmed contradiction (highest-similarity memory first)
short-circuits and is returned; the worker applies a single penalty per
tick regardless of how many memories would contradict.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.core.memory.conflict_heuristics import (
    HEURISTIC_BORDERLINE,
    HEURISTIC_DEFINITE,
    HEURISTIC_NO,
    classify_pair,
)
from app.llm.embedder import cosine_similarity

if TYPE_CHECKING:
    from app.core.concepts.concept_store import Concept
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import MemoryStore
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.concept_contradiction")


_LOG_PREVIEW_CHARS = 160
_VERIFY_MAX_TOKENS = 80
_SNIPPET_MAX_CHARS = 200

# Mirrors F5's verifier, reframed for concept-vs-memory: A is the belief
# Aiko holds, B is a memory. YES only when the memory genuinely disproves
# the belief (strict), so a merely-related memory can't erode it.
_SYSTEM_PROMPT = (
    "You decide whether a MEMORY contradicts a stated BELIEF about a "
    "person. Answer with ONE JSON object on a single line and nothing "
    "else. Schema: {\"verdict\": \"YES\" | \"NO\" | \"UNRELATED\", "
    "\"reason\": \"<= 80 chars\"}. "
    "YES = the memory shows the belief is (now) false or reversed. "
    "NO = the memory is compatible with the belief (or supports it). "
    "UNRELATED = the memory is about a different topic. "
    "Be strict: prefer NO or UNRELATED when uncertain."
)

_USER_TEMPLATE = "BELIEF: {belief}\nMEMORY: {memory}"

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


@dataclass(slots=True)
class _LLMVerdict:
    verdict: str  # "YES" | "NO" | "UNRELATED"
    reason: str


@dataclass(slots=True)
class ContradictionVerdict:
    """A confirmed counter-evidence hit for one concept.

    ``reason`` is the heuristic signal or LLM reason; ``snippet`` is the
    memory content (trimmed) so the lifecycle event can quote *what*
    disproved the belief.
    """

    memory_id: int
    similarity: float
    heuristic_label: str
    llm_verdict: str | None
    reason: str
    snippet: str


class ConceptContradictionDetector:
    """Read-only detector: does any near memory disprove this concept?"""

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        ollama: "OllamaClient",
        chat_model: str,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event | None = None,
        similarity_min: float = 0.6,
        similarity_max: float = 0.95,
        max_candidates: int = 6,
    ) -> None:
        self._memory_store = memory_store
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._similarity_min = float(similarity_min)
        self._similarity_max = float(similarity_max)
        self._max_candidates = max(1, int(max_candidates))

    def detect(self, concept: "Concept") -> ContradictionVerdict | None:
        """Return the first confirmed contradiction for ``concept``, else
        ``None``. Does no writes; safe to call inside the L3 pass."""
        emb = getattr(concept, "embedding", None)
        if emb is None or int(getattr(emb, "size", 0)) == 0:
            return None
        try:
            hits = self._memory_store.search(
                emb,
                top_k=self._max_candidates,
                min_score=self._similarity_min,
            )
        except Exception:
            log.debug(
                "contradiction candidate search failed (id=%s)",
                getattr(concept, "concept_id", "?"),
                exc_info=True,
            )
            return None
        if not hits:
            return None
        belief_text = self._belief_text(concept)
        if not belief_text:
            return None
        for hit in hits:
            if self._cancelled():
                return None
            mem = hit.memory
            content = (getattr(mem, "content", "") or "").strip()
            if not content:
                continue
            # Recompute the raw cosine for band-filtering: MemoryStore.search
            # returns a salience-adjusted score, not the plain cosine.
            cos = float(cosine_similarity(emb, mem.embedding))
            if not (self._similarity_min <= cos < self._similarity_max):
                continue
            result = classify_pair(content, belief_text)
            if result.label == HEURISTIC_NO:
                continue
            signals = ",".join(result.signals) or result.label
            if result.label == HEURISTIC_DEFINITE:
                log.info(
                    "concept-contradiction definite: concept_id=%s mem_id=%s "
                    "sim=%.3f signals=%s belief=%r memory=%r",
                    getattr(concept, "concept_id", "?"),
                    getattr(mem, "id", "?"),
                    cos,
                    result.signals,
                    _preview(belief_text),
                    _preview(content),
                )
                return self._verdict(mem, cos, result.label, None, signals)
            # Borderline -- spend an LLM call if budget allows.
            if result.label != HEURISTIC_BORDERLINE:
                continue
            if not self._rate_limiter.allow():
                log.info(
                    "concept-contradiction borderline skip (rate-limited): "
                    "concept_id=%s mem_id=%s sim=%.3f",
                    getattr(concept, "concept_id", "?"),
                    getattr(mem, "id", "?"),
                    cos,
                )
                continue
            verdict = self._verify_with_llm(belief_text, content)
            if verdict is None or verdict.verdict != "YES":
                continue
            log.info(
                "concept-contradiction confirmed by LLM: concept_id=%s "
                "mem_id=%s sim=%.3f reason=%r",
                getattr(concept, "concept_id", "?"),
                getattr(mem, "id", "?"),
                cos,
                _preview(verdict.reason),
            )
            return self._verdict(
                mem, cos, result.label, verdict.verdict,
                verdict.reason or signals,
            )
        return None

    # ── helpers ──────────────────────────────────────────────────────

    @staticmethod
    def _belief_text(concept: "Concept") -> str:
        label = (getattr(concept, "label", "") or "").strip()
        rationale = (getattr(concept, "rationale", "") or "").strip()
        if label and rationale:
            return f"{label}. {rationale}"
        return label or rationale

    @staticmethod
    def _snippet(content: str) -> str:
        s = " ".join((content or "").split())
        if len(s) <= _SNIPPET_MAX_CHARS:
            return s
        return s[: _SNIPPET_MAX_CHARS - 1] + "\u2026"

    def _verdict(
        self,
        mem: object,
        similarity: float,
        heuristic_label: str,
        llm_verdict: str | None,
        reason: str,
    ) -> ContradictionVerdict:
        return ContradictionVerdict(
            memory_id=int(getattr(mem, "id", 0) or 0),
            similarity=float(similarity),
            heuristic_label=str(heuristic_label),
            llm_verdict=llm_verdict,
            reason=str(reason or "").strip(),
            snippet=self._snippet(getattr(mem, "content", "") or ""),
        )

    def _cancelled(self) -> bool:
        return self._cancel_event is not None and self._cancel_event.is_set()

    # ── LLM verifier (mirrors F5's) ──────────────────────────────────

    def _verify_with_llm(
        self, belief_text: str, memory_text: str
    ) -> _LLMVerdict | None:
        user_content = _USER_TEMPLATE.format(
            belief=belief_text or "", memory=memory_text or ""
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
                options={"num_predict": _VERIFY_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                think=True,
                surface="concept_contradiction",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("concept-contradiction verify call raised", exc_info=True)
            return None
        if self._cancelled():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return None
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "concept-contradiction verify raw: chars=%d elapsed_ms=%.0f "
                "preview=%r",
                len(raw),
                (time.monotonic() - t0) * 1000.0,
                _preview(raw),
            )
        return self._parse_verdict(raw)

    @staticmethod
    def _parse_verdict(raw: str) -> _LLMVerdict | None:
        match = _JSON_OBJECT_RE.search(raw or "")
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        if not isinstance(parsed, dict):
            return None
        verdict = str(parsed.get("verdict", "")).strip().upper()
        if verdict not in {"YES", "NO", "UNRELATED"}:
            return None
        reason = str(parsed.get("reason", "")).strip()
        if len(reason) > 200:
            reason = reason[:197] + "\u2026"
        return _LLMVerdict(verdict=verdict, reason=reason)


__all__ = ["ConceptContradictionDetector", "ContradictionVerdict"]
