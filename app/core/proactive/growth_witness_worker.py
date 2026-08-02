"""K70 — Growth-witness worker (silent producer).

During a quiet window this worker reads the H3 mood-drift daily ring
(``aiko.mood_drift_samples``), runs the pure :func:`detect_growth`
detector over an older-baseline-vs-recent window, and — only when a real
durable **positive** shift clears a high bar and the multi-week cooldown
has elapsed — drafts ONE private cue into the ``aiko.growth_witness`` kv
ring. The consumer
:meth:`InnerLifeProvidersMixin._render_growth_witness_block` surfaces the
newest unseen cue on a later turn (watermark-gated). This worker never
speaks or fires a proactive nudge.

Pacing (kv watermarks, all swallow-and-log):
  * ``growth_witness.last_fired_at``  — wall-clock cooldown (days).
  * ``growth_witness.last_signature`` — same-finding suppression so the
    identical "you seem lighter" never re-drafts back-to-back.

The cue can be enriched with a deterministic corroborating detail (a goal
the user has been chipping at) read from the optional goal store; the
detail is woven into the rendered cue, never a standalone trigger.

Every failure path is swallowed and logged at debug — the worst case is a
missed beat, never a crashed tick.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.affect import mood_drift as _md
from app.core.proactive.idle_worker import WorkSignal
from app.core.relationship import growth_witness as _gw
from app.core.infra import timephrase


if TYPE_CHECKING:
    from app.core.goals.goal_store import GoalStore


log = logging.getLogger("app.growth_witness_worker")


_KV_LAST_FIRED_AT = "growth_witness.last_fired_at"
_KV_LAST_SIGNATURE = "growth_witness.last_signature"


def _utcnow() -> datetime:
    return timephrase.utcnow()


def _parse_iso(value: str | None) -> datetime | None:
    if not value or not isinstance(value, str):
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


class GrowthWitnessWorker:
    """IdleWorker that drafts rare "you've grown" cues about the user."""

    name = "growth_witness"

    def __init__(
        self,
        *,
        kv_get: Callable[[str], "str | None"],
        kv_set: Callable[[str, str], None],
        user_display_name_provider: Callable[[], str],
        enabled_provider: Callable[[], bool] | None = None,
        goal_store: "GoalStore | None" = None,
        interval_seconds: float = 21600.0,
        cooldown_days: float = 14.0,
        min_samples: int = _gw.DEFAULT_MIN_SAMPLES,
        min_valence_delta: float = _gw.DEFAULT_MIN_VALENCE_DELTA,
        min_axis_delta: float = _gw.DEFAULT_MIN_AXIS_DELTA,
        journal_max: int = 4,
    ) -> None:
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_display_name_provider = user_display_name_provider
        self._enabled_provider = enabled_provider
        self._goal_store = goal_store
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_days) * 86400.0)
        self._min_samples = max(2, int(min_samples))
        self._min_valence_delta = float(min_valence_delta)
        self._min_axis_delta = float(min_axis_delta)
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
        return self._enabled()

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            # Matches run(): a raising provider is no opinion, not a veto.
            return True

    def _evaluate(
        self, now: datetime, *, forced: bool,
    ) -> "tuple[str | None, _gw.GrowthFinding | None, int]":
        """Resolve the finding this tick would draft.

        Returns ``(block_reason, finding, sample_count)``; a
        ``block_reason`` of ``None`` means "draft this finding".

        Read-only, so the probe can share it: the mood-drift ring and
        the signature watermark are ``kv_get``, ``detect_growth`` is
        pure, and ``_goal_detail`` only lists active goals. The writes
        live in ``append_finding`` / ``_mark_fired``, which only
        ``run()`` reaches.
        """
        if not forced and not self._cooldown_elapsed(now):
            return "skipped_cooldown", None, 0

        samples = _md.deserialize_samples(
            self._kv_get_safe(_md.KV_SAMPLES)
        )
        finding = _gw.detect_growth(
            samples,
            min_samples=self._min_samples,
            min_valence_delta=self._min_valence_delta,
            min_axis_delta=self._min_axis_delta,
            detail=self._goal_detail(),
        )
        if finding is None:
            return "no_finding", None, len(samples)

        if not forced:
            last_sig = self._kv_get_safe(_KV_LAST_SIGNATURE)
            if last_sig and last_sig == finding.signature:
                return "same_signature", finding, len(samples)
        return None, finding, len(samples)

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Binary: either the drift ring shows a durable shift or it doesn't.

        The 14-day cooldown is checked first and is the cheapest gate —
        one kv read — so on almost every tick the probe stops there
        without deserializing the ring at all.

        Probes as an unforced run; ``_force_next`` is debug state that
        ``run()`` consumes.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        reason, finding, _samples = self._evaluate(now, forced=False)
        if reason is not None or finding is None:
            return WorkSignal(pressure=0.0, reason=reason or "no_finding")
        return WorkSignal(pressure=1.0, reason=finding.kind)

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

        reason, finding, samples = self._evaluate(now, forced=forced)
        if reason == "skipped_cooldown":
            return {"drafted": 0, "skipped_cooldown": True}
        if reason == "no_finding":
            return {"drafted": 0, "no_finding": True, "samples": samples}
        if reason == "same_signature" and finding is not None:
            return {"drafted": 0, "same_signature": finding.signature}
        assert finding is not None

        _gw.append_finding(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "kind": finding.kind,
                "magnitude": finding.magnitude,
                "span_days": finding.span_days,
                "signature": finding.signature,
                "detail": finding.detail,
            },
            max_entries=self._journal_max,
        )
        self._mark_fired(now, finding.signature)
        log.info(
            "growth-witness drafted: kind=%s mag=%.2f span=%dd detail=%s",
            finding.kind,
            finding.magnitude,
            finding.span_days,
            bool(finding.detail),
        )
        return {
            "drafted": 1,
            "kind": finding.kind,
            "magnitude": finding.magnitude,
            "span_days": finding.span_days,
            "signature": finding.signature,
        }

    # ── corroboration ────────────────────────────────────────────────

    def _goal_detail(self) -> str:
        """A short phrase for a goal the user has been chipping at, or ''."""
        store = self._goal_store
        if store is None:
            return ""
        try:
            active = store.list_active()
        except Exception:
            return ""
        for goal in active or []:
            meta = getattr(goal, "metadata", None) or {}
            note = str(meta.get("last_progress_note") or "").strip()
            if not note:
                continue
            title = str(getattr(goal, "content", "") or "").strip()
            if not title:
                continue
            if len(title) > 60:
                title = title[:57].rsplit(" ", 1)[0] + "…"
            return f"the work he's been putting into {title}"
        return ""

    # ── gates ────────────────────────────────────────────────────────

    def _cooldown_elapsed(self, now: datetime) -> bool:
        if self._cooldown_seconds <= 0:
            return True
        last = _parse_iso(self._kv_get_safe(_KV_LAST_FIRED_AT))
        if last is None:
            return True
        return (now - last).total_seconds() >= self._cooldown_seconds

    def _mark_fired(self, now: datetime, signature: str) -> None:
        self._kv_set_safe(_KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))
        self._kv_set_safe(_KV_LAST_SIGNATURE, signature)

    # ── helpers ──────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm a one-shot bypass of the cooldown + signature gates."""
        self._force_next = True

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug("growth_witness kv_set failed key=%s", key, exc_info=True)
