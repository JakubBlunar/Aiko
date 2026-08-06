"""L17 drift worker: apply relabels, then record what actually changed.

Three jobs, in this order, once per tick:

1. **Relabel.** The synthesis worker stages a ``relabel_proposed`` event
   whenever a proposal folds into an existing concept but says it
   differently. This worker is the **single writer of ``label`` and
   ``rationale``** -- the direct counterpart to the L3 engine being the
   single writer of ``confidence`` / ``plasticity`` / ``status`` -- and
   it is where the anti-churn gates and the adjudication budget live.
2. **Classify.** Feed the moved concepts through the pure L17b
   classifier and persist the salient results as L17c learning events,
   so the history of *why* a belief changed outlives the belief.
3. **Sweep.** A one-time cold-start backfill. The forward pass is
   watermark-driven and the watermark jumps to the newest event id
   whether or not every moved concept fitted in the page, so on a store
   that already had months of history before this worker existed the
   first tick would classify the lowest page of concept ids and silently
   mark all the rest as accounted for. The sweep walks the concept id
   space once, on its own cursor, with ``since_event_id=0`` so historical
   decisive events still qualify. It runs *last* so the backfill never
   starves the forward pass, and it never publishes a pending reflection:
   Aiko should not boot up and start voicing five-week-old realisations.

Two hard constraints shape the implementation:

- ``demand()`` reads **one indexed aggregate** and nothing else. It does
  no NumPy, no graph walk, and no store scan. The consolidation worker
  learned this the hard way: its ``demand()`` probe restacked a cosine
  matrix every fifteen minutes and eventually took the process down with
  a native access violation.
- ``run()`` stacks vectors **once** via
  :meth:`ConceptStore.matrix_snapshot` and does a **single matmul** for
  the whole succession pass. It never calls ``nearest()`` in a loop,
  because the cross-status queries succession needs are exactly the ones
  that bypass the store's cached matrix.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import replace
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Callable

import numpy as np

from app.core.concepts.concept_drift import (
    ConceptTrace,
    DriftFinding,
    DriftThresholds,
    SuccessionCandidate,
    TrajectoryPoint,
    detect_drift,
    is_material_relabel,
    normalize_label,
)
from app.core.concepts.concept_event_store import ConceptEvent
from app.core.concepts.concept_learning_event_store import LearningEvent
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal, pressure_from_count

if TYPE_CHECKING:
    from app.core.concepts.concept_event_store import ConceptEventStore
    from app.core.concepts.concept_learning_event_store import (
        ConceptLearningEventStore,
    )
    from app.core.concepts.concept_store import Concept, ConceptStore
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter

log = logging.getLogger("app.concept_drift_worker")

# KV keys. The watermark is the newest ``concept_events`` id this worker
# has already accounted for; the snapshot is the bounded pending-reflection
# payload the T6 provider reads on the turn path (it must never scan); the
# sweep cursor is the highest concept id the cold-start backfill has walked.
DRIFT_WATERMARK_KEY = "concept.drift.watermark"
DRIFT_PENDING_KEY = "concept.drift.pending"
DRIFT_SWEEP_KEY = "concept.drift.sweep_cursor"

# Sentinel for a finished sweep. A cursor can only ever rise through real
# concept ids, so a negative value is unambiguous and needs no second key.
SWEEP_DONE = -1

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
_TEMPERATURE = 0.0
_MAX_TOKENS = 200
# Rejected relabels stay cached so the adjudication budget is not re-spent
# re-asking about the same wording every tick.
_NEG_CACHE_TTL_SECONDS = 12 * 3600.0
_NEG_CACHE_MAX = 500
# Statuses a belief can fade into, and the one it rises into.
_FADED = ("dormant", "retired")


def _utcnow() -> datetime:
    return timephrase.utcnow()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timephrase.utcnow().tzinfo)
    return parsed


class ConceptDriftWorker:
    """IdleWorker: relabel beliefs in place, record how they evolved."""

    name = "concept_drift"

    def __init__(
        self,
        *,
        concept_store: "ConceptStore",
        concept_event_store: "ConceptEventStore",
        learning_store: "ConceptLearningEventStore",
        memory_settings: Any,
        agent_settings: Any,
        embedder: Any = None,
        ollama: Any = None,
        chat_model: str | None = None,
        rate_limiter: "FactCheckRateLimiter | None" = None,
        cancel_event: Any = None,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        evidence_labels_provider: (
            Callable[[int], list[str]] | None
        ) = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._store = concept_store
        self._events = concept_event_store
        self._learning = learning_store
        self._memory_settings = memory_settings
        self._agent_settings = agent_settings
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._evidence_labels = evidence_labels_provider
        self._clock = clock or _utcnow
        self._negative_cache: dict[tuple[int, str], datetime] = {}

    # ── settings helpers ──────────────────────────────────────────────

    def _i(self, name: str, default: int) -> int:
        try:
            return int(getattr(self._memory_settings, name, default))
        except (TypeError, ValueError):
            return default

    def _fl(self, name: str, default: float) -> float:
        try:
            return float(getattr(self._memory_settings, name, default))
        except (TypeError, ValueError):
            return default

    def _b(self, name: str, default: bool) -> bool:
        return bool(getattr(self._memory_settings, name, default))

    def _enabled(self) -> bool:
        if not bool(getattr(self._agent_settings, "concepts_enabled", False)):
            return False
        return self._b("concept_drift_enabled", True)

    # ── idle worker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(self._i("concept_drift_interval_seconds", 3600))

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> bool:
        # Feature flag only; the interval is the heartbeat (P36).
        return self._enabled()

    def demand(
        self, *, now: datetime, last_run_at: datetime | None
    ) -> "WorkSignal | None":
        """Has anything happened on the timeline since the last run?

        One indexed ``MAX(id)`` and one KV read. Deliberately nothing
        else: the succession pass needs cross-status cosine work, and
        doing that here -- on every scheduler tick, for a probe -- is the
        pattern that crashed the consolidation worker.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        try:
            latest = self._events.max_event_id()
        except Exception:
            log.debug("drift demand probe failed", exc_info=True)
            return WorkSignal(pressure=0.0, reason="probe_failed")
        watermark = self._watermark()
        pending = max(0, latest - watermark)
        # An empty timeline has no history to sweep, so a fresh install
        # must not report backfill pressure on every tick.
        sweeping = latest > 0 and self._sweep_pending()
        if pending <= 0:
            if not sweeping:
                return WorkSignal(pressure=0.0, reason="no new events")
            # The backfill is real work but never urgent work: hold it at
            # the floor so a live worker always outranks it.
            return WorkSignal(
                pressure=pressure_from_count(1, saturation=25),
                reason="history sweep in progress",
                needs_llm=False,
            )
        reason = f"{pending} new concept events"
        if sweeping:
            reason += " + history sweep"
        return WorkSignal(
            pressure=pressure_from_count(pending, saturation=25),
            reason=reason,
            # Only the relabel adjudication spends the LLM, and only when
            # a proposal is actually waiting.
            needs_llm=self._has_relabel_candidates(watermark),
        )

    def _watermark(self) -> int:
        if self._kv_get is None:
            return 0
        try:
            raw = self._kv_get(DRIFT_WATERMARK_KEY)
            return int(raw) if raw else 0
        except (TypeError, ValueError):
            return 0

    def _set_watermark(self, value: int) -> None:
        if self._kv_set is None:
            return
        try:
            self._kv_set(DRIFT_WATERMARK_KEY, str(int(value)))
        except Exception:
            log.debug("drift watermark write failed", exc_info=True)

    def _sweep_cursor(self) -> int:
        """Highest concept id the cold-start sweep has already walked.

        Reports :data:`SWEEP_DONE` when there is no KV to remember the
        cursor in, because a sweep that cannot persist its position would
        re-read page one on every tick forever.
        """
        if self._kv_get is None:
            return SWEEP_DONE
        try:
            raw = self._kv_get(DRIFT_SWEEP_KEY)
        except Exception:
            return SWEEP_DONE
        if raw is None or str(raw) == "":
            return 0
        try:
            return int(raw)
        except (TypeError, ValueError):
            return 0

    def _set_sweep_cursor(self, value: int) -> None:
        if self._kv_set is None:
            return
        try:
            self._kv_set(DRIFT_SWEEP_KEY, str(int(value)))
        except Exception:
            log.debug("drift sweep cursor write failed", exc_info=True)

    def _sweep_pending(self) -> bool:
        """One KV read -- safe to call from ``demand()``."""
        if not self._b("concept_drift_sweep_enabled", True):
            return False
        return self._sweep_cursor() >= 0

    def _has_relabel_candidates(self, watermark: int) -> bool:
        if not self._b("concept_relabel_enabled", True):
            return False
        try:
            rows = self._events.list(
                event_type="relabel_proposed", limit=1
            )
        except Exception:
            return False
        return bool(rows) and int(rows[0].event_id) > int(watermark)

    # ── run ───────────────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        now = self._clock()
        self._evict_expired(now)
        watermark = self._watermark()
        try:
            latest = self._events.max_event_id()
        except Exception:
            log.warning("drift run: timeline unreadable", exc_info=True)
            return {"skipped": True, "reason": "timeline_unreadable"}
        sweeping = latest > 0 and self._sweep_pending()
        if latest <= watermark and not sweeping:
            return {"skipped": True, "reason": "no new events"}

        stats: dict[str, Any] = {
            "relabel_considered": 0,
            "relabel_applied": 0,
            "relabel_rejected": 0,
            "traces": 0,
            "succession_pairs": 0,
            "findings": 0,
            "recorded": 0,
        }

        if latest > watermark:
            # 1. Relabels first, so the ``relabeled`` events they write are
            #    visible to the classification pass below in the same tick.
            if self._b("concept_relabel_enabled", True):
                self._run_relabel_pass(watermark, now, stats)

            # 2. Classify what moved and persist the salient results.
            self._run_classification_pass(watermark, now, stats)

            # 3. Advance the watermark only after both passes succeeded, so
            #    a crash mid-run re-reads the same window rather than losing
            #    it. Re-read the ceiling: the relabel pass appended events.
            try:
                self._set_watermark(self._events.max_event_id())
            except Exception:
                self._set_watermark(latest)

        # 4. One page of the cold-start backfill, last so it can only ever
        #    use the budget the live passes left behind.
        if sweeping:
            self._run_sweep_pass(now, stats)
        return stats

    # ── pass 1: relabel ───────────────────────────────────────────────

    def _run_relabel_pass(
        self, watermark: int, now: datetime, stats: dict[str, Any]
    ) -> None:
        try:
            proposals = [
                event
                for event in self._events.list(
                    event_type="relabel_proposed",
                    limit=max(1, self._i("concept_relabel_scan_limit", 40)),
                )
                if int(event.event_id) > int(watermark)
            ]
        except Exception:
            log.debug("relabel proposal read failed", exc_info=True)
            return
        if not proposals:
            return

        # Newest proposal per concept wins: an older wording for the same
        # belief is already superseded by the time we get here.
        newest: dict[int, ConceptEvent] = {}
        for event in proposals:
            cid = int(event.concept_id or 0)
            if cid <= 0:
                continue
            if cid not in newest:
                newest[cid] = event

        cap = max(1, self._i("concept_relabel_max_per_run", 3))
        applied = 0
        for cid, event in newest.items():
            if applied >= cap:
                break
            stats["relabel_considered"] += 1
            if self._try_relabel(cid, event, now):
                applied += 1
                stats["relabel_applied"] += 1
            else:
                stats["relabel_rejected"] += 1

    def _try_relabel(
        self, concept_id: int, proposal: ConceptEvent, now: datetime
    ) -> bool:
        concept = self._store.get(concept_id)
        if concept is None or concept.status == "retired":
            return False
        proposed = str(proposal.label or "").strip()
        if not proposed:
            return False

        key = (concept_id, normalize_label(proposed))
        if key in self._negative_cache:
            return False

        # Cheap gates first -- none of these may cost an embed or a call.
        if not is_material_relabel(
            concept.label,
            proposed,
            min_token_delta=self._i("concept_drift_relabel_min_tokens", 1),
        ):
            return False
        if self._previously_held(concept_id, proposed):
            # History is its own thrash guard: a wording this belief has
            # already worn is a ping-pong, not a refinement.
            self._remember_rejection(key, now)
            return False
        if not self._cooldown_clear(concept_id, now):
            return False

        # The new wording must still *be* this belief. Below the dedupe
        # bar it is a different claim, and turning it into a rewrite would
        # silently overwrite one belief with another.
        vector = self._embed(proposed)
        if vector is None:
            return False
        similarity = self._cosine(concept.embedding, vector)
        floor = self._fl("concept_relabel_min_cosine", 0.80)
        if similarity < floor:
            self._remember_rejection(key, now)
            return False

        if not self._adjudicate(concept, proposed, proposal, now):
            self._remember_rejection(key, now)
            return False

        return self._apply_relabel(concept, proposed, proposal, vector, now)

    def _previously_held(self, concept_id: int, proposed: str) -> bool:
        """Has this belief already worn this wording at some point?

        Read straight off the label snapshots the timeline already keeps,
        which is why the anti-thrash guard costs nothing to maintain.
        """
        wanted = normalize_label(proposed)
        try:
            history = self._events.drift_window(
                concept_id, anchor=40, recent=60
            )
        except Exception:
            return False
        for event in history:
            if event.event_type == "relabel_proposed":
                continue
            if event.label and normalize_label(event.label) == wanted:
                return True
        return False

    def _cooldown_clear(self, concept_id: int, now: datetime) -> bool:
        days = self._fl("concept_relabel_cooldown_days", 21.0)
        if days <= 0:
            return True
        try:
            history = self._events.list(
                concept_id=concept_id, event_type="relabeled", limit=1
            )
        except Exception:
            return True
        if not history:
            return True
        last = _parse(history[0].created_at)
        if last is None:
            return True
        return (now - last).total_seconds() >= days * 86400.0

    def _apply_relabel(
        self,
        concept: "Concept",
        proposed: str,
        proposal: ConceptEvent,
        vector: Any,
        now: datetime,
    ) -> bool:
        old_label = concept.label
        concept.label = proposed
        rationale = str(proposal.reason or "").strip()
        if rationale:
            concept.rationale = rationale
        # The label embedding is the belief's identity vector for dedupe
        # and surfacing, so it has to move with the wording. ``update``
        # re-mirrors and marks the cached active matrix dirty.
        concept.embedding = vector
        try:
            self._store.update(concept)
        except Exception:
            log.warning("relabel update failed", exc_info=True)
            concept.label = old_label
            return False

        try:
            self._events.add(
                ConceptEvent(
                    event_type="relabeled",
                    kind=concept.kind,
                    subject=concept.subject,
                    label=proposed,
                    confidence=float(concept.confidence),
                    evidence_count=int(concept.evidence_count),
                    distinct_source_count=int(concept.distinct_source_count),
                    reason=f"was '{old_label}'",
                    concept_id=int(concept.concept_id),
                    created_at=now.isoformat(),
                )
            )
        except Exception:
            log.debug("relabeled event insert failed", exc_info=True)
        log.info(
            "concept relabel: #%s %r -> %r",
            concept.concept_id, old_label, proposed,
        )
        return True

    # ── pass 2: classify + persist ────────────────────────────────────

    def _run_classification_pass(
        self, watermark: int, now: datetime, stats: dict[str, Any]
    ) -> None:
        limits = DriftThresholds.from_settings(self._memory_settings)
        try:
            moved = self._events.concepts_with_events_after(
                watermark,
                limit=max(1, self._i("concept_drift_max_concepts", 120)),
            )
        except Exception:
            log.debug("drift dirty-set read failed", exc_info=True)
            return
        if not moved:
            return

        traces = self._build_traces(moved)
        stats["traces"] = len(traces)
        if not traces:
            return

        candidates = self._succession_candidates(traces, limits)
        stats["succession_pairs"] = len(candidates)

        findings = detect_drift(
            traces,
            candidates,
            now=now,
            thresholds=limits,
            since_event_id=watermark,
        )
        stats["findings"] = len(findings)
        if not findings:
            return
        stats["recorded"] = self._persist(findings)
        self._publish_pending(findings)

    # ── pass 3: cold-start sweep ──────────────────────────────────────

    def _run_sweep_pass(self, now: datetime, stats: dict[str, Any]) -> None:
        """Classify one page of the concept id space, ignoring the watermark.

        Idempotent by construction: findings are fingerprinted on their
        decisive event, so a page that overlaps what the forward pass
        already recorded writes nothing new.
        """
        cursor = self._sweep_cursor()
        if cursor < 0:
            return
        page = max(1, self._i("concept_drift_sweep_page", 60))
        try:
            ids = self._events.concepts_with_events_after(
                0, limit=page, after_concept_id=cursor
            )
        except Exception:
            log.debug("drift sweep page read failed", exc_info=True)
            return
        if not ids:
            self._set_sweep_cursor(SWEEP_DONE)
            stats["sweep_done"] = True
            log.info("concept drift sweep complete")
            return

        limits = replace(
            DriftThresholds.from_settings(self._memory_settings),
            # The forward cap is sized for one hour of movement; the
            # backfill is reading years of it and would otherwise trickle.
            max_findings=max(
                1, self._i("concept_drift_sweep_max_findings", 24)
            ),
        )
        traces = self._build_traces(ids)
        stats["swept"] = len(ids)
        stats["sweep_traces"] = len(traces)
        if traces:
            candidates = self._succession_candidates(traces, limits)
            findings = detect_drift(
                traces,
                candidates,
                now=now,
                thresholds=limits,
                since_event_id=0,
            )
            stats["sweep_findings"] = len(findings)
            if findings:
                stats["sweep_recorded"] = self._persist(findings)

        # Advance past the page whatever it yielded: a page with nothing
        # salient in it has still been read. ``max`` is belt-and-braces
        # against a cursor that fails to move and loops the same page.
        nxt = max(int(ids[-1]), cursor + 1)
        self._set_sweep_cursor(nxt)
        stats["sweep_cursor"] = nxt

    def _build_traces(self, concept_ids: list[int]) -> list[ConceptTrace]:
        anchor = max(0, self._i("concept_drift_trace_anchor", 20))
        recent = max(1, self._i("concept_drift_trace_recent", 60))
        traces: list[ConceptTrace] = []
        for cid in concept_ids:
            concept = self._store.get(cid)
            if concept is None:
                continue
            try:
                events = self._events.drift_window(
                    cid, anchor=anchor, recent=recent
                )
            except Exception:
                continue
            traces.append(
                ConceptTrace(
                    concept_id=cid,
                    kind=concept.kind,
                    subject=concept.subject,
                    label=concept.label,
                    status=concept.status,
                    confidence=float(concept.confidence),
                    plasticity=float(concept.plasticity),
                    first_evidence_at=str(concept.first_evidence_at or ""),
                    points=tuple(
                        TrajectoryPoint(
                            event_id=int(e.event_id),
                            event_type=str(e.event_type),
                            label=str(e.label or ""),
                            confidence=float(e.confidence),
                            created_at=str(e.created_at or ""),
                        )
                        for e in events
                    ),
                    evidence_refs=self._evidence_refs(cid),
                )
            )
        return traces

    def _evidence_refs(self, concept_id: int) -> frozenset[tuple[str, str]]:
        try:
            return frozenset(
                (str(edge.src_type), str(edge.src_id))
                for edge in self._store.evidence_of(concept_id)
            )
        except Exception:
            return frozenset()

    def _succession_candidates(
        self, traces: list[ConceptTrace], limits: DriftThresholds
    ) -> list[SuccessionCandidate]:
        """Pair faded beliefs with risen ones in ONE matmul.

        The faded side is drawn from the moved set; the risen side spans
        the whole active set, because a belief that faded this week may
        have been superseded by one that formed months ago.
        """
        faded = [t for t in traces if t.status in _FADED]
        if not faded:
            return []
        try:
            risen = [
                c
                for c in self._store.list_by(status="active")
                if int(c.concept_id) > 0
            ]
        except Exception:
            return []
        if not risen:
            return []

        by_id = {t.concept_id: t for t in traces}
        faded_ids = [t.concept_id for t in faded]
        risen_ids = [int(c.concept_id) for c in risen]
        try:
            left_ids, left = self._store.matrix_snapshot(faded_ids)
            right_ids, right = self._store.matrix_snapshot(risen_ids)
        except Exception:
            log.debug("drift matrix snapshot failed", exc_info=True)
            return []
        if not left_ids or not right_ids:
            return []
        if left.shape[1] != right.shape[1]:
            return []

        # The single matmul. Everything above is bookkeeping.
        try:
            sims = left @ right.T
        except Exception:
            log.debug("drift succession matmul failed", exc_info=True)
            return []

        lo = limits.succession_min_cosine
        hi = limits.succession_max_cosine
        risen_by_id = {int(c.concept_id): c for c in risen}
        out: list[SuccessionCandidate] = []
        for row, old_id in enumerate(left_ids):
            old_trace = by_id.get(old_id)
            if old_trace is None:
                continue
            best: tuple[float, int] | None = None
            for col, new_id in enumerate(right_ids):
                if new_id == old_id:
                    continue
                cos = float(sims[row][col])
                if not (lo <= cos < hi):
                    continue
                if best is None or cos > best[0]:
                    best = (cos, new_id)
            if best is None:
                continue
            cos, new_id = best
            new_trace = by_id.get(new_id) or self._trace_for_active(
                risen_by_id[new_id]
            )
            if new_trace is None:
                continue
            out.append(
                SuccessionCandidate(old=old_trace, new=new_trace, cosine=cos)
            )
        return out

    def _trace_for_active(self, concept: "Concept") -> ConceptTrace | None:
        """A trace for a rising concept that was not itself in the moved
        set (it promoted before the current window)."""
        cid = int(concept.concept_id)
        try:
            events = self._events.drift_window(cid, anchor=10, recent=20)
        except Exception:
            return None
        return ConceptTrace(
            concept_id=cid,
            kind=concept.kind,
            subject=concept.subject,
            label=concept.label,
            status=concept.status,
            confidence=float(concept.confidence),
            plasticity=float(concept.plasticity),
            first_evidence_at=str(concept.first_evidence_at or ""),
            points=tuple(
                TrajectoryPoint(
                    event_id=int(e.event_id),
                    event_type=str(e.event_type),
                    label=str(e.label or ""),
                    confidence=float(e.confidence),
                    created_at=str(e.created_at or ""),
                )
                for e in events
            ),
            evidence_refs=self._evidence_refs(cid),
        )

    def _persist(self, findings: list[DriftFinding]) -> int:
        recorded = 0
        for finding in findings:
            labels = self._resolve_evidence(finding.concept_id)
            event = LearningEvent.from_finding(finding, evidence_labels=labels)
            try:
                if self._learning.add(event) > 0:
                    recorded += 1
            except Exception:
                log.debug("learning event persist failed", exc_info=True)
        return recorded

    def _resolve_evidence(self, concept_id: int) -> list[str]:
        provider = self._evidence_labels
        if provider is None:
            return []
        try:
            return [str(label) for label in provider(int(concept_id))][:6]
        except Exception:
            return []

    def _publish_pending(self, findings: list[DriftFinding]) -> None:
        """Write the bounded snapshot the T6 reflection provider reads.

        The turn path must never scan for this, so the worker leaves a
        small, already-rendered payload behind. Machinery (salience,
        ids, event types) is deliberately excluded from what a later
        prompt block could read: only what changed and why.
        """
        if self._kv_set is None:
            return
        cap = max(1, self._i("concept_drift_pending_cap", 3))
        floor = self._fl("concept_reflection_min_salience", 0.6)
        payload = [
            {
                "fingerprint": finding.fingerprint(),
                "shape": finding.shape,
                "subject": finding.subject,
                "old": finding.old_label,
                "new": finding.new_label,
                "because": finding.because,
                "salience": round(float(finding.salience), 4),
            }
            for finding in findings
            if finding.salience >= floor
        ][:cap]
        try:
            self._kv_set(
                DRIFT_PENDING_KEY,
                json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
            )
        except Exception:
            log.debug("drift pending snapshot write failed", exc_info=True)

    # ── embedding + adjudication ──────────────────────────────────────

    def _embed(self, text: str) -> Any:
        if self._embedder is None:
            return None
        try:
            return self._embedder.embed(text)
        except Exception:
            log.debug("relabel embed failed", exc_info=True)
            return None

    @staticmethod
    def _cosine(left: Any, right: Any) -> float:
        try:
            a = np.asarray(left, dtype=np.float32).ravel()
            b = np.asarray(right, dtype=np.float32).ravel()
        except Exception:
            return 0.0
        if a.size == 0 or b.size == 0 or a.size != b.size:
            return 0.0
        na = float(np.linalg.norm(a))
        nb = float(np.linalg.norm(b))
        if na <= 0.0 or nb <= 0.0:
            return 0.0
        return float(np.dot(a, b) / (na * nb))

    def _adjudicate(
        self,
        concept: "Concept",
        proposed: str,
        proposal: ConceptEvent,
        now: datetime,
    ) -> bool:
        """Is the proposed wording genuinely *better*, not merely different?

        Skipped entirely when no model is wired (lean deployments and
        tests), in which case the cheap gates above are the whole bar.
        """
        if self._ollama is None:
            return True
        if self._rate_limiter is not None and not self._rate_limiter.allow(now):
            return False
        system = (
            "You are a precise annotator. An existing belief statement and a "
            "newly proposed wording of the SAME belief are given. Answer "
            "whether the new wording is a genuine improvement: more "
            "specific, more accurate, or better matched to recent evidence, "
            "while still asserting the same thing. Answer false if it "
            "asserts something different, is vaguer, is merely a stylistic "
            "variation, or if you are uncertain. "
            'Return JSON only: {"better": boolean, "reason": string}.'
        )
        user = (
            f"Current: {concept.label}\n"
            f"Proposed: {proposed}\n"
            f"Why it was proposed: {proposal.reason or '(not given)'}\n\n"
            "Is the proposed wording a genuine improvement?"
        )
        parsed = self._call_llm(system, user)
        if not isinstance(parsed, dict):
            return False
        return parsed.get("better") is True

    def _call_llm(self, system: str, user: str) -> dict[str, Any] | None:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        chunks: list[str] = []
        try:
            for chunk in self._ollama.chat_stream(
                messages,
                options={
                    "num_predict": _MAX_TOKENS,
                    "temperature": _TEMPERATURE,
                },
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="concept_drift_worker",
            ):
                chunks.append(chunk)
        except Exception:
            log.warning("relabel adjudication call failed", exc_info=True)
            return None
        match = _JSON_OBJECT_RE.search("".join(chunks))
        if match is None:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None

    # ── negative cache ────────────────────────────────────────────────

    def _remember_rejection(
        self, key: tuple[int, str], now: datetime
    ) -> None:
        self._negative_cache[key] = now + timedelta(
            seconds=_NEG_CACHE_TTL_SECONDS
        )

    def _evict_expired(self, now: datetime) -> None:
        for key in [k for k, exp in self._negative_cache.items() if exp <= now]:
            self._negative_cache.pop(key, None)
        if len(self._negative_cache) > _NEG_CACHE_MAX:
            overflow = len(self._negative_cache) - _NEG_CACHE_MAX
            for key, _exp in sorted(
                self._negative_cache.items(), key=lambda kv: kv[1]
            )[:overflow]:
                self._negative_cache.pop(key, None)


__all__ = [
    "DRIFT_PENDING_KEY",
    "DRIFT_SWEEP_KEY",
    "DRIFT_WATERMARK_KEY",
    "SWEEP_DONE",
    "ConceptDriftWorker",
]
