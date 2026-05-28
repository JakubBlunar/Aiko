"""Schedule follow-up nudges for user-mentioned future plans (schema v10).

When the user tells Aiko about something upcoming ("gym tonight at 8",
"job interview on Thursday"), the :class:`MemoryExtractor` writes that
as a memory with ``temporal_type='future_plan'`` and an absolute
``event_time``. The :class:`MemoryDecayWorker` later flips the row to
``past_event`` once the time has passed.

The piece in between — Aiko proactively asking "how was it?" at the
*right* moment, not the next time the user shows up — is what this
worker covers. It runs through the shared :class:`IdleWorkerScheduler`
so it inherits the quiet-window gate (no fighting for GPU during a
turn).

Behaviour:

  - On each tick, scan ``MemoryStore`` for ``future_plan`` rows whose
    ``event_time`` is within a short window centred on now (default
    ±30 minutes). The window catches a tick that fires shortly after
    the moment without re-triggering for plans that already had their
    callback fired.
  - For each match, write a single :class:`PreparedNudge` into the
    store the :class:`NarrativeWeaver` normally fills. The nudge
    framing tells Aiko to ask retrospectively *if it fits the flow*,
    not as the conversation opener — the persona rule from
    ``data/persona/aiko_companion.txt`` covers the rest.
  - Stamp ``metadata.followup_fired_at`` on the memory so subsequent
    ticks skip it. Idempotent.

Failure modes are tolerated everywhere: a missing memory store, a
malformed ``event_time``, a write failure on the nudge store — all
get logged at debug and the worker moves on. The worst outcome is a
missed callback, never a corrupt DB row.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.core.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory_store import Memory, MemoryStore
    from app.core.prepared_nudge import PreparedNudgeStore


log = logging.getLogger("app.follow_up_worker")


# Window around ``event_time`` during which a tick will fire the
# nudge. Wide enough that a typical 5-15 minute scheduler interval
# catches the moment, narrow enough that a plan only triggers once.
_DEFAULT_LOOKAHEAD = timedelta(minutes=30)
_DEFAULT_LOOKBACK = timedelta(hours=4)
# Maximum nudges fired per sweep so a backlog (e.g. after a long
# offline gap) doesn't flood the user with check-ins.
_DEFAULT_MAX_PER_RUN = 3


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


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


def _humanize_clock(when: datetime) -> str:
    """Render an event_time as a short, friendly clock string."""
    return when.astimezone().strftime("%H:%M").lstrip("0") or "now"


def _compose_nudge_text(
    *,
    user_display_name: str,
    memory: "Memory",
    event_time: datetime,
) -> str:
    """Build the line stored in the prepared-nudge slot for this plan.

    Intentionally framed as a *contingent* nudge ("if the conversation
    drifts there"), not an opener — the persona rule reinforces this.
    Stays under the 480-char store cap by truncating long memory
    contents to ~120 chars.
    """
    snippet = (memory.content or "").strip()
    if len(snippet) > 120:
        snippet = snippet[:117].rsplit(" ", 1)[0] + "…"
    clock = _humanize_clock(event_time)
    return (
        f"{user_display_name} mentioned earlier: \"{snippet}\" — that was "
        f"around {clock}. If the conversation drifts there, ask how it went "
        "naturally. Don't open with it."
    )


class FollowUpWorker:
    """IdleWorker that fires prepared-nudge callbacks for future plans."""

    name = "follow_up"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        prepared_nudge_store: "PreparedNudgeStore",
        user_id_provider,
        user_display_name_provider,
        interval_seconds: float = 300.0,
        ttl_seconds: float = 1800.0,
        lookahead: timedelta = _DEFAULT_LOOKAHEAD,
        lookback: timedelta = _DEFAULT_LOOKBACK,
        max_per_run: int = _DEFAULT_MAX_PER_RUN,
    ) -> None:
        self._memory_store = memory_store
        self._nudge_store = prepared_nudge_store
        self._user_id_provider = user_id_provider
        self._user_display_name_provider = user_display_name_provider
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._ttl_seconds = max(60.0, float(ttl_seconds))
        self._lookahead = lookahead
        self._lookback = lookback
        self._max_per_run = max(1, int(max_per_run))

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        now = _utcnow()
        try:
            user_id = self._resolve_user_id()
            user_name = self._resolve_user_name()
        except Exception:
            log.debug("follow_up: identity resolution failed", exc_info=True)
            return {"fired": 0, "skipped_no_user": True}
        if not user_id:
            return {"fired": 0, "skipped_no_user": True}

        try:
            candidates = self._memory_store.list_by_temporal_type(
                "future_plan",
            )
        except Exception:
            log.debug("follow_up: list future_plan failed", exc_info=True)
            return {"fired": 0, "errored": True}

        fired = 0
        considered = 0
        skipped_already_fired = 0
        skipped_out_of_window = 0
        for mem in candidates:
            if fired >= self._max_per_run:
                break
            considered += 1
            metadata = mem.metadata or {}
            if metadata.get("followup_fired_at"):
                skipped_already_fired += 1
                continue
            event_dt = _parse_iso(mem.event_time)
            if event_dt is None:
                # Plans without a precise event_time don't get a
                # callback — there's no moment to anchor to.
                skipped_out_of_window += 1
                continue
            delta = event_dt - now
            if delta > self._lookahead:
                # Still too far in the future.
                skipped_out_of_window += 1
                continue
            if delta < -self._lookback:
                # Too far in the past — the decay worker will flip
                # the row to past_event and we'll never need to
                # follow up. Mark it fired-equivalent so we stop
                # scanning it on subsequent ticks.
                self._mark_fired(mem, when=now, dropped=True)
                continue

            try:
                nudge_text = _compose_nudge_text(
                    user_display_name=user_name,
                    memory=mem,
                    event_time=event_dt,
                )
                self._nudge_store.upsert(
                    user_id,
                    text=nudge_text,
                    source_kind="callback",
                    source_id=str(mem.id),
                    ttl_seconds=self._ttl_seconds,
                )
            except Exception:
                log.debug(
                    "follow_up: nudge upsert failed for memory id=%s",
                    mem.id,
                    exc_info=True,
                )
                continue

            self._mark_fired(mem, when=now)
            fired += 1
            log.info(
                "follow_up nudge primed for memory id=%s (%s @ %s)",
                mem.id,
                (mem.content or "")[:80],
                event_dt.isoformat(),
            )

        return {
            "fired": fired,
            "considered": considered,
            "skipped_already_fired": skipped_already_fired,
            "skipped_out_of_window": skipped_out_of_window,
        }

    # ── helpers ──────────────────────────────────────────────────────

    def _resolve_user_id(self) -> str:
        try:
            return str(self._user_id_provider() or "").strip()
        except Exception:
            return ""

    def _resolve_user_name(self) -> str:
        try:
            name = str(self._user_display_name_provider() or "").strip()
        except Exception:
            name = ""
        return name or "the user"

    def _mark_fired(
        self,
        memory: "Memory",
        *,
        when: datetime,
        dropped: bool = False,
    ) -> None:
        """Stamp ``metadata.followup_fired_at`` so we don't fire again.

        ``dropped=True`` records that the moment is too far in the
        past for a useful callback (the decay worker is expected to
        flip the row to past_event soon anyway). The metadata key is
        the same either way — we just want subsequent ticks to skip
        the row.
        """
        try:
            self._memory_store.update(
                memory.id,
                metadata={
                    "followup_fired_at": when.isoformat(),
                    "followup_dropped": bool(dropped),
                },
                metadata_merge=True,
            )
        except Exception:
            log.debug(
                "follow_up: mark_fired update failed for id=%s",
                memory.id,
                exc_info=True,
            )


__all__ = ["FollowUpWorker"]
