"""Garden visit worker — Aiko wanders outside to tend the plants.

Background :class:`IdleWorker` that, during a quiet daylight window,
moves Aiko's world state to ``garden``, waters every plant there, and
auto-harvests any that are ripe. After a short visit it pushes her
back to ``desk`` so the user notices "she was out in the garden" without
her parking there forever.

Two-phase design (single worker, not two):
  * Phase 1 — **outbound**: when she's not in the garden and the
    cooldown elapsed, move to garden, water + harvest, stamp
    ``return_at`` into a kv_meta key so we know when to pull her back.
  * Phase 2 — **inbound**: when she's already in the garden and
    ``return_at`` is past, move her back to ``desk`` and clear the
    marker.

Behaviour is silent — no chat message, no proactive nudge. The user
sees her location change in the World tab and notices new produce in
the kitchenette next time they look. Aiko's persona prompt has
guidance for mentioning the harvest casually if the moment calls for
it on the next turn.

The cooldown jitter (1.5-3.5h) keeps visits from feeling metronomic.
The daylight gate uses :func:`app.core.affect.circadian.compute` so it
respects the user's locale.
"""
from __future__ import annotations

import logging
import random
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready


if TYPE_CHECKING:
    from app.core.world.world_store import WorldStore


log = logging.getLogger("app.garden_visit_worker")


# Periods of the day during which gardening feels right. Outside of
# this window the worker is a no-op (no 3 a.m. tomato fussing).
_DAYLIGHT_PERIODS: frozenset[str] = frozenset(
    {"morning", "midday", "afternoon", "early_morning"}
)

# Cooldown window (seconds) between two outbound visits.
_MIN_VISIT_COOLDOWN_S = 1.5 * 3600
_MAX_VISIT_COOLDOWN_S = 3.5 * 3600

# How long she lingers in the garden before walking back. Short enough
# that the user sees the round trip within one session, long enough
# that the visit feels real.
_VISIT_DURATION_MINUTES = 6.0

# Slug of the location she returns to after the visit. Falls back to
# the first available location if ``desk`` was renamed/removed.
_RETURN_SLUG = "desk"


