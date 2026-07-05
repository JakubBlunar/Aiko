"""Unified relevance budget selector.

One place that decides *what remembered context reaches Aiko's brain* on a
given turn. Historically three independent paths each surfaced their own
slice with a fixed cap and (mostly) no turn-relevance:

- memory RAG hits (``top_k`` by score, then a 30% token clip),
- topic clusters (top-N by *size*),
- concepts (top-N by *confidence*).

This selector replaces those independent caps with a single, shared,
context-window-relative token budget. Every candidate carries a
cross-source-comparable relevance (cosine to the live turn vector), an
estimated render token cost, and its native within-source order. The
selector:

0. admits any ``pinned`` candidates first — an always-on lane (e.g.
   high-confidence identity concepts) that bypasses ``min_relevance`` and
   the source ``cap`` so it enriches on top of the relevance picks,
1. drops anything below a per-source ``min_relevance`` threshold,
2. reserves each source's ``floor`` (a small guaranteed toehold, taken in
   native order) so a relevant turn never fully starves clusters/concepts,
3. fills the remaining budget greedily by *weighted* relevance
   (``relevance * weight``) up to each source's ``cap`` and the shared
   ``budget_tokens`` hard ceiling.

Every pass is clipped to ``budget_tokens``; pinned items consume the shared
budget like any other candidate.

The budget is *reserved before history is packed* by the caller, and on
overflow the caller squishes history first; ``degrade_level`` lets the
caller shrink surfacing gracefully as a last resort:

- ``0`` — normal: floors + greedy relevance fill.
- ``1`` — reduced: same shape, but the caller has already handed a smaller
  ``budget_tokens`` so the greedy fill naturally drops the weakest tail.
- ``2`` — floors only: skip the greedy remainder, keep just the guaranteed
  toehold (still clipped to ``budget_tokens``).

The selector is deliberately pure (no store / embed / IO): it takes
already-scored, already-costed candidates and returns a selection, so it is
cheap to unit-test in isolation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# Sources the budget spans this pass. Order here is the stable render order
# of the composed region (memories first, then the cluster line, then
# concept impressions) so the block reads the same way turn to turn.
SOURCES: tuple[str, ...] = ("memory", "cluster", "concept")


@dataclass(slots=True)
class SourceBudget:
    """Per-source allocation knobs.

    ``floor`` / ``cap`` are item *counts* (guaranteed minimum and hard
    maximum). ``weight`` scales relevance when ranking across sources (a
    higher weight biases the mix toward that source). ``min_relevance`` is
    the cosine floor below which a candidate is dropped as noise.
    """

    floor: int = 0
    cap: int = 6
    weight: float = 1.0
    min_relevance: float = 0.0


@dataclass(slots=True)
class ContextCandidate:
    """One selectable piece of remembered context, pre-scored + pre-costed.

    ``pinned`` marks an always-on candidate (e.g. a high-confidence identity
    concept describing how Aiko wants to behave): it is admitted before the
    relevance passes, bypasses ``min_relevance``, and does **not** count
    against the source ``cap`` — so it enriches the mix *in addition to* the
    turn-relevant picks rather than crowding them out. It still consumes the
    shared token budget like any other candidate.
    """

    source: str
    relevance: float
    tokens: int
    order: int
    payload: Any
    key: str = ""
    pinned: bool = False

    @property
    def weighted(self) -> float:
        return float(self.relevance)


@dataclass(slots=True)
class SourceSelection:
    """What the selector picked (and dropped) for one source."""

    chosen: list[ContextCandidate] = field(default_factory=list)
    tokens: int = 0
    pinned: int = 0
    dropped_for_relevance: int = 0
    dropped_for_budget: int = 0
    dropped_for_cap: int = 0
    top_relevance: float = 0.0

    @property
    def count(self) -> int:
        return len(self.chosen)

    def payloads(self) -> list[Any]:
        return [c.payload for c in self.chosen]


@dataclass(slots=True)
class ContextSelection:
    """The full cross-source selection + accounting for telemetry."""

    by_source: dict[str, SourceSelection]
    budget_tokens: int = 0
    used_tokens: int = 0
    degrade_level: int = 0

    def source(self, name: str) -> SourceSelection:
        return self.by_source.get(name, SourceSelection())

    def payloads(self, name: str) -> list[Any]:
        return self.source(name).payloads()

    @property
    def total_chosen(self) -> int:
        return sum(s.count for s in self.by_source.values())

    def as_dict(self) -> dict[str, Any]:
        """Compact, JSON-safe breakdown for the prompt telemetry drawer."""
        return {
            "budget_tokens": int(self.budget_tokens),
            "used_tokens": int(self.used_tokens),
            "degrade_level": int(self.degrade_level),
            "sources": {
                name: {
                    "chosen": sel.count,
                    "pinned": int(sel.pinned),
                    "tokens": int(sel.tokens),
                    "top_relevance": round(float(sel.top_relevance), 4),
                    "dropped_for_relevance": int(sel.dropped_for_relevance),
                    "dropped_for_budget": int(sel.dropped_for_budget),
                    "dropped_for_cap": int(sel.dropped_for_cap),
                }
                for name, sel in self.by_source.items()
            },
        }


@dataclass(slots=True)
class RelevantContext:
    """The rendered ``relevant_context`` region plus its accounting.

    Returned by the region builder (owned by the session controller, which
    has the stores + hedging helpers) and consumed by the prompt assembler,
    which inserts ``text`` at T3 and folds ``selection`` / ``concept_trace``
    into :class:`PromptTelemetry`.
    """

    text: str = ""
    selection: ContextSelection | None = None
    concept_trace: dict = field(default_factory=lambda: {"surfaced": [], "reason": "disabled"})
    reason: str = "ok"
    # Whether the memory candidate pool + shared turn embedding were reused
    # from the speculative :class:`RagPrefetcher` cache ("hit"), computed
    # fresh on the hot path with a warm prefetcher ("miss"), or the
    # prefetcher was absent ("skip"). Surfaced into PromptTelemetry.
    prefetch_event: str = "skip"


class ContextBudgetSelector:
    """Allocate a shared token budget across memory / cluster / concept
    candidates with per-source floors, caps, weights and relevance."""

    def __init__(self, budgets: dict[str, SourceBudget]) -> None:
        # Defensive copy + fill any missing source with an inert default so
        # callers can pass a partial map.
        self._budgets: dict[str, SourceBudget] = {}
        for name in SOURCES:
            self._budgets[name] = budgets.get(name, SourceBudget())

    def budget_for(self, source: str) -> SourceBudget:
        return self._budgets.get(source, SourceBudget())

    def select(
        self,
        candidates_by_source: dict[str, list[ContextCandidate]],
        *,
        budget_tokens: int,
        degrade_level: int = 0,
    ) -> ContextSelection:
        budget_tokens = max(0, int(budget_tokens))
        degrade_level = max(0, min(2, int(degrade_level)))
        by_source: dict[str, SourceSelection] = {
            name: SourceSelection() for name in SOURCES
        }
        used = 0

        # Pre-filter each source by min_relevance and stable native order.
        # Pinned candidates bypass the relevance floor (they are surfaced for
        # *what they are*, not their cosine to this turn).
        pools: dict[str, list[ContextCandidate]] = {}
        for name in SOURCES:
            cfg = self._budgets[name]
            raw = list(candidates_by_source.get(name, []))
            kept: list[ContextCandidate] = []
            for cand in raw:
                if not cand.pinned and float(cand.relevance) < float(cfg.min_relevance):
                    by_source[name].dropped_for_relevance += 1
                    continue
                kept.append(cand)
            kept.sort(key=lambda c: c.order)
            if kept:
                by_source[name].top_relevance = max(
                    float(c.relevance) for c in kept
                )
            pools[name] = kept

        # Track which candidates are still available to the greedy fill and
        # how many *non-pinned* items each source has admitted (the cap only
        # governs the relevance picks; pinned items enrich on top).
        admitted_keys: set[int] = set()
        flex_count: dict[str, int] = {name: 0 for name in SOURCES}

        def _admit(name: str, cand: ContextCandidate) -> bool:
            nonlocal used
            if used + cand.tokens > budget_tokens:
                by_source[name].dropped_for_budget += 1
                return False
            by_source[name].chosen.append(cand)
            by_source[name].tokens += cand.tokens
            used += cand.tokens
            admitted_keys.add(id(cand))
            if cand.pinned:
                by_source[name].pinned += 1
            else:
                flex_count[name] += 1
            return True

        # (0) Pinned always-on lane: admit these first, in native order,
        #     exempt from cap + min_relevance (still budget-clipped).
        for name in SOURCES:
            for cand in pools[name]:
                if cand.pinned:
                    _admit(name, cand)

        # (1) Floors: reserve each source's guaranteed toehold in native
        #     order, still clipped to the hard ceiling so we never exceed it.
        for name in SOURCES:
            cfg = self._budgets[name]
            floor = max(0, min(int(cfg.floor), int(cfg.cap)))
            taken = 0
            for cand in pools[name]:
                if id(cand) in admitted_keys:
                    continue
                if taken >= floor:
                    break
                if _admit(name, cand):
                    taken += 1

        # (2) Greedy remainder fill by weighted relevance, unless we've been
        #     told to keep floors only (degrade_level 2).
        if degrade_level < 2:
            remainder: list[ContextCandidate] = []
            for name in SOURCES:
                for cand in pools[name]:
                    if id(cand) in admitted_keys:
                        continue
                    remainder.append(cand)
            # Sort by weighted relevance desc; ties broken by native order.
            remainder.sort(
                key=lambda c: (
                    -(float(c.relevance) * float(self._budgets[c.source].weight)),
                    c.order,
                )
            )
            for cand in remainder:
                name = cand.source
                cfg = self._budgets[name]
                if flex_count[name] >= int(cfg.cap):
                    by_source[name].dropped_for_cap += 1
                    continue
                _admit(name, cand)

        return ContextSelection(
            by_source=by_source,
            budget_tokens=budget_tokens,
            used_tokens=used,
            degrade_level=degrade_level,
        )


__all__ = [
    "SOURCES",
    "SourceBudget",
    "ContextCandidate",
    "SourceSelection",
    "ContextSelection",
    "RelevantContext",
    "ContextBudgetSelector",
]
