"""K73 — Shared-ritual worker (silent producer).

During a quiet window this worker reads recent user message timing +
content, derives a coarse conversation-arc *shape* per
``(weekday, bucket, ISO-week)`` session (reusing the K3 local-time
bucket helpers and the pure ``conversation_arc.estimate_arc`` regex), and
folds the ``(weekday, bucket, shape)`` slots that have **genuinely
recurred** across enough weeks into the persisted ``aiko.shared_rituals``
kv store (via the pure :func:`shared_ritual.merge_rituals`), then queues
a cue for each newly-named ritual. The consumer
:meth:`InnerLifeProvidersMixin._render_shared_ritual_block` claims one as
a warm acknowledgment. This worker never speaks.

Two stores, two jobs. ``aiko.shared_rituals`` is the durable model —
which patterns exist, how many weeks each has run, which have been named
out loud — and it long outlives any one cue. ``cue_pool`` holds the
*saying* of it: one offer per ritual, retried if Aiko never took it, and
matched against her reply afterwards so "named" means she actually named
it. The ``acknowledged`` flag is set when the cue is published, since
from the store's point of view the question is only whether this ritual
still needs an offer, and the pool answers everything after that.

Idempotent by construction: it keeps the ritual store fresh and refuses
to re-publish anything the pool already holds a cue for, so it needs no
wall-clock cooldown of its own. The spacing between acknowledgments is
the type's ``surface_cooldown_hours``. Every failure path is swallowed
and logged at debug.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.conversation.conversation_arc import estimate_arc
from app.core.infra.schedule_learner import (
    _HOUR_TO_BUCKET,
    _iso_week_key,
    _weekday_name_local,
)
from app.core.proactive.cue_producer import CueProducer, StoreProvider
from app.core.proactive.cue_store import normalise_subject
from app.core.proactive.idle_worker import WorkSignal
from app.core.relationship import shared_ritual as _sr
from app.core.infra import timephrase


if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase


log = logging.getLogger("app.shared_ritual_worker")


def _utcnow() -> datetime:
    return timephrase.utcnow()


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


class SharedRitualWorker:
    """IdleWorker that mines + names dyadic conversational rituals."""

    name = "shared_ritual"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        enabled_provider: Callable[[], bool] | None = None,
        cue_store_provider: StoreProvider | None = None,
        user_name_provider: Callable[[], str] | None = None,
        interval_seconds: float = 86400.0,
        window_days: int = 56,
        min_weeks: int = _sr.DEFAULT_MIN_WEEKS,
        min_share: float = _sr.DEFAULT_MIN_SHARE,
        max_active: int = _sr.DEFAULT_MAX_ACTIVE,
        min_messages: int = 30,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._enabled_provider = enabled_provider
        self._cues = CueProducer("shared_ritual", cue_store_provider)
        self._user_name_provider = user_name_provider
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._window_days = max(7, int(window_days))
        self._min_weeks = max(1, int(min_weeks))
        self._min_share = max(0.0, min(1.0, float(min_share)))
        self._max_active = max(1, int(max_active))
        self._min_messages = max(1, int(min_messages))
        self._clock = clock or _utcnow
        self._force_next = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        # Hard veto only. The interval is the heartbeat now; how much
        # stock is on the shelf decides the rest.
        return self._enabled()

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        """Pressure from having no acknowledgment ready to offer.

        The sweep that refreshes the ritual store and the drafting of a
        cue are the same pass, so this paces both. That is the right
        trade: rituals are measured in weeks, and a store refreshed only
        when Aiko has nothing to say is still refreshed far more often
        than its subject matter changes.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        return self._cues.demand()

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"updated": 0, "disabled": True}

        now = self._clock()
        forced = self._force_next
        self._force_next = False

        rows = self._fetch_rows(now)
        if not forced and len(rows) < self._min_messages:
            return {
                "updated": 0,
                "below_min_messages": True,
                "messages": len(rows),
            }

        sessions = self._group_sessions(rows)
        slot_weeks: dict[tuple[str, str, str], set] = {}
        for (weekday, bucket, week), arcs in sessions.items():
            shape = _sr.dominant_shape(arcs)
            slot_weeks.setdefault((weekday, bucket, shape), set()).add(week)

        total_weeks = max(1, (self._window_days + 6) // 7)
        candidates = _sr.detect_rituals(
            slot_weeks,
            total_weeks=total_weeks,
            min_weeks=self._min_weeks,
            min_share=self._min_share,
            max_rituals=self._max_active,
        )

        existing = _sr.load_rituals(self._chat_db.kv_get)
        now_date = now.astimezone().strftime("%Y-%m-%d")
        merged, new_keys = _sr.merge_rituals(
            existing, candidates, now_date=now_date,
            max_active=self._max_active,
        )
        merged, drafted = self._publish(merged)
        _sr.save_rituals(self._chat_db.kv_set, merged)

        log.info(
            "shared-ritual sweep: messages=%d sessions=%d candidates=%d "
            "new=%s stored=%d drafted=%d",
            len(rows),
            len(sessions),
            len(candidates),
            new_keys or "[]",
            len(merged),
            drafted,
        )
        return {
            "updated": 1,
            "messages": len(rows),
            "candidates": len(candidates),
            "new_keys": new_keys,
            "stored": len(merged),
            "drafted": drafted,
        }

    # ── publishing ───────────────────────────────────────────────────

    def _publish(
        self, rituals: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], int]:
        """Queue a cue for the strongest ritual still owed an offer.

        One per sweep. Rituals qualify in a trickle -- weeks apart -- so
        a loop here would only ever matter on the first sweep after a
        long backfill, and naming two of them in one evening is exactly
        the announcement the copy forbids.
        """
        pick = _sr.pick_unacknowledged(rituals)
        if pick is None:
            return rituals, 0
        label = str(pick.get("label") or "").strip()
        if not label:
            return rituals, 0
        if normalise_subject(label) in self._cues.spoken_for():
            return rituals, 0
        text = _sr.render_inner_life_block(
            pick, user_display_name=self._user_name(),
        )
        if not text:
            return rituals, 0
        key = str(pick.get("key") or "")
        cue_id = self._cues.publish(
            label,
            text,
            payload={
                "key": key,
                "label": label,
                "weeks_seen": int(pick.get("weeks_seen") or 0),
            },
        )
        if not cue_id:
            return rituals, 0
        log.info("shared-ritual drafted: key=%s label=%r", key, label)
        return _sr.mark_acknowledged(rituals, key), 1

    # ── helpers ──────────────────────────────────────────────────────

    def _fetch_rows(self, now: datetime) -> list[tuple[Any, ...]]:
        cutoff = now - timedelta(days=self._window_days)
        try:
            return self._chat_db.execute_fetchall(
                "SELECT created_at, content FROM messages "
                "WHERE role = 'user' AND created_at >= ? "
                "ORDER BY created_at ASC",
                (cutoff.astimezone(timezone.utc).isoformat(),),
            )
        except Exception:
            log.debug("shared-ritual SELECT failed", exc_info=True)
            return []

    def _group_sessions(
        self, rows: list[tuple[Any, ...]],
    ) -> dict[tuple[str, str, tuple[int, int]], list[str | None]]:
        """Group message arcs by ``(weekday, bucket, iso_week)`` session."""
        sessions: dict[tuple[str, str, tuple[int, int]], list[str | None]] = {}
        for row in rows or []:
            if not row:
                continue
            dt = _parse_iso(row[0])
            if dt is None:
                continue
            local = dt.astimezone()
            weekday = _weekday_name_local(local)
            bucket = _HOUR_TO_BUCKET.get(local.hour, "late")
            week = _iso_week_key(local)
            content = row[1] if len(row) > 1 else ""
            arc = estimate_arc(str(content or ""))
            sessions.setdefault((weekday, bucket, week), []).append(arc)
        return sessions

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _user_name(self) -> str:
        if self._user_name_provider is None:
            return "them"
        try:
            return str(self._user_name_provider() or "them")
        except Exception:
            return "them"

    def force_next(self) -> None:
        """Arm a one-shot bypass of the min-messages floor on next run()."""
        self._force_next = True