class GardenVisitWorker:
    """IdleWorker that wanders Aiko between her room and the garden."""

    name = "garden_visit"

    def __init__(
        self,
        store: "WorldStore",
        *,
        notify: Callable[[dict[str, Any]], None] | None = None,
        interval_seconds: float = 1800.0,
        kv_get: Callable[[str], str | None] | None = None,
        kv_set: Callable[[str, str], None] | None = None,
        rng: random.Random | None = None,
        circadian_period_provider: Callable[[], str] | None = None,
    ) -> None:
        self._store = store
        self._notify = notify
        self._interval_seconds = float(interval_seconds)
        # Per-instance bookkeeping — return_at + next_eligible are
        # stored here when no kv_get/kv_set are supplied so tests can
        # exercise the two-phase logic without a real ChatDatabase.
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._mem_kv: dict[str, str] = {}
        self._rng = rng or random.Random()
        self._circadian_period_provider = circadian_period_provider

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    # ── readiness ───────────────────────────────────────────────────

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        ):
            return False
        garden = self._store.get_location("garden")
        if garden is None:
            return False
        try:
            state = self._store.get_state()
        except Exception:
            return False
        in_garden = state.location_id == garden.id
        # Phase 2 — already in the garden: ready only when return_at is past.
        if in_garden:
            return_at = self._load_return_at()
            return return_at is not None and now >= return_at
        # Phase 1 — outside the garden: respect daylight + cooldown.
        if not self._is_daylight(now):
            return False
        next_eligible = self._load_next_eligible()
        if next_eligible is not None and now < next_eligible:
            return False
        return True

    # ── main step ───────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        from app.core.world.world_store import promote_stage  # local to avoid cycle

        now = datetime.now(timezone.utc)
        garden = self._store.get_location("garden")
        if garden is None:
            return {"skipped": True, "reason": "no_garden"}
        try:
            state = self._store.get_state()
        except Exception:
            return {"skipped": True, "reason": "state_unavailable"}
        in_garden = state.location_id == garden.id
        if in_garden:
            return self._return_home(now=now)
        return self._visit_garden(garden=garden, now=now)

    # ── phase 1 — visit ─────────────────────────────────────────────

    def _visit_garden(self, *, garden: Any, now: datetime) -> dict[str, Any]:
        # Move + emit a state patch.
        new_state = self._store.set_state(
            location_id=garden.id,
            posture="standing",
            activity="stretching",
        )
        self._broadcast({"state": new_state.to_dict()})
        watered: list[dict[str, Any]] = []
        harvested: list[dict[str, Any]] = []
        try:
            plants = self._store.list_items(
                location_id=garden.id, kind="plant",
            )
        except Exception:
            plants = []
        for plant in plants:
            stage = str((plant.state or {}).get("stage", "")).lower()
            if stage == "mature":
                try:
                    result = self._store.harvest_plant(plant.id, now=now)
                except Exception:
                    log.debug(
                        "garden visit: harvest_plant raised id=%s",
                        plant.id,
                        exc_info=True,
                    )
                    result = None
                if result is None:
                    continue
                harvested.append(result)
                # Broadcast the produce + (re-)plant rows so the UI
                # reconciles in one pass. Annual paths emit a delete +
                # a fresh seed; perennial paths emit a plant update.
                produce = (result.get("produce") or {}).get("item")
                if produce is not None:
                    self._broadcast({"item": produce})
                if result["plant"].get("deleted"):
                    self._broadcast({"deleted_item_id": int(plant.id)})
                else:
                    # Plant was reset (perennial) — re-fetch and emit.
                    refreshed = self._store.get_item(plant.id)
                    if refreshed is not None:
                        self._broadcast({"item": refreshed.to_dict()})
                seed = result.get("seed")
                if seed is not None and seed.get("item") is not None:
                    self._broadcast({"item": seed["item"]})
                continue
            try:
                refreshed = self._store.water_plant(plant.id, now=now)
            except Exception:
                log.debug(
                    "garden visit: water_plant raised id=%s",
                    plant.id,
                    exc_info=True,
                )
                refreshed = None
            if refreshed is None:
                continue
            watered.append(
                {"id": int(plant.id), "name": plant.name, "stage": stage}
            )
            self._broadcast({"item": refreshed.to_dict()})
        # Stamp return_at + a fresh next_eligible jitter so the worker
        # exits the cooldown organically after the next round-trip.
        return_at = now + timedelta(minutes=_VISIT_DURATION_MINUTES)
        self._save_return_at(return_at)
        self._save_next_eligible(self._pick_next_eligible(now))
        result = {
            "phase": "outbound",
            "watered": watered,
            "harvested": [
                {
                    "plant": h["plant"],
                    "produce_name": h["produce"]["name"],
                    "quantity": h["produce"]["quantity"],
                }
                for h in harvested
            ],
            "return_at": return_at.isoformat(),
        }
        if watered or harvested:
            log.info("garden_visit outbound: %s", result)
        return result

    # ── phase 2 — return home ──────────────────────────────────────

    def _return_home(self, *, now: datetime) -> dict[str, Any]:
        target = self._store.get_location(_RETURN_SLUG)
        if target is None:
            locations = [
                l for l in self._store.list_locations() if l.slug != "garden"
            ]
            target = locations[0] if locations else None
        target_id = target.id if target is not None else None
        new_state = self._store.set_state(
            location_id=target_id,
            posture="sitting",
            activity="idle",
        )
        self._broadcast({"state": new_state.to_dict()})
        self._save_return_at(None)
        log.info(
            "garden_visit inbound: returned to %s",
            getattr(target, "slug", None),
        )
        return {
            "phase": "inbound",
            "returned_to_slug": getattr(target, "slug", None),
        }

    # ── helpers ─────────────────────────────────────────────────────

    def _is_daylight(self, now: datetime) -> bool:
        period = self._current_period(now)
        return period in _DAYLIGHT_PERIODS

    def _current_period(self, now: datetime) -> str:
        if self._circadian_period_provider is not None:
            try:
                return str(self._circadian_period_provider() or "")
            except Exception:
                pass
        try:
            from app.core.affect.circadian import compute

            state = compute(now.astimezone() if now.tzinfo else now)
            return str(state.period)
        except Exception:
            return ""

    def _pick_next_eligible(self, now: datetime) -> datetime:
        jitter = self._rng.uniform(
            _MIN_VISIT_COOLDOWN_S, _MAX_VISIT_COOLDOWN_S
        )
        return now + timedelta(seconds=jitter)

    def _broadcast(self, patch: dict[str, Any]) -> None:
        if self._notify is None:
            return
        try:
            self._notify(patch)
        except Exception:
            log.debug("garden_visit notify raised", exc_info=True)

    # ── kv persistence (mirrors IdleWorkerScheduler style) ──────────

    _RETURN_KEY = "garden_visit.return_at"
    _NEXT_KEY = "garden_visit.next_eligible_at"

    def _kv_read(self, key: str) -> str | None:
        if self._kv_get is not None:
            try:
                return self._kv_get(key)
            except Exception:
                return None
        return self._mem_kv.get(key)

    def _kv_write(self, key: str, value: str | None) -> None:
        if value is None:
            if self._kv_set is not None:
                try:
                    self._kv_set(key, "")
                except Exception:
                    pass
            self._mem_kv.pop(key, None)
            return
        if self._kv_set is not None:
            try:
                self._kv_set(key, value)
                return
            except Exception:
                pass
        self._mem_kv[key] = value

    def _load_return_at(self) -> datetime | None:
        raw = self._kv_read(self._RETURN_KEY)
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    def _save_return_at(self, when: datetime | None) -> None:
        self._kv_write(
            self._RETURN_KEY,
            when.isoformat() if when is not None else None,
        )

    def _load_next_eligible(self) -> datetime | None:
        raw = self._kv_read(self._NEXT_KEY)
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    def _save_next_eligible(self, when: datetime | None) -> None:
        self._kv_write(
            self._NEXT_KEY,
            when.isoformat() if when is not None else None,
        )


__all__ = ["GardenVisitWorker"]
