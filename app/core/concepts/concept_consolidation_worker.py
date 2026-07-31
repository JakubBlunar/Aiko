"""L2 near-duplicate consolidation: fuse paraphrase-twin concepts.

Creation-time dedup (the synthesis worker's ``reinforces_id`` path plus its
``_DEDUPE_COS`` label-embedding guard) stops anything at / above the dedup
cosine from ever becoming two rows. But paraphrase twins that land just
*below* that bar -- or that formed when the twin wasn't in the proposer's
shown list -- accumulate with no retroactive fix, so the ``active`` set
slowly fills with restatements of the same belief (heaviest in
``identity/user``). This worker is that retroactive fix.

**Shape.** An idle-scheduler worker on its own interval. Each tick pulls a
small rolling batch of the stalest ``active`` concepts, and for each finds
its nearest *same-``(subject, kind)``* active neighbour via the store's one
cosine primitive. A pair whose cosine clears ``concept_consolidation_merge_cosine``
is a *candidate* -- not a decision: pure cosine can't tell a true paraphrase
from a template collision ("X energizes him" vs "Y energizes him"), so a
maintenance-tier LLM adjudicates whether the two are genuinely the same
belief. Only on a ``same`` verdict does it merge (via
:meth:`ConceptStore.merge_into`, which folds the weaker row's evidence into
the stronger and deletes it).

**Bounded + safe.** LLM adjudications are capped per hour / day by a
dedicated :class:`FactCheckRateLimiter` (its own ``state_key`` so it never
shares budget with L9 / L15 / F5), and an in-memory negative cache keeps a
rejected pair from being re-adjudicated every tick. The worker never mutates
``confidence`` / ``plasticity`` / ``status`` -- it always keeps the stronger
row as canonical, so the single-writer L3 lifecycle engine stays the only
writer of those fields.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

from app.core.concepts.concept_event_store import ConceptEvent
from app.core.infra import timephrase
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_store import Concept, ConceptStore
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter

log = logging.getLogger("app.concept_consolidation_worker")

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_TEMPERATURE = 0.0
_MAX_TOKENS = 200
# How long a rejected ("not the same belief") pair stays cached so the
# LLM budget isn't re-spent re-asking about it every tick.
_NEG_CACHE_TTL_SECONDS = 6 * 3600.0
# Cap on the negative cache so a long-running process can't grow it
# unbounded; oldest entries are evicted first past this size.
_NEG_CACHE_MAX = 2000


class ConceptConsolidationWorker:
    """IdleWorker: LLM-adjudicated near-duplicate concept fusion."""

    name = "concept_consolidation"

    def __init__(
        self,
        *,
        concept_store: "ConceptStore",
        memory_settings: Any,
        agent_settings: Any,
        ollama: Any,
        chat_model: str | None = None,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: Any = None,
        concept_event_store: "ConceptEventStore | None" = None,
        graph_mature_provider: Callable[[], bool] | None = None,
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = concept_store
        self._memory_settings = memory_settings
        self._agent_settings = agent_settings
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._events = concept_event_store
        self._graph_mature_provider = graph_mature_provider
        self._user_name_provider = user_display_name_provider
        self._assistant_name_provider = assistant_display_name_provider
        self._clock = clock or timephrase.utcnow
        # pair (frozenset of two ids) -> expiry datetime.
        self._negative_cache: dict[frozenset[int], datetime] = {}

    # ── idle worker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "concept_consolidation_interval_seconds",
                900,
            )
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> bool:
        if not self._enabled():
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at
        )

    def _enabled(self) -> bool:
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return False
        return bool(
            getattr(
                self._memory_settings,
                "concept_consolidation_enabled",
                True,
            )
        )

    def _graph_mature(self) -> bool:
        """Only consolidate once the topic graph has cleared the L21
        maturity floor (same gate the L2/L3 workers use). No provider
        wired => treat as mature so lean / test deployments still run."""
        provider = self._graph_mature_provider
        if provider is None:
            return True
        try:
            return bool(provider())
        except Exception:
            log.debug("graph_mature_provider raised", exc_info=True)
            return True

    # ── config knobs ──────────────────────────────────────────────────

    def _i(self, name: str, default: int) -> int:
        return int(getattr(self._memory_settings, name, default))

    def _fl(self, name: str, default: float) -> float:
        return float(getattr(self._memory_settings, name, default))

    # ── run ────────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        if not self._graph_mature():
            return {"skipped": True, "reason": "immature_graph"}

        now = self._clock()
        self._evict_expired(now)

        batch_size = max(1, self._i("concept_consolidation_batch_size", 40))
        merge_cos = self._fl("concept_consolidation_merge_cosine", 0.84)
        batch = self._store.list_stalest(batch_size)

        stats: dict[str, Any] = {
            "scanned": 0,
            "pairs_considered": 0,
            "adjudicated": 0,
            "merged": 0,
        }

        pairs = self._collect_pairs(batch, merge_cos, stats)
        # Highest-cosine (most-likely-dup) pairs first, so a tight LLM
        # budget is spent on the strongest candidates.
        pairs.sort(key=lambda p: p[0], reverse=True)

        for cos, a, b in pairs:
            key = frozenset({a.concept_id, b.concept_id})
            if key in self._negative_cache:
                continue
            # The LLM adjudication is the real work unit -> spend a token.
            if not self._rate_limiter.allow(now):
                break
            stats["adjudicated"] += 1
            same, reason = self._adjudicate(a, b)
            if not same:
                self._remember_rejection(key, now)
                continue
            if self._merge(a, b, cos, reason):
                stats["merged"] += 1

        return stats

    # ── candidate generation ──────────────────────────────────────────

    def _collect_pairs(
        self, batch: list["Concept"], merge_cos: float, stats: dict[str, Any]
    ) -> list[tuple[float, "Concept", "Concept"]]:
        seen: set[frozenset[int]] = set()
        pairs: list[tuple[float, "Concept", "Concept"]] = []
        for seed in batch:
            if seed.status != "active":
                continue
            vec = getattr(seed, "embedding", None)
            if vec is None or getattr(vec, "size", 0) == 0:
                continue
            stats["scanned"] += 1
            neighbours = self._store.nearest(
                vec,
                subject=seed.subject,
                kind=seed.kind,
                status="active",
                k=3,
            )
            for cand, cos in neighbours:
                if cand.concept_id == seed.concept_id:
                    continue
                # ``nearest`` is cosine-descending: the first non-self
                # neighbour is the top one. Below the bar => nothing to do.
                if cos < merge_cos:
                    break
                key = frozenset({seed.concept_id, cand.concept_id})
                if key in seen:
                    break
                seen.add(key)
                pairs.append((float(cos), seed, cand))
                # One (the strongest) candidate pair per seed.
                break
        stats["pairs_considered"] = len(pairs)
        return pairs

    # ── negative cache ─────────────────────────────────────────────────

    def _evict_expired(self, now: datetime) -> None:
        expired = [k for k, exp in self._negative_cache.items() if exp <= now]
        for k in expired:
            self._negative_cache.pop(k, None)
        # Hard cap: drop the soonest-to-expire entries if oversized.
        if len(self._negative_cache) > _NEG_CACHE_MAX:
            for k, _exp in sorted(
                self._negative_cache.items(), key=lambda kv: kv[1]
            )[: len(self._negative_cache) - _NEG_CACHE_MAX]:
                self._negative_cache.pop(k, None)

    def _remember_rejection(self, key: frozenset[int], now: datetime) -> None:
        self._negative_cache[key] = now + timedelta(
            seconds=_NEG_CACHE_TTL_SECONDS
        )

    # ── merge ──────────────────────────────────────────────────────────

    def _merge(
        self, a: "Concept", b: "Concept", cos: float, reason: str
    ) -> bool:
        canonical, absorbed = self._pick_canonical(a, b)
        absorbed_id = absorbed.concept_id
        absorbed_label = absorbed.label
        ok = self._store.merge_into(
            canonical_id=canonical.concept_id,
            absorbed_id=absorbed_id,
        )
        if not ok:
            return False
        log.info(
            "concept consolidation: merged #%s %r into #%s %r (cos=%.3f)",
            absorbed_id,
            absorbed_label,
            canonical.concept_id,
            canonical.label,
            cos,
        )
        self._emit_event(canonical.concept_id, absorbed_id, absorbed_label,
                         cos, reason)
        return True

    @staticmethod
    def _pick_canonical(
        a: "Concept", b: "Concept"
    ) -> tuple["Concept", "Concept"]:
        """Stronger row (confidence, then evidence breadth, then id for a
        stable tiebreak) survives as canonical; the other is absorbed."""
        def strength(c: "Concept") -> tuple:
            return (
                float(c.confidence),
                int(c.distinct_source_count),
                int(c.evidence_count),
                int(c.concept_id),
            )

        return (a, b) if strength(a) >= strength(b) else (b, a)

    def _emit_event(
        self,
        canonical_id: int,
        absorbed_id: int,
        absorbed_label: str,
        cos: float,
        reason: str,
    ) -> None:
        if self._events is None:
            return
        canonical = self._store.get(canonical_id)
        if canonical is None:
            return
        detail = (
            f"Merged #{absorbed_id} '{absorbed_label}' into "
            f"#{canonical_id} '{canonical.label}'"
        )
        if reason:
            detail = f"{detail} -- {reason}"
        try:
            self._events.add(
                ConceptEvent(
                    event_type="merged",
                    kind=canonical.kind,
                    subject=canonical.subject,
                    label=canonical.label,
                    confidence=float(canonical.confidence),
                    novelty=max(0.0, 1.0 - float(cos)),
                    evidence_count=int(canonical.evidence_count),
                    distinct_source_count=int(
                        canonical.distinct_source_count
                    ),
                    reason=detail,
                    concept_id=canonical_id,
                )
            )
        except Exception:
            log.debug("concept merge event insert failed", exc_info=True)

    # ── LLM adjudication ───────────────────────────────────────────────

    def _subject_word(self, subject: str) -> str:
        if subject == "aiko":
            name = None
            if self._assistant_name_provider is not None:
                try:
                    name = self._assistant_name_provider()
                except Exception:
                    name = None
            return name or "Aiko"
        name = None
        if self._user_name_provider is not None:
            try:
                name = self._user_name_provider()
            except Exception:
                name = None
        return name or "the user"

    def _adjudicate(self, a: "Concept", b: "Concept") -> tuple[bool, str]:
        subject_word = self._subject_word(a.subject)
        kind = a.kind.replace("_", " ")
        system = (
            "You are a precise annotator. Two short belief statements about "
            f"{subject_word} are given. Decide whether they express the SAME "
            f"underlying {kind}: one is a paraphrase, restatement, or a "
            "subset of the other and they should be merged into a single "
            "belief. Two statements that merely share a sentence template or "
            "a broad theme while asserting DIFFERENT specifics are NOT the "
            "same. When uncertain, answer false. "
            'Return JSON only: {"same": boolean, "reason": string}.'
        )
        user = (
            f"Statement A: {a.label}\n"
            f"{self._rationale_line(a)}"
            f"Statement B: {b.label}\n"
            f"{self._rationale_line(b)}"
            f"\nAre A and B the same {kind}?"
        )
        parsed = self._call_llm(system, user)
        if not isinstance(parsed, dict):
            return (False, "")
        same = bool(parsed.get("same") is True)
        reason = str(parsed.get("reason") or "").strip()
        if len(reason) > 200:
            reason = reason[:199] + "\u2026"
        return (same, reason)

    @staticmethod
    def _rationale_line(c: "Concept") -> str:
        rationale = (getattr(c, "rationale", "") or "").strip()
        if not rationale:
            return ""
        if len(rationale) > 200:
            rationale = rationale[:199] + "\u2026"
        return f"(rationale: {rationale})\n"

    def _call_llm(self, system: str, user: str) -> dict[str, Any] | None:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={
                    "num_predict": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="concept_consolidation_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning(
                "concept consolidation LLM call failed", exc_info=True
            )
            return None
        return self._parse("".join(chunks))

    @staticmethod
    def _parse(raw: str) -> dict[str, Any] | None:
        match = _JSON_OBJECT_RE.search(raw or "")
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None


__all__ = ["ConceptConsolidationWorker"]
