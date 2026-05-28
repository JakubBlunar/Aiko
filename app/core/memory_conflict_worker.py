"""Memory conflict detector (F5 personality backlog).

Periodic background worker that scans pairs of memories with high
cosine similarity but lexically contradicting content (``loves X`` vs
``hates X``). Auto-demotes the loser when the F3 confidence delta is
clear; surfaces ambiguous pairs in the Conflicts sub-tab of the
Memory drawer for the user to resolve.

Pipeline (one tick = one ``run`` call):

1. Snapshot the in-memory mirror, filtered to the **allow-listed
   kinds** -- ``fact`` / ``preference`` / ``relationship`` / ``event``.
   Process / journal kinds (``knowledge_gap`` / ``curiosity_finding``
   / ``open_question`` / ``callback`` / ``reflection`` / ``promise``
   / ``shared_moment`` / ``self_tagged`` / ``self`` / ``catchphrase``)
   are excluded -- they aren't durable claims.
2. **All-pairs cosine** within the ``[similarity_min,
   similarity_max)`` band. Lower bound (default 0.80) excludes
   topically-distant pairs; upper bound (default 0.92) sits just under
   the dedupe threshold so paraphrases stay out. Pairs already in
   ``memory_conflicts`` (any status) are skipped -- one detection per
   pair, ever.
3. **Heuristic gate**
   (:mod:`app.core.conflict_heuristics`):
     - ``definite`` -> straight to resolve (no LLM call).
     - ``borderline`` -> queued for LLM verification.
     - ``no``        -> dropped.
4. **LLM verification** for borderlines, gated by
   :class:`app.core.fact_check_rate_limiter.FactCheckRateLimiter`
   with ``state_key='conflict_detector.rate_state'``. The prompt asks
   for a one-line JSON verdict (``YES`` / ``NO`` / ``UNRELATED``).
   Only ``YES`` proceeds to resolve.
5. **Resolve.** Compute ``delta = |conf_winner - conf_loser|``. When
   ``delta >= auto_resolve_delta`` (default 0.30), the
   higher-confidence side wins; the loser is demoted (confidence
   clamped to 0.20, tier -> ``archive``,
   ``metadata.superseded_by`` stamped) and the row is written with
   ``status='auto_resolved'``. Otherwise the row is written with
   ``status='open'`` for the UI to surface.

Per-tick caps (``max_corpus`` / ``max_pairs_per_run``) keep the
nested loop bounded so a runaway store doesn't blow a tick.

The worker doesn't move memories around; it works with
``MemoryStore.update`` (for demotions) and reads embeddings from the
in-memory mirror -- no SQLite writes inside the pair loop.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import numpy as np

from app.core.conflict_heuristics import (
    HEURISTIC_BORDERLINE,
    HEURISTIC_DEFINITE,
    HEURISTIC_NO,
    HeuristicResult,
    classify_pair,
)
from app.core.idle_worker import default_is_ready
from app.core.memory_conflict_store import (
    FLAGGED_BY_AUTO,
    MemoryConflictStore,
    STATUS_AUTO_RESOLVED,
    STATUS_OPEN,
)

if TYPE_CHECKING:
    from app.core.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory_store import Memory, MemoryStore
    from app.core.settings import AgentSettings, MemorySettings
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.memory_conflict_worker")


# ── allow-listed kinds ──────────────────────────────────────────────
# Durable-truth kinds. Process / journal kinds are intentionally
# excluded:
#   - knowledge_gap / curiosity_finding / open_question -> open
#     questions and their answers, not contradictions per se.
#   - reflection / callback / promise / shared_moment   -> narrative
#     records, not factual claims.
#   - self_tagged / self / catchphrase                  -> Aiko's own
#     persona/voice notes; treating those as F5-eligible would make
#     her flag her own self-image as "wrong".
_ALLOWED_KINDS: frozenset[str] = frozenset({
    "fact",
    "preference",
    "relationship",
    "event",
})


# Cap how much of any text we render in a single log line. Mirrors
# the F1 / G3 worker convention.
_LOG_PREVIEW_CHARS = 200


# Demote target -- the confidence we clamp the loser to. Matches the
# F1 fact-checker's contradict floor (``max(0.2, ...)``) so an
# F5-demoted memory looks the same in retrieval as an
# F1-fact-check-contradicted one.
_DEMOTE_CONFIDENCE = 0.20


# Cap on the LLM response so a malformed answer can't run away with
# the budget.
_VERIFY_MAX_TOKENS = 80


# Prompt template. The model returns one JSON object on a single line.
_SYSTEM_PROMPT = (
    "You decide if two short memory snippets contradict each other "
    "about the same topic. Answer with ONE JSON object on a single "
    "line and nothing else. Schema: {\"verdict\": \"YES\" | \"NO\" | "
    "\"UNRELATED\", \"reason\": \"<= 80 chars\"}. "
    "YES = the two snippets cannot both be true. "
    "NO = both can be true (no contradiction). "
    "UNRELATED = the snippets are about different topics. "
    "Be strict: prefer NO or UNRELATED when uncertain."
)

_USER_TEMPLATE = "A: {a}\nB: {b}"


_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _now_iso() -> str:
    return _utcnow().isoformat()


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
class _ResolutionPlan:
    """Decision the worker came to for a single confirmed pair."""

    auto_resolve: bool
    winner: "Memory"
    loser: "Memory"
    delta: float


class MemoryConflictWorker:
    """IdleWorker that finds and resolves contradicting memory pairs."""

    name = "memory_conflict_detector"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        conflict_store: MemoryConflictStore,
        ollama: "OllamaClient",
        chat_model: str,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        notify_memory_updated: Any | None = None,
        clock: Any | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._conflict_store = conflict_store
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._notify_memory_updated = notify_memory_updated
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "conflict_detector_interval_seconds",
                3600,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(
            getattr(self._agent_settings, "conflict_detector_enabled", True)
        ):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "conflict_detector_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        now = self._clock()
        sim_min = float(
            getattr(
                self._memory_settings,
                "conflict_detector_similarity_min",
                0.80,
            )
        )
        sim_max = float(
            getattr(
                self._memory_settings,
                "conflict_detector_similarity_max",
                0.92,
            )
        )
        max_corpus = int(
            getattr(
                self._memory_settings,
                "conflict_detector_max_corpus",
                1000,
            )
        )
        max_pairs = int(
            getattr(
                self._memory_settings,
                "conflict_detector_max_pairs_per_run",
                50,
            )
        )
        auto_resolve_delta = float(
            getattr(
                self._memory_settings,
                "conflict_detector_auto_resolve_delta",
                0.30,
            )
        )

        candidates = self._snapshot_candidates(max_corpus=max_corpus)
        log.info(
            "conflict-detector start: corpus_size=%d sim_band=[%.2f,%.2f) "
            "max_pairs=%d auto_resolve_delta=%.2f",
            len(candidates),
            sim_min,
            sim_max,
            max_pairs,
            auto_resolve_delta,
        )
        if len(candidates) < 2:
            return {
                "skipped": True,
                "reason": "corpus_too_small",
                "corpus_size": len(candidates),
            }

        pairs_scanned = 0
        pairs_skipped_existing = 0
        definite_count = 0
        borderline_consulted = 0
        borderline_skipped_rate_limit = 0
        borderline_dropped_by_llm = 0
        opened = 0
        auto_resolved = 0
        # Also track LLM-call timings so we can report them in the
        # summary line at the end.
        llm_total_ms = 0.0

        # Pre-compute embeddings as a single (n, d) matrix so we can
        # vectorise the cosine in NumPy. Each row is already
        # unit-normalised by ``MemoryStore`` on insert/update, so the
        # cosine is just the dot product.
        emb_matrix = np.asarray(
            [m.embedding for m in candidates], dtype=np.float32,
        )
        if emb_matrix.ndim != 2 or emb_matrix.shape[0] != len(candidates):
            log.warning(
                "conflict-detector: bad embedding matrix shape=%s; bailing",
                emb_matrix.shape,
            )
            return {"skipped": True, "reason": "bad_embeddings"}

        # We process pairs in row-major order with i < j, but we
        # short-circuit as soon as ``max_pairs`` borderline+definite
        # checks have been processed (heuristic-or-LLM work counts;
        # cheap drops don't).
        processed_pairs = 0
        for i, mem_a in enumerate(candidates):
            if self._cancel_event.is_set():
                log.info("conflict-detector cancelled mid-scan")
                return {"cancelled": True, "pairs_scanned": pairs_scanned}
            if processed_pairs >= max_pairs:
                break
            # Vectorised cosine of row i vs every j > i.
            sims = emb_matrix[i + 1:] @ emb_matrix[i]
            # Indices (in the j-suffix) where the cosine is in band.
            # ``np.nonzero`` returns a 1D tuple; take element 0.
            band_idx = np.nonzero((sims >= sim_min) & (sims < sim_max))[0]
            for offset_j in band_idx.tolist():
                if processed_pairs >= max_pairs:
                    break
                j = i + 1 + int(offset_j)
                mem_b = candidates[j]
                pairs_scanned += 1
                similarity = float(sims[offset_j])

                if self._conflict_store.has_pair(mem_a.id, mem_b.id):
                    pairs_skipped_existing += 1
                    continue

                heuristic = classify_pair(mem_a.content, mem_b.content)
                if heuristic.label == HEURISTIC_NO:
                    continue

                # From here on the pair counts toward the per-run cap
                # because we either spend an LLM call or commit a row.
                processed_pairs += 1

                if heuristic.label == HEURISTIC_DEFINITE:
                    definite_count += 1
                    log.info(
                        "conflict-detector definite: a_id=%s b_id=%s "
                        "sim=%.3f signals=%s a=%r b=%r",
                        mem_a.id,
                        mem_b.id,
                        similarity,
                        heuristic.signals,
                        _preview(mem_a.content),
                        _preview(mem_b.content),
                    )
                    plan = self._plan_resolution(
                        mem_a, mem_b, auto_resolve_delta,
                    )
                    if self._commit_pair(
                        mem_a=mem_a,
                        mem_b=mem_b,
                        similarity=similarity,
                        heuristic=heuristic,
                        llm_verdict=None,
                        llm_reason=None,
                        plan=plan,
                        now=now,
                    ):
                        if plan.auto_resolve:
                            auto_resolved += 1
                        else:
                            opened += 1
                    continue

                # Borderline -- needs an LLM check.
                if not self._rate_limiter.allow(now):
                    borderline_skipped_rate_limit += 1
                    log.info(
                        "conflict-detector borderline skip (rate-limited): "
                        "a_id=%s b_id=%s sim=%.3f",
                        mem_a.id,
                        mem_b.id,
                        similarity,
                    )
                    continue

                t0 = time.monotonic()
                verdict = self._verify_with_llm(mem_a.content, mem_b.content)
                elapsed_ms = (time.monotonic() - t0) * 1000.0
                llm_total_ms += elapsed_ms
                if verdict is None:
                    log.info(
                        "conflict-detector borderline LLM unparseable: "
                        "a_id=%s b_id=%s sim=%.3f elapsed_ms=%.0f",
                        mem_a.id,
                        mem_b.id,
                        similarity,
                        elapsed_ms,
                    )
                    borderline_dropped_by_llm += 1
                    continue
                borderline_consulted += 1
                log.info(
                    "conflict-detector borderline verdict=%s reason=%r "
                    "a_id=%s b_id=%s sim=%.3f elapsed_ms=%.0f",
                    verdict.verdict,
                    _preview(verdict.reason),
                    mem_a.id,
                    mem_b.id,
                    similarity,
                    elapsed_ms,
                )
                if verdict.verdict != "YES":
                    borderline_dropped_by_llm += 1
                    continue
                plan = self._plan_resolution(
                    mem_a, mem_b, auto_resolve_delta,
                )
                if self._commit_pair(
                    mem_a=mem_a,
                    mem_b=mem_b,
                    similarity=similarity,
                    heuristic=heuristic,
                    llm_verdict=verdict.verdict,
                    llm_reason=verdict.reason,
                    plan=plan,
                    now=now,
                ):
                    if plan.auto_resolve:
                        auto_resolved += 1
                    else:
                        opened += 1

        result = {
            "corpus_size": len(candidates),
            "pairs_scanned": pairs_scanned,
            "pairs_skipped_existing": pairs_skipped_existing,
            "definite": definite_count,
            "borderline_consulted": borderline_consulted,
            "borderline_skipped_rate_limit": borderline_skipped_rate_limit,
            "borderline_dropped_by_llm": borderline_dropped_by_llm,
            "opened": opened,
            "auto_resolved": auto_resolved,
            "llm_total_ms": round(llm_total_ms, 1),
        }
        log.info("conflict-detector done: %s", result)
        return result

    # ── corpus + plumbing ────────────────────────────────────────────

    def _snapshot_candidates(self, *, max_corpus: int) -> list["Memory"]:
        """Return at most ``max_corpus`` allow-listed memories, newest first."""
        out: list["Memory"] = []
        for kind in _ALLOWED_KINDS:
            try:
                out.extend(self._memory_store.iter_by_kind(kind))
            except Exception:
                log.debug(
                    "conflict-detector: iter_by_kind(%s) raised",
                    kind,
                    exc_info=True,
                )
        # Skip rows missing an embedding; the matrix step requires it.
        usable = [m for m in out if getattr(m, "embedding", None) is not None]
        # Skip empties (defensive -- MemoryStore.add already enforces
        # min content length, but a third-party migration could
        # introduce a blank).
        usable = [m for m in usable if (m.content or "").strip()]
        # Sort newest first so the per-run cap retains the freshest
        # contradictions; older-still-suspicious pairs catch up on
        # subsequent ticks.
        usable.sort(key=lambda m: m.created_at or "", reverse=True)
        return usable[: max(1, int(max_corpus))]

    def _plan_resolution(
        self,
        mem_a: "Memory",
        mem_b: "Memory",
        auto_resolve_delta: float,
    ) -> _ResolutionPlan:
        conf_a = float(getattr(mem_a, "confidence", 0.7))
        conf_b = float(getattr(mem_b, "confidence", 0.7))
        if conf_a > conf_b:
            winner, loser = mem_a, mem_b
        elif conf_b > conf_a:
            winner, loser = mem_b, mem_a
        else:
            # Tie: prefer the newer memory.
            newer = mem_a if (mem_a.created_at or "") >= (mem_b.created_at or "") else mem_b
            older = mem_b if newer is mem_a else mem_a
            winner, loser = newer, older
        delta = abs(conf_a - conf_b)
        return _ResolutionPlan(
            auto_resolve=delta >= auto_resolve_delta,
            winner=winner,
            loser=loser,
            delta=delta,
        )

    def _commit_pair(
        self,
        *,
        mem_a: "Memory",
        mem_b: "Memory",
        similarity: float,
        heuristic: HeuristicResult,
        llm_verdict: str | None,
        llm_reason: str | None,
        plan: _ResolutionPlan,
        now: datetime,
    ) -> bool:
        """Persist the conflict row + (optionally) demote the loser.

        Returns ``True`` if the row was actually written; ``False``
        if the store rejected it (e.g. duplicate -- harmless).
        """
        when_iso = now.isoformat() if isinstance(now, datetime) else _now_iso()
        if plan.auto_resolve:
            try:
                self._memory_store.update(
                    plan.loser.id,
                    confidence=_DEMOTE_CONFIDENCE,
                    tier="archive",
                    metadata={
                        "superseded_by": int(plan.winner.id),
                        "superseded_at": when_iso,
                        "superseded_reason": "conflict_detector",
                    },
                    metadata_merge=True,
                )
            except Exception:
                log.warning(
                    "conflict-detector demote failed: loser_id=%s",
                    plan.loser.id,
                    exc_info=True,
                )
                # Keep going -- record the pair as ``open`` so the UI
                # can still surface the contradiction even if the
                # demotion didn't stick.
                plan = _ResolutionPlan(
                    auto_resolve=False,
                    winner=plan.winner,
                    loser=plan.loser,
                    delta=plan.delta,
                )
            else:
                if self._notify_memory_updated is not None:
                    try:
                        self._notify_memory_updated(
                            {"memory_id": int(plan.loser.id)},
                        )
                    except Exception:
                        log.debug(
                            "conflict-detector: notify_memory_updated raised",
                            exc_info=True,
                        )
        try:
            pair_id = self._conflict_store.record(
                memory_a_id=mem_a.id,
                memory_b_id=mem_b.id,
                similarity=similarity,
                confidence_delta=plan.delta,
                heuristic_label=heuristic.label,
                heuristic_signals=list(heuristic.signals),
                llm_verdict=llm_verdict,
                llm_reason=llm_reason,
                status=STATUS_AUTO_RESOLVED if plan.auto_resolve else STATUS_OPEN,
                winner_id=int(plan.winner.id) if plan.auto_resolve else None,
                loser_id=int(plan.loser.id) if plan.auto_resolve else None,
                resolution_action="demote" if plan.auto_resolve else None,
                flagged_by=FLAGGED_BY_AUTO,
                detected_at=when_iso,
            )
        except Exception:
            log.warning(
                "conflict-detector: record raised for a_id=%s b_id=%s",
                mem_a.id,
                mem_b.id,
                exc_info=True,
            )
            return False
        if pair_id is None:
            return False
        log.info(
            "conflict-detector pair recorded: pair_id=%s status=%s "
            "winner_id=%s loser_id=%s delta=%.3f",
            pair_id,
            STATUS_AUTO_RESOLVED if plan.auto_resolve else STATUS_OPEN,
            int(plan.winner.id) if plan.auto_resolve else "-",
            int(plan.loser.id) if plan.auto_resolve else "-",
            plan.delta,
        )
        return True

    # ── LLM verifier ─────────────────────────────────────────────────

    def _verify_with_llm(
        self,
        text_a: str,
        text_b: str,
    ) -> _LLMVerdict | None:
        """Ask the local LLM whether the two snippets contradict.

        Returns ``None`` on parse / network failure (the caller treats
        a None as "drop the pair, don't burn another tick").
        """
        user_content = _USER_TEMPLATE.format(a=text_a or "", b=text_b or "")
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        if log.isEnabledFor(logging.DEBUG):
            log.debug(
                "conflict-detector verify prompt: model=%s prompt_chars=%d "
                "user_payload=%r",
                self._chat_model,
                len(user_content) + len(_SYSTEM_PROMPT),
                _preview(user_content),
            )
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _VERIFY_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="memory_conflict_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("conflict-detector verify call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return None
        log.debug(
            "conflict-detector verify raw: chars=%d preview=%r",
            len(raw),
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
