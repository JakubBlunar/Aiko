"""User-correction worker (F13 personality backlog).

The off-turn half of F13. The turn path
(:meth:`app.core.session.post_turn_helpers_mixin.PostTurnHelpersMixin._maybe_capture_user_correction`)
does only a cheap pattern gate and stashes a candidate pair -- the user's
message plus the stored note it might be correcting. This worker drains
that queue during idle windows and does the expensive, irreversible half
that must never sit on the turn path:

1. **Confirm.** Ask the worker LLM whether the user's message really is an
   explicit correction of the stored note, and if so what the corrected
   fact is. Rate-limited on the shared :class:`FactCheckRateLimiter`
   plumbing, exactly like the F5 conflict detector's borderline tier. This
   is the true precision gate -- a false positive here rewrites a true
   memory, so the bar is "prefer NO when uncertain".

2. **Supersede.** On a confirmed correction, write the corrected fact as a
   new high-confidence memory and demote the corrected row (confidence
   floored, tier -> ``archive``, ``metadata.superseded_by`` stamped) -- the
   same supersede shape F5 uses, so a user-corrected memory looks the same
   in retrieval as a conflict-demoted one, and the link stays auditable and
   reversible.

3. **Propagate (no LLM).** If the demoted memory was evidence under any
   concept, knock that concept's confidence down by a
   plasticity-damped penalty
   (:func:`app.core.concepts.concept_lifecycle.apply_contradiction_penalty`),
   so a contradicted belief does not resurface right after the user
   corrected the fact under it. Pure arithmetic -- an edge lookup and a
   formula, no model call.

4. **Acknowledge.** Arm a low-key next-turn cue ("ah, I had that
   backwards") so Aiko owns the correction once, naturally, rather than
   silently swallowing it.

The correction of *fact* vs disagreement of *opinion* boundary is enforced
upstream by the detector (only ``fact`` / ``preference`` / ``relationship``
/ ``event`` rows are candidates; ``self`` stance rows are not) and again by
the LLM's NO verdict on a mere difference of taste.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.concepts.concept_lifecycle import apply_contradiction_penalty
from app.core.infra import timephrase
from app.core.proactive.idle_worker import WorkSignal, pressure_from_count

if TYPE_CHECKING:
    from app.core.concepts.concept_store import ConceptStore
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.memory.memory_store import MemoryStore
    from app.llm.embedder import Embedder
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.user_correction_worker")


# Confidence the corrected fact is written at -- high, because a user
# correction is the best evidence the system ever gets, but short of a
# pinned 0.9+ so ordinary hygiene can still touch it later.
_CORRECTION_CONFIDENCE = 0.9

# Confidence the corrected row is clamped to on demotion. Matches the F5
# / F1 contradict floor so a user-superseded memory looks identical to a
# conflict-demoted one in retrieval.
_DEMOTE_CONFIDENCE = 0.20

_VERIFY_MAX_TOKENS = 120
_LOG_PREVIEW_CHARS = 200

_ALLOWED_KINDS: frozenset[str] = frozenset({
    "fact",
    "preference",
    "relationship",
    "event",
})

_SYSTEM_PROMPT = (
    "You decide whether the USER MESSAGE is explicitly correcting a FACT "
    "the assistant had stored about the user, and if so what the corrected "
    "fact is. Answer with ONE JSON object on a single line and nothing "
    "else. Schema: {\"verdict\": \"YES\" | \"NO\", \"correction\": \"<the "
    "corrected fact as a short standalone statement about the user, <= 120 "
    "chars; empty when NO>\"}. "
    "YES only when the message directly contradicts and repairs the stored "
    "note (\"no, it's my sister\", \"I never said that\", \"actually it's "
    "Tuesday\"). "
    "NO for agreement, elaboration, a new unrelated fact, or a mere "
    "difference of opinion or taste. "
    "Write the correction in the third person about the user, not as a "
    "quote of their words. Be strict: prefer NO when uncertain."
)

_USER_TEMPLATE = "STORED NOTE: {note}\nUSER MESSAGE: {msg}"

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return timephrase.utcnow()


def _preview(text: str | None) -> str:
    if text is None:
        return ""
    s = str(text)
    if len(s) <= _LOG_PREVIEW_CHARS:
        return s
    return s[: _LOG_PREVIEW_CHARS - 1] + "\u2026"


@dataclass(slots=True)
class _Confirmation:
    verdict: str  # "YES" | "NO"
    correction: str


class UserCorrectionWorker:
    """IdleWorker that confirms and applies user corrections (F13)."""

    name = "user_correction_worker"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        embedder: "Embedder",
        ollama: "OllamaClient",
        chat_model: str,
        rate_limiter: "FactCheckRateLimiter",
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        drain_candidates: Callable[[], list[Any]],
        pending_count: Callable[[], int],
        queue_cue: Callable[..., bool],
        concept_store: "ConceptStore | None" = None,
        notify_memory_updated: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._embedder = embedder
        self._ollama = ollama
        self._chat_model = chat_model
        self._rate_limiter = rate_limiter
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._drain_candidates = drain_candidates
        self._pending_count = pending_count
        self._queue_cue = queue_cue
        self._concept_store = concept_store
        self._notify_memory_updated = notify_memory_updated
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "user_correction_interval_seconds",
                45,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        """Enabled, and something actually stashed to confirm.

        The empty queue is a **hard veto** rather than zero pressure, for
        the reason :meth:`IdleFactChecker.is_ready` spells out: the
        scheduler's heartbeat is checked *before* ``signal.pressure``
        (:func:`evaluate_admission`), so a worker that expresses "nothing
        to do" only as pressure still runs every heartbeat forever. That
        is not hypothetical -- F13 has never had a candidate in the app's
        history, and the resulting `no_candidates` line was 675 of 18,168
        lines in a log rotation (H53).
        """
        if not bool(
            getattr(self._agent_settings, "user_correction_enabled", True)
        ):
            return False
        try:
            return int(self._pending_count()) > 0
        except Exception:
            # A broken probe must not wedge the worker shut: fall through
            # to a run, which is cheap when the queue really is empty.
            log.debug("user-correction readiness probe failed", exc_info=True)
            return True

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Pressure from candidates the turn path has stashed but not yet
        confirmed. Cheap: an in-memory queue length, no store touch.
        """
        if not bool(
            getattr(self._agent_settings, "user_correction_enabled", True)
        ):
            return WorkSignal(pressure=0.0, reason="disabled")
        try:
            pending = int(self._pending_count())
        except Exception:
            log.debug("user-correction demand probe failed", exc_info=True)
            return None
        if pending < 1:
            return WorkSignal(pressure=0.0, reason="no candidates")
        return WorkSignal(
            # Even one pending correction is worth confirming promptly --
            # the acknowledgment cue is only apt for a turn or two -- so a
            # tiny queue already carries most of the pressure.
            pressure=pressure_from_count(pending, saturation=3),
            reason=f"{pending} pending",
            needs_llm=True,
        )

    def run(self) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "user_correction_enabled", True)
        ):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        candidates = self._drain_candidates() or []
        if not candidates:
            return {"skipped": True, "reason": "no_candidates"}

        max_per_run = int(
            getattr(
                self._memory_settings,
                "user_correction_max_per_run",
                8,
            )
        )
        penalty = float(
            getattr(
                self._memory_settings,
                "user_correction_concept_penalty",
                0.25,
            )
        )
        now = self._clock()

        stats = {
            "candidates": len(candidates),
            "confirmed": 0,
            "rejected": 0,
            "skipped_stale": 0,
            "skipped_rate_limit": 0,
            "llm_unparseable": 0,
            "concepts_touched": 0,
            "llm_total_ms": 0.0,
        }

        for candidate in candidates[: max(1, max_per_run)]:
            if self._cancel_event.is_set():
                stats["cancelled"] = True
                break
            hit = candidate.get("hit") if isinstance(candidate, dict) else None
            user_text = (
                candidate.get("user_text", "") if isinstance(candidate, dict) else ""
            )
            if hit is None:
                continue

            mem = self._memory_store.get(int(getattr(hit, "memory_id", 0) or 0))
            # The row may have moved on since it was stashed: deleted, or
            # already superseded by F5 / another correction. Re-correcting
            # an archived row is meaningless.
            if mem is None or self._already_superseded(mem):
                stats["skipped_stale"] += 1
                continue

            if not self._rate_limiter.allow(now):
                stats["skipped_rate_limit"] += 1
                # The rest of the batch would hit the same wall; stop and
                # let them drop rather than spin.
                break

            t0 = time.monotonic()
            confirmation = self._confirm_with_llm(mem.content, user_text)
            stats["llm_total_ms"] += (time.monotonic() - t0) * 1000.0
            if confirmation is None:
                stats["llm_unparseable"] += 1
                continue
            if confirmation.verdict != "YES" or not confirmation.correction:
                stats["rejected"] += 1
                log.info(
                    "F13 rejected: memory_id=%s verdict=%s msg=%r",
                    mem.id,
                    confirmation.verdict,
                    _preview(user_text),
                )
                continue

            if self._apply_correction(
                old_id=int(mem.id),
                old_content=mem.content,
                kind=str(getattr(mem, "kind", "") or "fact"),
                correction=confirmation.correction,
                penalty=penalty,
                now=now,
                stats=stats,
            ):
                stats["confirmed"] += 1

        stats["llm_total_ms"] = round(float(stats["llm_total_ms"]), 1)
        log.info("user-correction done: %s", stats)
        return stats

    # ── correction application ───────────────────────────────────────

    @staticmethod
    def _already_superseded(mem: Any) -> bool:
        if str(getattr(mem, "tier", "")).strip().lower() == "archive":
            return True
        metadata = getattr(mem, "metadata", None)
        if isinstance(metadata, dict) and metadata.get("superseded_by"):
            return True
        return False

    def _apply_correction(
        self,
        *,
        old_id: int,
        old_content: str,
        kind: str,
        correction: str,
        penalty: float,
        now: datetime,
        stats: dict[str, Any],
    ) -> bool:
        """Write the correction, demote the old row, propagate, acknowledge.

        Returns ``True`` if the supersede stuck. Ordered so the write is
        the last thing that can fail before the demotion: a new memory with
        no demotion is a harmless duplicate, but a demotion with no
        replacement would quietly drop the fact.
        """
        norm_kind = kind.strip().lower()
        if norm_kind not in _ALLOWED_KINDS:
            norm_kind = "fact"

        try:
            embedding = self._embedder.embed(correction)
        except Exception:
            log.warning("F13 embed failed for correction", exc_info=True)
            return False

        when_iso = now.isoformat() if isinstance(now, datetime) else _utcnow().isoformat()

        try:
            new_mem = self._memory_store.add(
                correction,
                norm_kind,
                embedding,
                confidence=float(
                    getattr(
                        self._memory_settings,
                        "user_correction_confidence",
                        _CORRECTION_CONFIDENCE,
                    )
                ),
                metadata={
                    "source": "user_correction",
                    "corrects_memory_id": int(old_id),
                    "corrected_at": when_iso,
                },
                # F16 (v30): the user just told her the right version to her
                # face -- the highest-quality testimony there is, so the
                # corrected row is ``stated``, never a hedged impression.
                provenance="stated",
            )
        except Exception:
            log.warning("F13 add correction failed", exc_info=True)
            return False
        if new_mem is None:
            log.info("F13 add returned no row (deduped?); skipping demotion")
            return False
        new_id = int(new_mem.id)

        try:
            self._memory_store.update(
                int(old_id),
                confidence=_DEMOTE_CONFIDENCE,
                tier="archive",
                metadata={
                    "superseded_by": new_id,
                    "superseded_at": when_iso,
                    "superseded_reason": "user_correction",
                },
                metadata_merge=True,
            )
        except Exception:
            log.warning(
                "F13 demote failed: old_id=%s (correction %s stands alone)",
                old_id,
                new_id,
                exc_info=True,
            )

        self._notify(old_id)
        self._notify(new_id)

        stats["concepts_touched"] += self._propagate_to_concepts(
            old_id, penalty,
        )

        try:
            self._queue_cue(wrong=old_content, corrected=correction)
        except Exception:
            log.debug("F13 cue arm failed", exc_info=True)

        log.info(
            "F13 correction applied: old_id=%s -> new_id=%s kind=%s "
            "correction=%r",
            old_id,
            new_id,
            norm_kind,
            _preview(correction),
        )
        return True

    def _propagate_to_concepts(self, old_id: int, penalty: float) -> int:
        """Knock down the confidence of concepts the demoted memory backed.

        No LLM: F13 already knows the memory was wrong, so it skips the L9
        contradiction *detector* (the only place a concept model call
        lives) and applies the penalty straight to the affected concepts.
        """
        store = self._concept_store
        if store is None or penalty <= 0.0:
            return 0
        try:
            affected = store.affected_concepts_for_memory(int(old_id))
        except Exception:
            log.debug("F13 affected_concepts lookup failed", exc_info=True)
            return 0
        touched = 0
        for cid in affected:
            try:
                concept = store.get(int(cid))
            except Exception:
                continue
            if concept is None:
                continue
            if str(getattr(concept, "status", "")) not in ("active", "candidate"):
                continue
            before = float(concept.confidence)
            concept.confidence = apply_contradiction_penalty(
                before,
                penalty=penalty,
                plasticity=float(getattr(concept, "plasticity", 0.5)),
            )
            try:
                store.update(concept)
            except Exception:
                log.debug(
                    "F13 concept update failed: cid=%s", cid, exc_info=True,
                )
                continue
            touched += 1
            log.info(
                "F13 concept penalty: cid=%s conf %.3f -> %.3f",
                cid,
                before,
                concept.confidence,
            )
        return touched

    def _notify(self, memory_id: int) -> None:
        if self._notify_memory_updated is None:
            return
        try:
            self._notify_memory_updated({"memory_id": int(memory_id)})
        except Exception:
            log.debug("F13 notify_memory_updated raised", exc_info=True)

    # ── LLM confirmation ─────────────────────────────────────────────

    def _confirm_with_llm(self, note: str, msg: str) -> _Confirmation | None:
        user_content = _USER_TEMPLATE.format(note=note or "", msg=msg or "")
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_content},
        ]
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={"num_predict": _VERIFY_MAX_TOKENS},
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                # Telling a correction from a new fact or a taste
                # disagreement is a borderline judgement reasoning helps.
                think=True,
                surface="user_correction_worker",
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning("F13 confirm call raised", exc_info=True)
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return None
        return self._parse_confirmation(raw)

    @staticmethod
    def _parse_confirmation(raw: str) -> _Confirmation | None:
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
        if verdict not in {"YES", "NO"}:
            return None
        correction = str(parsed.get("correction", "")).strip()
        if len(correction) > 200:
            correction = correction[:197] + "\u2026"
        return _Confirmation(verdict=verdict, correction=correction)


__all__ = ["UserCorrectionWorker"]
