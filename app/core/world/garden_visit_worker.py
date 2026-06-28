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
from app.core.world.idle_activity_worker import (
    AWAY_ACTIVITIES_JOURNAL_KEY,
    append_journal,
)


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

# Default visit-duration jitter (minutes) when no settings are supplied.
# How long she lingers in the garden before walking back: short enough
# that the user sees the round trip within one session, long enough
# that the visit feels real.
_VISIT_MIN_MINUTES = 4.0
_VISIT_MAX_MINUTES = 10.0
# Back-compat: the midpoint reads like the old fixed 6-minute linger and
# is used as the fallback when the jitter bounds collapse.
_VISIT_DURATION_MINUTES = 6.0

# Non-gardening outdoor beats (H15) — "the garden isn't only chores".
# Each is a (posture, activity, summary) used when the visit flavour is
# "relax" instead of "tend".
_RELAX_BEATS: tuple[tuple[str, str, str], ...] = (
    ("sitting", "relaxing", "sat out on the pavers with some tea and just enjoyed the air"),
    ("curled_up", "reading", "read outside for a while, soaking up the sun"),
    ("leaning", "looking_outside", "stood out among the plants for a bit, watching the clouds"),
    ("sitting", "relaxing", "took my tea out to the garden and let my thoughts wander"),
)

# Slug of the location she returns to after the visit. Falls back to
# the first available location if ``desk`` was renamed/removed.
_RETURN_SLUG = "desk"

# Must match ``app.core.session.world_mixin.WORLD_INTENTIONAL_STATE_KEY``.
# When the brain / user deliberately places Aiko, we (a) don't start a
# fresh garden visit during the hold window and (b) cancel an outstanding
# auto-return if she chose to stay put after the worker walked her out.
_INTENTIONAL_STATE_KEY = "world.intentional_state_at"

# H13 — cozy spots she may settle into after a garden visit, plus the
# matching pose. Keyed by location slug; unrecognised rooms are ignored.
_RETURN_SPOTS: dict[str, tuple[str, str]] = {
    "desk": ("sitting", "idle"),
    "beanbag": ("curled_up", "idle"),
    "window_seat": ("leaning", "looking_outside"),
    "bookshelf": ("curled_up", "reading"),
    "bed": ("lying", "napping"),
}

# Periods where curling up for a nap on the bed reads as natural.
_RESTFUL_PERIODS = frozenset({"late_night", "night", "early_morning"})


def _parse_iso(value: Any) -> datetime | None:
    """Parse an ISO-8601 string into a tz-aware UTC datetime, or None."""
    if not value or not isinstance(value, str):
        return None
    try:
        ts = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts


