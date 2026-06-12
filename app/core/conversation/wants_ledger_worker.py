"""K52 — wants-ledger feeder worker.

IdleWorker that keeps :mod:`app.core.conversation.wants_ledger`
stocked from producers that already exist. No LLM call — ingestion is
deterministic:

- **Curiosity seeds** (K9, ``kind="curiosity_seed"`` memories) —
  unconsumed seeds become ``ask`` wants ("bring up what you've been
  curious about: ...").
- **Forward-curiosity questions** (K34 journal ring on kv_meta) —
  the newest drafted wonderings become ``ask`` wants ("ask {user}
  ...").
- **Active goals** (K1 ``GoalStore``) — the newest active goals
  become low-pressure ``steer`` wants ("steer toward something of
  yours: ...").

Dedup / capping / re-entry cooldown all live in the pure module's
:func:`add_want`; the worker just walks the producers and offers each
candidate. The worker also applies pressure growth each tick so the
ledger keeps maturing even when no chat turns happen (the provider
applies growth lazily too — both paths land on the same pure
function, so semantics are identical).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.conversation import wants_ledger
from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.goals.goal_store import GoalStore
    from app.core.memory.memory_store import MemoryStore


log = logging.getLogger("app.wants_ledger_worker")


# Per-run ingestion caps — keep each tick cheap and let the ledger
# fill over hours, not in one burst.
_MAX_SEEDS_PER_RUN = 2
_MAX_FORWARD_PER_RUN = 2
_MAX_GOALS_PER_RUN = 2
# Goal-derived wants start lower than ask/share wants: steering toward
# a goal is a background pull, not a fresh itch.
_GOAL_INITIAL_PRESSURE = 0.05


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WantsLedgerWorker:
    """IdleWorker feeding the K52 wants ledger from existing stores."""

    name = "wants_ledger"

    def __init__(
        self,
        *,
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        user_display_name_provider: Callable[[], str],
        memory_store: "MemoryStore | None" = None,
        goal_store: "GoalStore | None" = None,
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 3600.0,
        cap: int = 8,
        growth_per_day: float = 0.25,
        max_age_days: float = 14.0,
        reentry_cooldown_days: float = 5.0,
    ) -> None:
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_display_name_provider = user_display_name_provider
        self._memory_store = memory_store
        self._goal_store = goal_store
        self._enabled_provider = enabled_provider
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._cap = max(1, int(cap))
        self._growth_per_day = max(0.0, float(growth_per_day))
        self._max_age_days = max(1.0, float(max_age_days))
        self._reentry_cooldown_days = max(0.0, float(reentry_cooldown_days))

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
                    return {"added": 0, "disabled": True}
            except Exception:
                pass
        now = _utcnow()
        state = wants_ledger.deserialize(self._kv_get_safe(wants_ledger.KV_WANTS_LEDGER))
        state = wants_ledger.apply_growth(
            state, now,
            growth_per_day=self._growth_per_day,
            max_age_days=self._max_age_days,
            reentry_cooldown_days=self._reentry_cooldown_days,
        )

        added = 0
        name = self._safe_name()
        for text, kind, source, ref, pressure in self._candidates(name):
            state, ok = wants_ledger.add_want(
                state,
                text=text,
                kind=kind,
                source=source,
                source_ref=ref,
                now=now,
                cap=self._cap,
                initial_pressure=pressure,
            )
            if ok:
                added += 1
                log.info("wants-ledger added: source=%s ref=%s", source, ref)

        try:
            self._kv_set(wants_ledger.KV_WANTS_LEDGER, wants_ledger.serialize(state))
        except Exception:
            log.debug("wants ledger persist failed", exc_info=True)
        return {"added": added, "live": len(state.wants)}

    # ── candidate producers ──────────────────────────────────────────

    def _candidates(self, name: str) -> list[tuple[str, str, str, str, float]]:
        """Yield ``(text, kind, source, source_ref, initial_pressure)``."""
        out: list[tuple[str, str, str, str, float]] = []

        # 1. Curiosity seeds (oldest unconsumed first — same fairness
        # rule as the K9 surfacing block).
        memory = self._memory_store
        if memory is not None:
            try:
                seeds = list(memory.iter_by_kind("curiosity_seed"))
            except Exception:
                log.debug("wants: seed iter failed", exc_info=True)
                seeds = []
            active = [
                s for s in seeds
                if not (s.metadata or {}).get("consumed_at")
                and s.tier != "archive"
            ]
            active.sort(key=lambda m: m.created_at or "")
            for seed in active[:_MAX_SEEDS_PER_RUN]:
                topic = ((seed.metadata or {}).get("topic") or seed.content or "").strip()
                if not topic:
                    continue
                out.append((
                    f"bring up what you've been curious about: {_clip(topic)}",
                    "ask",
                    "curiosity_seed",
                    f"seed:{seed.id}",
                    0.15,
                ))

        # 2. Forward-curiosity journal (newest entries first).
        try:
            from app.core.proactive.forward_curiosity_worker import load_questions

            ring = load_questions(self._kv_get)
        except Exception:
            log.debug("wants: forward-curiosity load failed", exc_info=True)
            ring = []
        for entry in list(reversed(ring))[:_MAX_FORWARD_PER_RUN]:
            question = str(entry.get("question") or "").strip()
            if not question:
                continue
            ref = str(entry.get("source_id") or entry.get("at") or "").strip()
            if not ref:
                continue
            out.append((
                f"ask {name} {_clip(question)}",
                "ask",
                "forward_curiosity",
                f"fc:{ref}",
                0.15,
            ))

        # 3. Active goals (newest first, low starting pressure).
        goals = self._goal_store
        if goals is not None:
            try:
                rows = goals.list_active()
            except Exception:
                log.debug("wants: goal list failed", exc_info=True)
                rows = []
            for goal in rows[:_MAX_GOALS_PER_RUN]:
                summary = (goal.content or "").strip()
                if not summary:
                    continue
                out.append((
                    f"steer toward something of yours: {_clip(summary)}",
                    "steer",
                    "goal",
                    f"goal:{goal.id}",
                    _GOAL_INITIAL_PRESSURE,
                ))
        return out

    # ── helpers ──────────────────────────────────────────────────────

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _safe_name(self) -> str:
        try:
            return (self._user_display_name_provider() or "them").strip() or "them"
        except Exception:
            return "them"


def _clip(text: str, limit: int = 140) -> str:
    cleaned = " ".join(str(text).split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip(",;: ") + "…"


__all__ = ["WantsLedgerWorker"]
