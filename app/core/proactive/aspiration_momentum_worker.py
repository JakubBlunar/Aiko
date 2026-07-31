"""L14 — Aspiration momentum worker (silent producer).

During a quiet window this worker reads the active ``aspiration`` concepts
(through a :class:`~app.core.concepts.concept_view.ConceptView`, per the L24
"read through the view, never the store" rule), picks at most one that is
confident, has gone *stale* since it was last reinforced, and is off its
per-concept cooldown, and drafts ONE private cue into the
``aiko.aspiration_momentum`` kv ring. The consumer
:meth:`InnerLifeProvidersMixin._render_aspiration_momentum_block` surfaces the
newest unseen cue on a later turn (watermark-gated). This worker never speaks or
fires a proactive nudge -- it is a *cue producer*; the chat model phrases the
check-in in context (never verbatim).

Pacing:
  * per-concept cooldown kv (``aspiration_momentum.last.<id>``) so a check-in
    rotates across the active aspirations instead of hammering the top one;
  * a global signature watermark (``aspiration_momentum.last_signature``) so the
    identical cue never re-drafts back-to-back;
  * the idle-scheduler ``interval_seconds`` provides the natural global cadence.

Every failure path is swallowed and logged at debug -- the worst case is a
missed beat, never a crashed tick.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive import aspiration_momentum as _am
from app.core.proactive.idle_worker import default_is_ready
from app.core.infra import timephrase


if TYPE_CHECKING:
    from app.core.concepts.concept_view import ConceptView


log = logging.getLogger("app.aspiration_momentum_worker")


_KV_LAST_SIGNATURE = "aspiration_momentum.last_signature"


def _utcnow() -> datetime:
    return timephrase.utcnow()


class AspirationMomentumWorker:
    """IdleWorker that drafts occasional "check in on the journey" cues."""

    name = "aspiration_momentum"

    def __init__(
        self,
        *,
        kv_get: Callable[[str], "str | None"],
        kv_set: Callable[[str, str], None],
        view_provider: Callable[[], "ConceptView | None"],
        user_display_name_provider: Callable[[], str],
        enabled_provider: Callable[[], bool] | None = None,
        subjects: tuple[str, ...] = ("user", "aiko"),
        interval_seconds: float = 21600.0,
        cooldown_days: float = 10.0,
        min_confidence: float = 0.6,
        staleness_min_days: float = 7.0,
        journal_max: int = 4,
    ) -> None:
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._view_provider = view_provider
        self._user_display_name_provider = user_display_name_provider
        self._enabled_provider = enabled_provider
        self._subjects = tuple(subjects) or ("user", "aiko")
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._cooldown_days = max(0.0, float(cooldown_days))
        self._min_confidence = float(min_confidence)
        self._staleness_min_days = max(0.0, float(staleness_min_days))
        self._journal_max = max(1, int(journal_max))
        # MCP debug: bypass the cooldown + signature gates on next run().
        self._force_next = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if self._enabled_provider is not None:
            try:
                if not bool(self._enabled_provider()):
                    return False
            except Exception:
                pass
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if self._enabled_provider is not None:
            try:
                if not bool(self._enabled_provider()):
                    return {"drafted": 0, "disabled": True}
            except Exception:
                pass

        now = _utcnow()
        forced = self._force_next
        self._force_next = False

        view = None
        try:
            view = self._view_provider()
        except Exception:
            log.debug("aspiration_momentum view_provider raised", exc_info=True)
        if view is None or not getattr(view, "enabled", False):
            return {"drafted": 0, "no_view": True}

        concepts: list[Any] = []
        for subject in self._subjects:
            try:
                concepts.extend(
                    view.core(
                        kind="aspiration",
                        subject=subject,
                        min_confidence=self._min_confidence,
                    )
                )
            except Exception:
                log.debug(
                    "aspiration_momentum core(%s) raised", subject, exc_info=True
                )
        if not concepts:
            return {"drafted": 0, "no_active": True}

        # Forced runs relax the staleness + per-concept cooldown gates so an
        # MCP poke always produces a cue when *any* aspiration exists.
        candidate = _am.select_candidate(
            concepts,
            now=now,
            kv_get=self._kv_get,
            min_confidence=self._min_confidence,
            staleness_min_days=0.0 if forced else self._staleness_min_days,
            cooldown_days=0.0 if forced else self._cooldown_days,
        )
        if candidate is None:
            return {"drafted": 0, "no_candidate": True, "active": len(concepts)}

        if not forced:
            last_sig = self._kv_get_safe(_KV_LAST_SIGNATURE)
            if last_sig and last_sig == candidate.signature:
                return {"drafted": 0, "same_signature": candidate.signature}

        _am.append_cue(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "concept_id": candidate.concept_id,
                "subject": candidate.subject,
                "label": candidate.label,
            },
            max_entries=self._journal_max,
        )
        self._mark_fired(now, candidate)
        log.info(
            "aspiration-momentum drafted: id=%d subject=%s stale=%.1fd",
            candidate.concept_id,
            candidate.subject,
            candidate.staleness_days,
        )
        return {
            "drafted": 1,
            "concept_id": candidate.concept_id,
            "subject": candidate.subject,
            "signature": candidate.signature,
        }

    # ── helpers ──────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm a one-shot bypass of the staleness/cooldown + signature gates."""
        self._force_next = True

    def _mark_fired(self, now: datetime, candidate: "_am.MomentumCandidate") -> None:
        stamp = now.isoformat(timespec="seconds")
        self._kv_set_safe(
            _am.per_concept_cooldown_key(candidate.concept_id), stamp
        )
        self._kv_set_safe(_KV_LAST_SIGNATURE, candidate.signature)

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug(
                "aspiration_momentum kv_set failed key=%s", key, exc_info=True
            )


__all__ = ["AspirationMomentumWorker"]
