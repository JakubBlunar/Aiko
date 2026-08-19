"""Memory salience decay worker (schema v8 / E1+E2).

Thin :class:`IdleWorker` that calls :meth:`MemoryStore.decay` with the
current :class:`MemorySettings` per-tier rates + revival coefficients.
The actual elapsed-time accounting lives inside ``decay()`` itself,
which reads ``memory.last_decay_run_at`` from the ``kv_meta`` table --
so running every hour applies 1/24 of the daily rate, and coming back
online after 3 days produces 3 days' worth (clamped to
``decay_max_catchup_days``).

Replaces the legacy ``SessionController._memory_decay_loop`` daemon
thread; consolidating into the scheduler means decay shares the same
quiet-window gate as the promotion worker.

Schema v10 — also runs two cheap reclassification passes per tick:

  - ``future_plan`` rows that have stopped being future are flipped
    to ``past_event`` with a fresh ``relevance_until``. Two signals
    retire a plan: an ``event_time`` at least an hour into the past
    (the 1-hour buffer keeps a plan flagged "future" through the
    moment it's actually happening, so no premature flip if Aiko has
    the chat open while the user is at the gym), or an expired
    ``relevance_until`` (plus ``_CLOCKLESS_PLAN_GRACE``) for the plans
    the extractor could not pin to a clock at all -- "next week",
    "soon", "in the near future". Those clockless plans are the common
    case and without the second signal they never expire at all.
  - ``past_event`` rows whose ``relevance_until`` already passed
    are demoted to the ``archive`` tier so they stop crowding RAG
    while staying available for archive / reflection work.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.proactive.idle_worker import WorkSignal
from app.core.infra import timephrase
from app.core.memory.memory_store import _RELEVANCE_WINDOW

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.infra.settings import MemorySettings
    from app.core.infra.engagement_clock import EngagementClock


log = logging.getLogger("app.memory_decay_worker")


# Buffer between ``event_time`` passing and the future_plan -> past_event
# flip. Keeps the row "live" through the actual moment so retrieval
# bullet annotation still reads as "(planned for tonight 20:00)" while
# the user is actually at the gym.
_FUTURE_TO_PAST_BUFFER = timedelta(hours=1)
# Window past_event memories stay available in normal RAG before the
# decay worker demotes them to ``archive``. Read from the store's table
# rather than restated here: this worker rewrites ``relevance_until`` on
# every reclassify, so a private copy is a rule that can silently
# disagree with the one the writer applies (the H40 near-miss, one lane
# over).
_PAST_EVENT_RELEVANCE_WINDOW = (
    _RELEVANCE_WINDOW["past_event"] or timedelta(days=7)
)
# Slack past ``relevance_until`` before a plan the user never pinned to
# a clock is treated as history. Deliberately much longer than that
# column implies: for a clockless plan ``relevance_until`` is only
# ``created_at + 1 day``, a *retrieval* expiry chosen to keep a vague
# "next week" out of RAG rather than a claim that the plan is dead.
# Retiring on it directly would make "did the cookies ever happen?"
# unaskable after a single day. A fortnight is about how long a vague
# plan stays worth asking about, and it still retires the 30-to-60-day
# rows that were previously immortal.
_CLOCKLESS_PLAN_GRACE = timedelta(days=14)


class MemoryDecayWorker:
    """IdleWorker wrapping :meth:`MemoryStore.decay`.

    Also piggy-backs the F2 knowledge-gap expiry pass (90-day TTL on
    unresolved unpinned gaps) so we don't need a second worker just to
    sweep stale journal rows.
    """

    name = "memory_decay"

    # kv_meta anchor for the engagement-clock elapsed accounting: the
    # ``clock.total()`` value at the last successful decay run. Parallel
    # to the store's own wall-clock ``memory.last_decay_run_at`` anchor.
    _KV_LAST_DECAY_ENGAGEMENT = "memory.last_decay_engagement"

    def __init__(
        self,
        store: "MemoryStore",
        settings: "MemorySettings",
        *,
        knowledge_gap_store: "Any | None" = None,
        engagement_clock: "EngagementClock | None" = None,
        kv_get: "Any | None" = None,
        kv_set: "Any | None" = None,
    ) -> None:
        self._store = store
        self._settings = settings
        # F2: optional handle to the knowledge-gap store. Wired by
        # ``SessionController`` so the decay worker can also run the
        # 90-day expiry pass on the journal. ``None`` keeps tests and
        # lean deployments running without the extra hook.
        self._knowledge_gap_store = knowledge_gap_store
        # Shared engagement clock + kv_meta accessors for its anchor. When
        # all three are present (and ``memory_decay_use_engagement_clock``
        # is on) decay is driven by active-conversation time rather than
        # wall-clock, so absence / quiet stretches don't fade memories.
        # Missing any of them => today's wall-clock path.
        self._engagement_clock = engagement_clock
        self._kv_get = kv_get
        self._kv_set = kv_set

    @property
    def interval_seconds(self) -> float:
        return float(self._settings.decay_worker_interval_seconds)

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        # Feature flag only; the interval became the heartbeat (P36).
        return bool(self._settings.tiers_enabled)

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """How much decay has accrued since the last sweep. Never LLM.

        On the engagement-clock path this is the real win: decay is
        driven by *active conversation* time, so while the user is away
        there is genuinely nothing to decay and the old 30-minute timer
        was waking up to write zero rows all night.

        On the wall-clock path there is no cheap signal beyond elapsed
        time, so pressure just tracks the heartbeat. Reporting it anyway
        (rather than returning ``None``) is what keeps this pure-SQL
        worker in the compute lane instead of the LLM one.
        """
        if not self._settings.tiers_enabled:
            return WorkSignal(pressure=0.0, reason="tiers_disabled")

        engaged_days = self._peek_engaged_days()
        if engaged_days is not None:
            # Saturate at a full day of active conversation.
            return WorkSignal(
                pressure=max(0.0, min(1.0, engaged_days)),
                reason=f"{engaged_days:.3f} engaged days",
            )

        heartbeat = max(1.0, self.interval_seconds)
        elapsed_s = (
            heartbeat if last_run_at is None
            else (now - last_run_at).total_seconds()
        )
        return WorkSignal(
            pressure=max(0.0, min(1.0, elapsed_s / heartbeat)),
            reason="wall clock",
        )

    def _peek_engaged_days(self) -> float | None:
        """Read-only twin of :meth:`_engaged_elapsed`.

        ``None`` means "not on the engagement path". Crucially this
        never writes the baseline anchor -- that side effect belongs to
        an actual sweep, not to being asked whether one is worth doing.
        """
        clock = self._engagement_clock
        if (
            clock is None
            or self._kv_get is None
            or self._kv_set is None
            or not getattr(clock, "enabled", False)
            or not bool(
                getattr(self._settings, "memory_decay_use_engagement_clock", True)
            )
        ):
            return None
        try:
            raw = self._kv_get(self._KV_LAST_DECAY_ENGAGEMENT)
        except Exception:
            return None
        if raw is None:
            # No baseline yet; let the sweep run and write one.
            return 1.0
        try:
            anchor = float(raw)
        except (TypeError, ValueError):
            return None
        try:
            return float(
                clock.engaged_days_since(
                    anchor,
                    clamp_days=float(self._settings.decay_max_catchup_days),
                )
            )
        except Exception:
            return None

    def run(self) -> dict[str, Any]:
        if not self._settings.tiers_enabled:
            return {"skipped": True, "reason": "tiers_disabled"}
        rates = {
            "scratchpad": float(self._settings.decay_rate_scratchpad),
            "long_term": float(self._settings.decay_rate_long_term),
            "archive": float(self._settings.decay_rate_archive),
        }
        max_catchup = float(self._settings.decay_max_catchup_days)
        # When the engagement clock is wired + enabled, drive elapsed_days
        # from active-conversation time (clamped by the same catch-up cap)
        # and pass it explicitly so ``decay()`` skips its wall-clock
        # anchor. Otherwise ``elapsed_days=None`` keeps the exact
        # wall-clock behaviour. Computed *before* decay so we only advance
        # our engagement anchor once decay actually applied.
        engaged_days, engaged_now = self._engaged_elapsed(max_catchup)
        try:
            stats = self._store.decay(
                elapsed_days=engaged_days,
                decay_rates=rates,
                revival_coefficient=float(self._settings.revival_coefficient),
                revival_decay_per_day=float(self._settings.revival_decay_per_day),
                max_catchup_days=max_catchup,
            )
        except Exception:
            log.warning("memory decay failed", exc_info=True)
            raise
        # Advance the engagement anchor after a successful engagement-driven
        # sweep so the next run measures only the newly-accrued active time.
        if engaged_now is not None and self._kv_set is not None:
            try:
                self._kv_set(self._KV_LAST_DECAY_ENGAGEMENT, repr(engaged_now))
            except Exception:
                log.debug("engagement decay anchor write failed", exc_info=True)
        log.info("memory_decay sweep: %s", stats)
        # F2: piggyback gap expiry. Best-effort — if it fails, the
        # decay sweep result still counts as a successful tick.
        out: dict[str, Any] = dict(stats) if isinstance(stats, dict) else {}
        gap_store = self._knowledge_gap_store
        if gap_store is not None:
            try:
                pruned = gap_store.prune_expired()
                if pruned:
                    out["knowledge_gaps_expired"] = int(pruned)
                    log.info(
                        "memory_decay: expired %d stale knowledge gap(s)",
                        pruned,
                    )
            except Exception:
                log.debug("knowledge gap expiry failed", exc_info=True)
        # Schema v10: temporal reclassification. Best-effort — if it
        # fails the rest of the decay sweep still counts as a tick.
        try:
            stats_temporal = self._reclassify_temporal()
        except Exception:
            log.debug("temporal reclassification failed", exc_info=True)
            stats_temporal = {}
        out.update(stats_temporal)
        return out

    # ── engagement-clock elapsed accounting ──────────────────────────

    def _engaged_elapsed(
        self, max_catchup_days: float
    ) -> tuple[float | None, float | None]:
        """Return ``(elapsed_days, engaged_now)`` for the engagement path.

        ``elapsed_days is None`` means "use the wall-clock path" (clock
        disabled / not wired / setting off). Otherwise it's the active
        time elapsed since our anchor, clamped to ``max_catchup_days``;
        ``engaged_now`` is the current ``clock.total()`` to persist as the
        new anchor after a successful sweep (``None`` on the first run,
        whose baseline this method has already written).
        """
        clock = self._engagement_clock
        if (
            clock is None
            or self._kv_get is None
            or self._kv_set is None
            or not getattr(clock, "enabled", False)
            or not bool(
                getattr(self._settings, "memory_decay_use_engagement_clock", True)
            )
        ):
            return None, None
        try:
            raw = self._kv_get(self._KV_LAST_DECAY_ENGAGEMENT)
        except Exception:
            return None, None
        now_units = clock.total()
        if raw is None:
            # First engagement-driven run: store the baseline, apply no
            # decay this pass (mirrors decay()'s wall-clock first-run guard).
            try:
                self._kv_set(self._KV_LAST_DECAY_ENGAGEMENT, repr(now_units))
            except Exception:
                log.debug("engagement decay baseline write failed", exc_info=True)
            return 0.0, None
        try:
            anchor = float(raw)
        except (TypeError, ValueError):
            anchor = now_units
        elapsed = clock.engaged_days_since(anchor, clamp_days=max_catchup_days)
        return elapsed, now_units

    # ── v10 temporal passes ──────────────────────────────────────────

    def _reclassify_temporal(self) -> dict[str, int]:
        """Run the two v10 temporal reclassification passes.

        Returns counters under stable keys so a future telemetry hook
        can graph how often the worker is actually doing useful work.
        """
        now = timephrase.utcnow()
        future_cutoff = (now - _FUTURE_TO_PAST_BUFFER).isoformat()
        relevance_cutoff = now.isoformat()
        plan_cutoff = (now - _CLOCKLESS_PLAN_GRACE).isoformat()

        out = {
            "future_plans_to_past": 0,
            "past_events_archived": 0,
        }

        # Pass 1: future_plan -> past_event. There are two ways a plan
        # stops being a plan. An ``event_time`` that precedes ``now -
        # buffer`` is the precise one, and the buffer means a plan
        # currently happening keeps its "(planned for tonight 20:00)"
        # framing in retrieval until the moment is over. But the
        # extractor can only pin an event_time when the user named a
        # time: "next week" / "soon" / "in the near future" all produce
        # a plan with no clock on it, and those rows used to be
        # immortal -- forever a pending future, long after the thing
        # happened, still feeding forward-curiosity questions like
        # "did we ever manage to reschedule that evening date?" about
        # an evening that had already happened. ``relevance_until`` is
        # derived for *every* future_plan, so it retires the clockless
        # ones -- after a grace window, since for them that column is
        # only ``created_at + 1 day`` and means "stop surfacing this in
        # RAG", not "this plan is over".
        overdue = self._overdue_future_plans(future_cutoff, plan_cutoff)
        for mem in overdue:
            try:
                event_dt = self._parse_iso(mem.event_time)
                # Anchor the new relevance window on event_time so a
                # plan that slipped recognised hours ago still gets
                # the full retrospective window from when it actually
                # happened, not when the worker noticed. With no
                # event_time, anchor on the moment the plan went stale
                # rather than on ``now``: a plan that expired weeks ago
                # should fall straight through pass 2 into the archive,
                # not come back as a freshly-relevant past event.
                anchor = event_dt or self._parse_iso(mem.relevance_until) or now
                new_relevance = (anchor + _PAST_EVENT_RELEVANCE_WINDOW).isoformat()
                self._store.reclassify(
                    mem.id,
                    temporal_type="past_event",
                    relevance_until=new_relevance,
                )
                out["future_plans_to_past"] += 1
            except Exception:
                log.debug(
                    "reclassify future_plan id=%s failed", mem.id, exc_info=True
                )

        # Pass 2: past_event -> archive (tier demotion). Cheap because
        # the mirror snapshot is already filtered down to the candidates.
        try:
            expired = self._store.list_by_temporal_type(
                "past_event",
                relevance_until_before=relevance_cutoff,
            )
        except Exception:
            log.debug("list expired past_events failed", exc_info=True)
            expired = []
        for mem in expired:
            if mem.tier == "archive":
                # Already demoted on a previous tick.
                continue
            try:
                self._store.update(mem.id, tier="archive")
                out["past_events_archived"] += 1
            except Exception:
                log.debug(
                    "archive past_event id=%s failed", mem.id, exc_info=True
                )

        if out["future_plans_to_past"] or out["past_events_archived"]:
            log.info(
                "memory_decay temporal: %d future->past, %d past->archive",
                out["future_plans_to_past"],
                out["past_events_archived"],
            )
        return out

    def _overdue_future_plans(
        self, event_cutoff: str, plan_cutoff: str,
    ) -> list["Memory"]:
        """Future plans that have stopped being future, by either signal.

        Deduped by id, event-time hits first, so a row carrying both
        signals is reclassified once and anchored on its event_time.
        """
        found: dict[object, "Memory"] = {}
        for kwargs in (
            {"event_time_before": event_cutoff},
            {"relevance_until_before": plan_cutoff},
        ):
            try:
                rows = self._store.list_by_temporal_type("future_plan", **kwargs)
            except Exception:
                log.debug("list future_plans failed (%s)", kwargs, exc_info=True)
                continue
            for mem in rows:
                found.setdefault(mem.id, mem)
        return list(found.values())

    @staticmethod
    def _parse_iso(value: str | None) -> datetime | None:
        """Best-effort ISO-8601 -> aware datetime parser.

        Mirrors the parsing done in :mod:`app.core.rag.rag_retriever`; kept
        local so the worker doesn't import the heavyweight retriever
        module just for one helper.
        """
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


__all__ = ["MemoryDecayWorker"]
