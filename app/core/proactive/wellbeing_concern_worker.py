"""K72 — Wellbeing-concern worker (silent producer).

During a quiet window this worker reads multi-day signal — the user's
recent message timestamps (small-hours activity), recent message text
(explicit "haven't slept / eaten" mentions), and the H3 mood-drift ring
(a heavy low stretch) — runs the pure :func:`pick_concern` detector, and
only when a real worrying pattern clears a high bar AND the long cooldown
has elapsed drafts ONE private cue into the ``aiko.wellbeing_concern`` kv
ring. The consumer
:meth:`InnerLifeProvidersMixin._render_wellbeing_concern_block` surfaces
the newest unseen cue on a later turn (watermark-gated). This worker
never speaks or fires a proactive nudge.

Pacing (kv watermarks, all swallow-and-log):
  * ``wellbeing_concern.last_fired_at``  — wall-clock cooldown (days).
  * ``wellbeing_concern.last_signature`` — same-pattern suppression so
    the identical "a few late nights" never re-drafts; an *escalation*
    (more nights / a new neglect category) is a different signature and
    breaks through.

Reads ``messages`` directly (timestamps + content for the lexical scan),
local-tz the same way K3 ``ScheduleLearner`` does (``dt.astimezone()``).
Every failure path is swallowed and logged at debug — the worst case is
a missed beat, never a crashed tick.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.affect import mood_drift as _md
from app.core.proactive.idle_worker import default_is_ready
from app.core.relationship import wellbeing_concern as _wc


if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.wellbeing_concern_worker")


_KV_LAST_FIRED_AT = "wellbeing_concern.last_fired_at"
_KV_LAST_SIGNATURE = "wellbeing_concern.last_signature"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: object) -> datetime | None:
    if not isinstance(value, str):
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


class WellbeingConcernWorker:
    """IdleWorker that drafts rare, gentle "you doing okay?" cues."""

    name = "wellbeing_concern"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 21600.0,
        cooldown_days: float = 7.0,
        window_days: int = 7,
        late_night_min: int = _wc.DEFAULT_LATE_NIGHT_MIN,
        neglect_min_days: int = _wc.DEFAULT_NEGLECT_MIN_DAYS,
        rough_run: int = _wc.DEFAULT_ROUGH_RUN,
        rough_threshold: float = _wc.DEFAULT_ROUGH_THRESHOLD,
        journal_max: int = 4,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._enabled_provider = enabled_provider
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_days) * 86400.0)
        self._window_days = max(1, int(window_days))
        self._late_night_min = max(1, int(late_night_min))
        self._neglect_min_days = max(1, int(neglect_min_days))
        self._rough_run = max(1, int(rough_run))
        self._rough_threshold = float(rough_threshold)
        self._journal_max = max(1, int(journal_max))
        self._clock = clock or _utcnow
        # MCP debug: bypass the cooldown + signature gates on next run().
        self._force_next = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled():
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"drafted": 0, "disabled": True}

        now = self._clock()
        forced = self._force_next
        self._force_next = False

        if not forced and not self._cooldown_elapsed(now):
            return {"drafted": 0, "skipped_cooldown": True}

        late_dates, neglect_days, neglect_cats = self._collect_message_signal(now)
        drift_samples = _md.deserialize_samples(self._kv_get_safe(_md.KV_SAMPLES))

        finding = _wc.pick_concern(
            late_night_dates=late_dates,
            neglect_days=neglect_days,
            neglect_categories=neglect_cats,
            drift_samples=drift_samples,
            late_night_min=self._late_night_min,
            neglect_min_days=self._neglect_min_days,
            rough_run=self._rough_run,
            rough_threshold=self._rough_threshold,
        )
        if finding is None:
            return {
                "drafted": 0,
                "no_finding": True,
                "late_nights": len(late_dates),
                "neglect_days": len(neglect_days),
                "samples": len(drift_samples),
            }

        if not forced:
            last_sig = self._kv_get_safe(_KV_LAST_SIGNATURE)
            if last_sig and last_sig == finding.signature:
                return {"drafted": 0, "same_signature": finding.signature}

        _wc.append_finding(
            self._chat_db.kv_get,
            self._chat_db.kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "kind": finding.kind,
                "detail": finding.detail,
                "severity": finding.severity,
                "signature": finding.signature,
            },
            max_entries=self._journal_max,
        )
        self._mark_fired(now, finding.signature)
        log.info(
            "wellbeing-concern drafted: kind=%s detail=%r severity=%.2f",
            finding.kind,
            finding.detail,
            finding.severity,
        )
        return {
            "drafted": 1,
            "kind": finding.kind,
            "detail": finding.detail,
            "severity": finding.severity,
            "signature": finding.signature,
        }

    # ── signal collection ────────────────────────────────────────────

    def _collect_message_signal(
        self, now: datetime,
    ) -> tuple[list[str], list[str], list[str]]:
        """Return ``(late_night_dates, neglect_days, neglect_categories)``.

        One bounded range scan over recent user messages; small-hours
        activity is keyed on local date, neglect mentions on local date +
        category. All best-effort: a SELECT failure yields empty lists so
        the detector simply finds nothing.
        """
        cutoff = now - timedelta(days=self._window_days)
        try:
            rows = self._chat_db.execute_fetchall(
                "SELECT created_at, content FROM messages "
                "WHERE role = 'user' AND created_at >= ? "
                "ORDER BY created_at ASC",
                (cutoff.astimezone(timezone.utc).isoformat(),),
            )
        except Exception:
            log.debug("wellbeing-concern SELECT failed", exc_info=True)
            return [], [], []

        late_dates: set[str] = set()
        neglect_days: set[str] = set()
        neglect_cats: set[str] = set()
        for row in rows or []:
            if not row:
                continue
            dt = _parse_iso(row[0])
            if dt is None:
                continue
            local = dt.astimezone()
            date_key = local.strftime("%Y-%m-%d")
            if _wc.LATE_NIGHT_START_HOUR <= local.hour < _wc.LATE_NIGHT_END_HOUR:
                late_dates.add(date_key)
            content = row[1] if len(row) > 1 else ""
            cats = _wc.classify_neglect_text(str(content or ""))
            if cats:
                neglect_days.add(date_key)
                neglect_cats.update(cats)
        return sorted(late_dates), sorted(neglect_days), sorted(neglect_cats)

    # ── gates ────────────────────────────────────────────────────────

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

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
            return self._chat_db.kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._chat_db.kv_set(key, value)
        except Exception:
            log.debug(
                "wellbeing_concern kv_set failed key=%s", key, exc_info=True,
            )