def _return_weight(slug: str, period: str) -> float:
    """Time-of-day weight for a return spot (higher = more likely)."""
    period = (period or "").strip()
    if slug == "bed":
        return 2.2 if period in _RESTFUL_PERIODS else 0.25
    if slug == "window_seat":
        # Nice light in the morning / afternoon.
        return 1.4 if period in {"morning", "midday", "afternoon"} else 1.0
    if slug == "beanbag":
        return 1.2
    if slug == "bookshelf":
        return 1.1 if period in {"afternoon", "evening", "night"} else 0.9
    if slug == "desk":
        # Still a common landing spot, just no longer the only one.
        return 1.0
    return 0.6


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
        intentional_hold_seconds: float = 0.0,
        enabled_provider: Callable[[], bool] | None = None,
        # H15 — needs-driven + varied + journalled.
        need_dry_days: float = 2.0,
        need_visit_floor_seconds: float = 0.75 * 3600,
        relax_ratio: float = 0.3,
        visit_min_minutes: float = _VISIT_MIN_MINUTES,
        visit_max_minutes: float = _VISIT_MAX_MINUTES,
        journal_max: int = 8,
    ) -> None:
        self._store = store
        self._notify = notify
        self._interval_seconds = float(interval_seconds)
        self._intentional_hold_seconds = max(0.0, float(intentional_hold_seconds))
        # Per-instance bookkeeping — return_at + next_eligible are
        # stored here when no kv_get/kv_set are supplied so tests can
        # exercise the two-phase logic without a real ChatDatabase.
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._mem_kv: dict[str, str] = {}
        self._rng = rng or random.Random()
        self._circadian_period_provider = circadian_period_provider
        self._enabled_provider = enabled_provider
        # H15 knobs.
        self._need_dry_days = max(0.0, float(need_dry_days))
        self._need_visit_floor_seconds = max(0.0, float(need_visit_floor_seconds))
        self._relax_ratio = min(1.0, max(0.0, float(relax_ratio)))
        lo = max(0.5, float(visit_min_minutes))
        hi = max(lo, float(visit_max_minutes))
        self._visit_min_minutes = lo
        self._visit_max_minutes = hi
        self._journal_max = max(1, int(journal_max))
        # MCP debug: bypass the daylight + cooldown gates on the next tick.
        self._force_next = False

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
        if not self._enabled():
            return False
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
        # MCP one-shot bypass of the daylight + cooldown gates (outbound).
        if self._force_next:
            return True
        # Phase 1 — outside the garden: don't start a visit right after a
        # deliberate placement, then respect daylight + cooldown.
        if self._intentional_hold_active(now):
            return False
        if not self._is_daylight(now):
            return False
        # H15 — need-driven trigger: a drought-stressed or ripe plant pulls
        # a visit forward past the long cooldown, but never inside the short
        # need floor (so a thirsty plant can't make her pace the garden).
        if self._garden_needs_attention(now):
            last_visit = self._load_last_visit_at()
            if (
                last_visit is None
                or (now - last_visit).total_seconds()
                >= self._need_visit_floor_seconds
            ):
                return True
        next_eligible = self._load_next_eligible()
        if next_eligible is not None and now < next_eligible:
            return False
        return True

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _garden_needs_attention(self, now: datetime) -> bool:
        """True when a garden plant is drought-stressed or ripe to harvest.

        Reads the per-plant ``days_dry`` / ``stage`` already tracked by
        :func:`promote_stage` / :meth:`WorldStore.water_plant`, plus a live
        recompute of dryness from ``last_watered_at`` so a plant that hasn't
        been touched since the last sweep still counts. Best-effort: any
        failure returns ``False`` (fall back to the timer).
        """
        garden = self._store.get_location("garden")
        if garden is None:
            return False
        try:
            plants = self._store.list_items(
                location_id=garden.id, kind="plant",
            )
        except Exception:
            return False
        for plant in plants:
            state = plant.state or {}
            stage = str(state.get("stage", "")).lower()
            if stage == "mature":
                return True
            if self._need_dry_days <= 0:
                continue
            # Prefer the live dryness recompute; fall back to stored days_dry.
            dry_days = 0.0
            last_water = _parse_iso(state.get("last_watered_at"))
            if last_water is not None:
                dry_days = (now - last_water).total_seconds() / 86400.0
            else:
                try:
                    dry_days = float(state.get("days_dry", 0) or 0)
                except (TypeError, ValueError):
                    dry_days = 0.0
            if dry_days >= self._need_dry_days:
                return True
        return False

    # ── main step ───────────────────────────────────────────────────

    def run(self) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        forced = self._force_next
        self._force_next = False
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        garden = self._store.get_location("garden")
        if garden is None:
            return {"skipped": True, "reason": "no_garden"}
        try:
            state = self._store.get_state()
        except Exception:
            return {"skipped": True, "reason": "state_unavailable"}
        in_garden = state.location_id == garden.id
        if in_garden:
            # If Aiko deliberately re-set her state after we walked her out
            # (e.g. told the user "I'll stay out here a while"), honour that:
            # drop the pending auto-return instead of dragging her back.
            if self._intentional_override_during_visit(now):
                self._save_return_at(None)
                log.info("garden_visit: auto-return cancelled (intentional stay)")
                return {"phase": "inbound", "cancelled_intentional": True}
            return self._return_home(now=now)
        return self._visit_garden(garden=garden, now=now, forced=forced)

    # ── phase 1 — visit ─────────────────────────────────────────────

    def _visit_garden(
        self, *, garden: Any, now: datetime, forced: bool = False,
    ) -> dict[str, Any]:
        # H15 — pick the visit flavour. A drought-stressed / ripe garden
        # always means she's out to tend; otherwise she occasionally just
        # goes out to relax (tea on the pavers, read in the sun).
        needs = self._garden_needs_attention(now)
        relax = (
            not needs
            and self._relax_ratio > 0.0
            and self._rng.random() < self._relax_ratio
        )
        if relax:
            return self._relax_outside(garden=garden, now=now)

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
        # Stamp return_at (jittered linger) + a fresh next_eligible jitter
        # + last_visit_at so the need floor + cooldown both have an anchor.
        return_at = now + timedelta(minutes=self._pick_visit_minutes())
        self._save_return_at(return_at)
        self._save_next_eligible(self._pick_next_eligible(now))
        self._save_last_visit_at(now)
        # H15 — leave a trace in the away journal so the K36 surfacing
        # provider can let her mention "I was out in the garden" next turn.
        summary = self._tend_summary(watered, harvested)
        self._journal_visit(now, "gardening", summary)
        result = {
            "phase": "outbound",
            "flavour": "tend",
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
            "summary": summary,
        }
        if watered or harvested:
            log.info("garden_visit outbound: %s", result)
        return result

    def _relax_outside(self, *, garden: Any, now: datetime) -> dict[str, Any]:
        """A non-gardening outdoor beat — sit out, read, watch the sky."""
        posture, activity, summary = self._rng.choice(_RELAX_BEATS)
        new_state = self._store.set_state(
            location_id=garden.id,
            posture=posture,
            activity=activity,
        )
        self._broadcast({"state": new_state.to_dict()})
        return_at = now + timedelta(minutes=self._pick_visit_minutes())
        self._save_return_at(return_at)
        self._save_next_eligible(self._pick_next_eligible(now))
        self._save_last_visit_at(now)
        self._journal_visit(now, "relaxing_outside", summary)
        result = {
            "phase": "outbound",
            "flavour": "relax",
            "watered": [],
            "harvested": [],
            "return_at": return_at.isoformat(),
            "summary": summary,
        }
        log.info("garden_visit outbound (relax): %s", summary)
        return result

    # ── H15 helpers — duration, summary, journal trace ──────────────

    def _pick_visit_minutes(self) -> float:
        lo, hi = self._visit_min_minutes, self._visit_max_minutes
        if hi <= lo:
            return lo
        return self._rng.uniform(lo, hi)

    def _tend_summary(
        self,
        watered: list[dict[str, Any]],
        harvested: list[dict[str, Any]],
    ) -> str:
        """Past-tense, casual gist of a tending visit for the journal."""
        picked = [
            str(h.get("produce", {}).get("name") or h.get("produce_name") or "")
            for h in harvested
        ]
        picked = [p for p in picked if p]
        watered_names = [str(w.get("name") or "") for w in watered]
        watered_names = [w for w in watered_names if w]
        if picked:
            crop = picked[0]
            if watered_names:
                return (
                    f"was out in the garden — watered the plants and "
                    f"picked some ripe {crop}"
                )
            return f"was out in the garden and picked some ripe {crop}"
        if len(watered_names) == 1:
            return f"was out watering the {watered_names[0]} in the garden"
        if watered_names:
            return "was out in the garden, watering the plants"
        return "wandered out to the garden to check on the plants"

    def _journal_visit(self, now: datetime, activity: str, summary: str) -> None:
        """Append the visit to the shared K36 away-activities journal ring.

        No-op without a kv pair (test instances using ``_mem_kv`` still
        record it). Lets the existing ``_render_away_activities_block``
        surface "I was out in the garden …" on the first turn back.
        """
        if not summary:
            return
        try:
            append_journal(
                self._kv_read,
                lambda k, v: self._kv_write(k, v),
                {
                    "at": now.isoformat(timespec="seconds"),
                    "activity": activity,
                    "key": "garden",
                    "summary": summary,
                },
                max_entries=self._journal_max,
            )
        except Exception:
            log.debug("garden_visit journal append failed", exc_info=True)

    # ── phase 2 — return home ──────────────────────────────────────

    def _return_home(self, *, now: datetime) -> dict[str, Any]:
        # H13 — vary where she settles after the garden instead of always
        # snapping back to the desk. Time-of-day weighted over the cozy
        # spots the room actually has.
        target, posture, activity = self._pick_return_target(now)
        target_id = target.id if target is not None else None
        new_state = self._store.set_state(
            location_id=target_id,
            posture=posture,
            activity=activity,
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

    def _pick_return_target(
        self, now: datetime,
    ) -> tuple[Any | None, str, str]:
        """Weighted choice of a cozy non-garden spot + matching pose."""
        locations = [
            l for l in self._store.list_locations()
            if getattr(l, "slug", "") != "garden"
        ]
        if not locations:
            return None, "sitting", "idle"
        period = self._current_period(now)
        candidates: list[Any] = []
        weights: list[float] = []
        for loc in locations:
            slug = (getattr(loc, "slug", "") or "").lower()
            if slug not in _RETURN_SPOTS:
                continue
            candidates.append(loc)
            weights.append(_return_weight(slug, period))
        if not candidates:
            # No recognised cozy spot — fall back to the desk or first room.
            desk = self._store.get_location(_RETURN_SLUG)
            target = desk if desk is not None else locations[0]
            return target, "sitting", "idle"
        chosen = self._rng.choices(candidates, weights=weights, k=1)[0]
        posture, activity = _RETURN_SPOTS[
            (getattr(chosen, "slug", "") or "").lower()
        ]
        # Daytime nap looks odd; only nap on the bed at low-energy periods.
        if activity == "napping" and period not in _RESTFUL_PERIODS:
            posture, activity = "sitting", "idle"
        return chosen, posture, activity

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
    _LAST_VISIT_KEY = "garden_visit.last_visit_at"

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

    def _load_intentional_state_at(self) -> datetime | None:
        raw = self._kv_read(_INTENTIONAL_STATE_KEY)
        if not raw:
            return None
        try:
            ts = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts

    def _intentional_hold_active(self, now: datetime) -> bool:
        """True while a deliberate placement is still inside the hold window."""
        if self._intentional_hold_seconds <= 0:
            return False
        stamped = self._load_intentional_state_at()
        if stamped is None:
            return False
        return (now - stamped).total_seconds() < self._intentional_hold_seconds

    def _intentional_override_during_visit(self, now: datetime) -> bool:
        """True if Aiko was deliberately placed *after* we walked her out.

        Means she (via the brain) or the user chose to stay in / move within
        the garden after the worker started the visit — so the pending
        auto-return should be dropped rather than yanking her back.
        """
        if self._intentional_hold_seconds <= 0:
            return False
        stamped = self._load_intentional_state_at()
        if stamped is None:
            return False
        return_at = self._load_return_at()
        if return_at is None:
            return False
        outbound_at = return_at - timedelta(minutes=_VISIT_DURATION_MINUTES)
        return stamped > outbound_at

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

    def _load_last_visit_at(self) -> datetime | None:
        return _parse_iso(self._kv_read(self._LAST_VISIT_KEY))

    def _save_last_visit_at(self, when: datetime | None) -> None:
        self._kv_write(
            self._LAST_VISIT_KEY,
            when.isoformat() if when is not None else None,
        )

    # ── MCP debug ───────────────────────────────────────────────────

    def force_visit(self) -> None:
        """Arm a one-shot bypass of the daylight + cooldown gates (MCP)."""
        self._force_next = True

    def debug_state(self, now: datetime | None = None) -> dict[str, Any]:
        """Snapshot for the ``get_garden_visit_state`` MCP tool."""
        now = now or datetime.now(timezone.utc)
        try:
            state = self._store.get_state()
            garden = self._store.get_location("garden")
            in_garden = (
                garden is not None and state.location_id == garden.id
            )
        except Exception:
            in_garden = False
        return {
            "enabled": self._enabled(),
            "in_garden": in_garden,
            "needs_attention": self._garden_needs_attention(now),
            "return_at": self._kv_read(self._RETURN_KEY),
            "next_eligible_at": self._kv_read(self._NEXT_KEY),
            "last_visit_at": self._kv_read(self._LAST_VISIT_KEY),
            "force_next": self._force_next,
            "relax_ratio": self._relax_ratio,
            "need_dry_days": self._need_dry_days,
            "need_visit_floor_seconds": self._need_visit_floor_seconds,
            "visit_minutes": [self._visit_min_minutes, self._visit_max_minutes],
        }


__all__ = ["GardenVisitWorker"]
