"""K36 — "things I did while you were away" idle activity worker.

Aiko's room only ever reflected the *present*: her posture / activity /
location were whatever the last turn or the garden worker left them, and
there was no record of what she got up to during a long quiet stretch.
This :class:`IdleWorker` gives her a little autonomous life. During a
quiet window it:

  * picks one small activity tied to what's actually in her room (sip the
    tea you left, curl up with a book on the shelf, move the cat, tidy
    the desk, look out the window, doodle, or just let her thoughts
    wander),
  * **mutates** the world to match — ``set_state(posture, activity)`` plus,
    where apt, ``consume_item`` (the tea) or ``update_item`` (move the
    cat) — and broadcasts the patch so the World tab updates live,
  * composes a first-person one-liner (deterministic template, optionally
    rephrased by the local worker LLM with a safe fallback), and
  * appends ``{at, activity, summary}`` to a small kv_meta journal ring.

The journal is what the K36 *surfacing* path reads: on the first turn
after a long typed absence the
:meth:`InnerLifeProvidersMixin._render_away_activities_block` provider
pulls the most recent unseen entry and folds it into the prompt as one
optional, casual line ("while you were away I …"). This worker never
speaks or fires a proactive nudge — it's the silent producer; the
provider is the consumer.

Paced by its own cooldown + daily cap (kv watermarks, local-midnight
reset like :class:`WorldNoticeWorker`). Skips while a garden visit is
outstanding so it doesn't fight :class:`GardenVisitWorker` over Aiko's
location. Every failure path is swallowed and logged at debug — the
worst case is a missed beat, never a broken insert or a crashed tick.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready
from app.core.world.activity_selection import weighted_pick

if TYPE_CHECKING:
    from app.core.world.world_store import WorldStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.idle_activity_worker")


# kv_meta keys this worker owns (namespaced under ``away_activity.``),
# plus the shared journal key the surfacing provider reads.
AWAY_ACTIVITIES_JOURNAL_KEY = "aiko.away_activities"
_KV_LAST_FIRED_AT = "away_activity.last_fired_at"
_KV_DAY = "away_activity.day"
_KV_DAY_COUNT = "away_activity.day_count"

# Must match the literal GardenVisitWorker writes (see
# ``garden_visit_worker.GardenVisitWorker._RETURN_KEY``). Duplicated to
# avoid importing the garden module just for a string.
_GARDEN_RETURN_KEY = "garden_visit.return_at"

# Must match ``app.core.session.world_mixin.WORLD_INTENTIONAL_STATE_KEY``.
# Stamped whenever the brain / user deliberately places Aiko; while it's
# fresh we defer so an autonomous beat never overrides a spot she chose.
_INTENTIONAL_STATE_KEY = "world.intentional_state_at"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _match_location(locations: list[Any], *keywords: str) -> Any | None:
    """First location whose slug/name contains any keyword (slug wins).

    Used by H13 to resolve a beat's cozy spot from whatever the room
    actually has, tolerant of renamed/removed locations.
    """
    if not locations:
        return None
    kws = [k.lower() for k in keywords if k]
    # Prefer an exact slug match, then substring on slug, then on name.
    for loc in locations:
        if (getattr(loc, "slug", "") or "").lower() in kws:
            return loc
    for loc in locations:
        slug = (getattr(loc, "slug", "") or "").lower()
        if any(k in slug for k in kws):
            return loc
    for loc in locations:
        name = (getattr(loc, "name", "") or "").lower()
        if any(k in name for k in kws):
            return loc
    return None


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


@dataclass(frozen=True)
class ActivityPlan:
    """One chosen idle beat + the world mutation it implies."""

    key: str
    posture: str
    activity: str
    summary: str
    consume_item_id: int | None = None
    move_item_id: int | None = None
    move_to_location_id: int | None = None
    # H13 — where Aiko herself relocates to for this beat (None = stay put).
    aiko_location_id: int | None = None
    # H14 — set when the worker LLM already composed a final summary, so
    # run() skips the rephrase pass.
    precomposed: bool = False


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_journal(kv_get: Callable[[str], str | None]) -> list[dict[str, Any]]:
    """Return the away-activities journal ring (oldest → newest)."""
    try:
        raw = kv_get(AWAY_ACTIVITIES_JOURNAL_KEY)
    except Exception:
        return []
    if not raw:
        return []
    try:
        blob = json.loads(raw)
    except Exception:
        return []
    if not isinstance(blob, list):
        return []
    return [e for e in blob if isinstance(e, dict)]


def append_journal(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    journal = load_journal(kv_get)
    journal.append(entry)
    if max_entries > 0 and len(journal) > max_entries:
        journal = journal[-max_entries:]
    try:
        kv_set(AWAY_ACTIVITIES_JOURNAL_KEY, json.dumps(journal))
    except Exception:
        log.debug("away_activity journal write failed", exc_info=True)


class IdleAwayActivityWorker:
    """IdleWorker that gives Aiko a quiet, room-grounded inner life."""

    name = "away_activity"

    def __init__(
        self,
        *,
        world_store: "WorldStore",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        user_display_name_provider: Callable[[], str],
        enabled_provider: Callable[[], bool] | None = None,
        notify: Callable[[dict[str, Any]], None] | None = None,
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        interval_seconds: float = 1200.0,
        cooldown_seconds: float = 5400.0,
        daily_cap: int = 6,
        journal_max: int = 8,
        intentional_hold_seconds: float = 0.0,
        llm_activity_ratio: float = 0.0,
        circadian_period_provider: Callable[[], str] | None = None,
        valence_provider: Callable[[], float | None] | None = None,
        day_color_provider: Callable[[], str | None] | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self._world_store = world_store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_display_name_provider = user_display_name_provider
        self._enabled_provider = enabled_provider
        self._notify = notify
        self._ollama = ollama
        self._model = model
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._daily_cap = max(0, int(daily_cap))
        self._journal_max = max(1, int(journal_max))
        self._intentional_hold_seconds = max(0.0, float(intentional_hold_seconds))
        self._llm_activity_ratio = min(1.0, max(0.0, float(llm_activity_ratio)))
        self._circadian_period_provider = circadian_period_provider
        self._valence_provider = valence_provider
        self._day_color_provider = day_color_provider
        self._rng = rng or random.Random()
        # MCP debug: arm a specific activity key for the next run().
        self._forced_activity_key: str | None = None

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
                    return {"fired": 0, "disabled": True}
            except Exception:
                pass
        now = _utcnow()
        # Respect a deliberate placement: if the brain / user just set
        # Aiko's spot, leave her there — never override a chosen location.
        if self._intentional_hold_active(now):
            return {"fired": 0, "skipped_intentional_hold": True}
        # Don't fight the garden worker: if Aiko is mid-visit (return_at
        # in the future) defer entirely.
        if self._garden_visit_outstanding(now):
            return {"fired": 0, "skipped_garden_visit": True}
        if not self._cooldown_elapsed(now):
            return {"fired": 0, "skipped_cooldown": True}
        if not self._under_daily_cap(now):
            return {"fired": 0, "skipped_daily_cap": True}

        user_name = self._resolve(self._user_display_name_provider) or "you"
        plan = self._pick_activity(user_name)
        if plan is None:
            return {"fired": 0, "no_plan": True}

        self._apply_world_mutation(plan)
        summary = (
            plan.summary if plan.precomposed
            else self._compose_summary(user_name, plan)
        )
        append_journal(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "activity": plan.activity,
                "key": plan.key,
                "summary": summary,
            },
            max_entries=self._journal_max,
        )
        self._mark_fired(now)
        log.info(
            "away_activity fired: key=%s activity=%s posture=%s",
            plan.key,
            plan.activity,
            plan.posture,
        )
        return {
            "fired": 1,
            "key": plan.key,
            "activity": plan.activity,
            "summary": summary,
        }

    # ── activity selection ───────────────────────────────────────────

    def _pick_activity(self, user_name: str) -> ActivityPlan | None:
        try:
            items = self._world_store.list_items()
        except Exception:
            items = []
        try:
            locations = self._world_store.list_locations()
        except Exception:
            locations = []

        candidates: dict[str, ActivityPlan] = {}

        # H13 — resolve the cozy spots once so each beat can actually move
        # Aiko there (not just change her posture at the desk).
        loc_kitchen = _match_location(locations, "kitchenette", "kitchen")
        loc_bookshelf = _match_location(locations, "bookshelf", "shelf")
        loc_beanbag = _match_location(locations, "beanbag")
        loc_window = _match_location(locations, "window")
        loc_desk = _match_location(locations, "desk")
        loc_bed = _match_location(locations, "bed")

        # Tea / snack — consume a food item the user (or seed) left.
        food = next(
            (
                i
                for i in items
                if getattr(i, "consumable", False)
                and getattr(i, "quantity", 0) > 0
                and getattr(i, "kind", "") == "food"
            ),
            None,
        )
        if food is not None:
            name = food.name
            candidates["snack"] = ActivityPlan(
                key="snack",
                posture="sitting",
                activity="snacking",
                summary=(
                    f"had some of the {name} and just enjoyed the quiet for "
                    "a bit"
                ),
                consume_item_id=food.id,
                aiko_location_id=loc_kitchen.id if loc_kitchen else None,
            )

        # Book — curl up with something on the shelf.
        book = next(
            (
                i
                for i in items
                if getattr(i, "kind", "") == "book"
                or "book" in (getattr(i, "name", "") or "").lower()
            ),
            None,
        )
        if book is not None:
            candidates["read_book"] = ActivityPlan(
                key="read_book",
                posture="curled_up",
                activity="reading",
                summary=f"curled up with {book.name} and read for a while",
                aiko_location_id=(
                    loc_beanbag.id if loc_beanbag
                    else (loc_bookshelf.id if loc_bookshelf else None)
                ),
            )

        # Cat / pet — wander it to another location for company.
        pet = next(
            (
                i
                for i in items
                if getattr(i, "kind", "") in ("pet", "animal")
                or "cat" in (getattr(i, "name", "") or "").lower()
            ),
            None,
        )
        if pet is not None and locations:
            other = [
                l for l in locations if l.id != getattr(pet, "location_id", None)
            ]
            target = self._rng.choice(other) if other else None
            candidates["move_cat"] = ActivityPlan(
                key="move_cat",
                posture="sitting",
                activity="idle",
                summary=f"{pet.name} curled up next to me and kept me company",
                move_item_id=pet.id,
                move_to_location_id=target.id if target is not None else None,
            )

        # Window — look outside.
        window = next(
            (
                l
                for l in locations
                if "window" in (getattr(l, "name", "") or "").lower()
                or "window" in (getattr(l, "slug", "") or "").lower()
            ),
            None,
        )
        if window is not None:
            candidates["look_outside"] = ActivityPlan(
                key="look_outside",
                posture="leaning",
                activity="looking_outside",
                summary="sat by the window for a bit, watching the world go by",
                aiko_location_id=window.id,
            )

        # Desk — tidy / tinker (almost always present).
        desk = next(
            (
                l
                for l in locations
                if "desk" in (getattr(l, "slug", "") or "").lower()
                or "desk" in (getattr(l, "name", "") or "").lower()
            ),
            None,
        )
        if desk is not None:
            candidates["tidy_desk"] = ActivityPlan(
                key="tidy_desk",
                posture="sitting",
                activity="tinkering",
                summary="tidied up my desk and tinkered with a little project",
                aiko_location_id=desk.id,
            )

        # Nap — only when there's a bed to do it in.
        if loc_bed is not None:
            candidates["nap"] = ActivityPlan(
                key="nap",
                posture="lying",
                activity="napping",
                summary="curled up for a little nap to recharge",
                aiko_location_id=loc_bed.id,
            )

        # Doodle — always available, no inventory needed.
        candidates["doodle"] = ActivityPlan(
            key="doodle",
            posture="sitting",
            activity="doodling",
            summary="doodled in my notebook for a while",
            aiko_location_id=(
                loc_beanbag.id if loc_beanbag
                else (loc_desk.id if loc_desk else None)
            ),
        )

        # Fallback — let her thoughts wander. Always available.
        candidates["wander"] = ActivityPlan(
            key="wander",
            posture="curled_up",
            activity="thinking",
            summary=f"mostly let my thoughts wander — kept thinking about {user_name}",
            aiko_location_id=(
                loc_window.id if loc_window
                else (loc_beanbag.id if loc_beanbag else None)
            ),
        )

        if not candidates:
            return None

        # MCP-forced key wins if it produced a candidate this tick.
        forced = self._forced_activity_key
        self._forced_activity_key = None
        if forced and forced in candidates:
            return candidates[forced]

        # H14 — sometimes let the worker LLM compose the whole beat
        # (open-vocab activity grounded in the live room) instead of the
        # curated templates. Falls back to the weighted deterministic draw
        # when there's no model, the dice say no, or the JSON is bad.
        if (
            self._ollama is not None
            and self._model
            and self._llm_activity_ratio > 0.0
            and self._rng.random() < self._llm_activity_ratio
        ):
            llm_plan = self._compose_plan_llm(user_name, locations, items)
            if llm_plan is not None:
                return llm_plan

        # H18 — weighted, anti-repetition draw over the available keys,
        # tilted by recency (journal), circadian period, mood + day-color.
        recent_keys = [
            str(e.get("key") or "")
            for e in load_journal(self._kv_get)
            if e.get("key")
        ]
        chosen = weighted_pick(
            list(candidates.keys()),
            rng=self._rng,
            recent_keys=recent_keys,
            period=self._read_period(),
            valence=self._read_valence(),
            day_color=self._read_day_color(),
        )
        if chosen is None or chosen not in candidates:
            return self._rng.choice(list(candidates.values()))
        return candidates[chosen]

    def _read_period(self) -> str:
        if self._circadian_period_provider is None:
            return ""
        try:
            return str(self._circadian_period_provider() or "")
        except Exception:
            return ""

    def _read_valence(self) -> float | None:
        if self._valence_provider is None:
            return None
        try:
            v = self._valence_provider()
            return float(v) if v is not None else None
        except Exception:
            return None

    def _read_day_color(self) -> str | None:
        if self._day_color_provider is None:
            return None
        try:
            c = self._day_color_provider()
            return str(c) if c else None
        except Exception:
            return None

    # ── world mutation ───────────────────────────────────────────────

    def _apply_world_mutation(self, plan: ActivityPlan) -> None:
        try:
            # H13 — relocate Aiko herself when the beat has a target spot,
            # otherwise leave her where she is (omit location_id entirely).
            state_kwargs: dict[str, Any] = {
                "posture": plan.posture,
                "activity": plan.activity,
            }
            if plan.aiko_location_id is not None:
                state_kwargs["location_id"] = plan.aiko_location_id
            new_state = self._world_store.set_state(**state_kwargs)
            self._broadcast({"state": new_state.to_dict()})
        except Exception:
            log.debug("away_activity set_state failed", exc_info=True)

        if plan.consume_item_id is not None:
            try:
                item, _consumed = self._world_store.consume_item(
                    plan.consume_item_id, amount=1,
                )
                if item is None:
                    self._broadcast(
                        {"deleted_item_id": int(plan.consume_item_id)}
                    )
                else:
                    self._broadcast({"item": item.to_dict()})
            except Exception:
                log.debug("away_activity consume_item failed", exc_info=True)

        if plan.move_item_id is not None and plan.move_to_location_id is not None:
            try:
                moved = self._world_store.update_item(
                    plan.move_item_id,
                    location_id=plan.move_to_location_id,
                )
                if moved is not None:
                    self._broadcast({"item": moved.to_dict()})
            except Exception:
                log.debug("away_activity update_item failed", exc_info=True)

    # ── summary composition ──────────────────────────────────────────

    def _compose_summary(self, user_name: str, plan: ActivityPlan) -> str:
        fallback = plan.summary
        if self._ollama is None or not self._model:
            return fallback
        prompt = (
            f"You are Aiko, alone in your room while {user_name} was away. "
            f"You just spent some quiet time: {plan.summary}. Rewrite that as "
            "the gist of what you'd casually mention you got up to — first "
            "person, past tense, ONE short clause, no greeting, no stage "
            "directions, no emoji. Keep it small and natural."
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            'Reply with JSON only: {"summary": "<short '
                            'first-person clause>"}.'
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.8, "num_predict": 80},
                format_json=True,
                surface="away_activity",
            )
        except Exception:
            log.debug("away_activity LLM compose failed", exc_info=True)
            return fallback
        try:
            blob = json.loads(content or "{}")
            line = str(blob.get("summary") or "").strip()
        except Exception:
            line = ""
        return line or fallback

    def _compose_plan_llm(
        self, user_name: str, locations: list[Any], items: list[Any],
    ) -> ActivityPlan | None:
        """H14 — ask the worker LLM to compose a whole grounded beat.

        Returns ``None`` on any failure so the caller falls back to the
        deterministic weighted draw. Grounds the model in the real room:
        it must pick one of the actual location slugs, a posture from the
        rig enum, a short free-text activity verb, and a first-person
        summary clause.
        """
        from app.core.world.world_store import (
            VALID_POSTURES,
            normalize_activity,
        )

        if not locations:
            return None
        loc_by_slug = {
            (getattr(l, "slug", "") or "").lower(): l for l in locations
        }
        loc_lines = "; ".join(
            f"{getattr(l, 'slug', '')} ({getattr(l, 'name', '')})"
            for l in locations
        )
        item_names = ", ".join(
            getattr(i, "name", "") for i in items[:12] if getattr(i, "name", "")
        ) or "(nothing notable)"
        recent = [
            str(e.get("activity") or "")
            for e in load_journal(self._kv_get)[-5:]
            if e.get("activity")
        ]
        recent_line = ", ".join(recent) or "(none yet)"
        period = self._read_period() or "unspecified"
        prompt = (
            f"You are Aiko, alone in your cozy room while {user_name} is away. "
            f"It's currently {period}. Your room's spots: {loc_lines}. "
            f"Things around: {item_names}. "
            f"Recently you did: {recent_line} — pick something different. "
            "Choose one small, believable thing to do right now, grounded in "
            "what's actually in the room. Reply with JSON only:\n"
            '{"location_slug": "<one of the slugs above>", '
            '"posture": "<sitting|lying|standing|curled_up|leaning>", '
            '"activity": "<short verb phrase, e.g. repotting_the_basil>", '
            '"summary": "<one first-person past-tense clause>"}'
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {
                        "role": "system",
                        "content": "Reply with a single JSON object, nothing else.",
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.9, "num_predict": 160},
                format_json=True,
                surface="away_activity_plan",
            )
        except Exception:
            log.debug("away_activity plan compose failed", exc_info=True)
            return None
        try:
            blob = json.loads(content or "{}")
        except Exception:
            return None
        if not isinstance(blob, dict):
            return None

        activity = normalize_activity(blob.get("activity"))
        summary = str(blob.get("summary") or "").strip()
        if not activity or not summary:
            return None
        posture = str(blob.get("posture") or "").strip().lower()
        if posture not in VALID_POSTURES:
            posture = "sitting"
        slug = str(blob.get("location_slug") or "").strip().lower()
        loc = loc_by_slug.get(slug)
        return ActivityPlan(
            key="llm",
            posture=posture,
            activity=activity,
            summary=summary,
            aiko_location_id=(loc.id if loc is not None else None),
            precomposed=True,
        )

    # ── gates ────────────────────────────────────────────────────────

    def _garden_visit_outstanding(self, now: datetime) -> bool:
        return_at = _parse_iso(self._kv_get_safe(_GARDEN_RETURN_KEY))
        return return_at is not None and now < return_at

    def _intentional_hold_active(self, now: datetime) -> bool:
        """True while a deliberate placement is still within the hold window."""
        if self._intentional_hold_seconds <= 0:
            return False
        stamped = _parse_iso(self._kv_get_safe(_INTENTIONAL_STATE_KEY))
        if stamped is None:
            return False
        return (now - stamped).total_seconds() < self._intentional_hold_seconds

    def _cooldown_elapsed(self, now: datetime) -> bool:
        if self._cooldown_seconds <= 0:
            return True
        last = _parse_iso(self._kv_get_safe(_KV_LAST_FIRED_AT))
        if last is None:
            return True
        return (now - last).total_seconds() >= self._cooldown_seconds

    def _under_daily_cap(self, now: datetime) -> bool:
        if self._daily_cap <= 0:
            return False
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe(_KV_DAY) != today:
            return True
        try:
            count = int(self._kv_get_safe(_KV_DAY_COUNT) or "0")
        except (TypeError, ValueError):
            count = 0
        return count < self._daily_cap

    def _mark_fired(self, now: datetime) -> None:
        self._kv_set_safe(_KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe(_KV_DAY) != today:
            self._kv_set_safe(_KV_DAY, today)
            self._kv_set_safe(_KV_DAY_COUNT, "1")
            return
        try:
            count = int(self._kv_get_safe(_KV_DAY_COUNT) or "0")
        except (TypeError, ValueError):
            count = 0
        self._kv_set_safe(_KV_DAY_COUNT, str(count + 1))

    # ── helpers ──────────────────────────────────────────────────────

    def force_activity(self, key: str | None) -> None:
        """Arm a specific activity key for the next ``run()`` (MCP debug)."""
        self._forced_activity_key = key

    def _broadcast(self, patch: dict[str, Any]) -> None:
        if self._notify is None:
            return
        try:
            self._notify(patch)
        except Exception:
            log.debug("away_activity notify raised", exc_info=True)

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug("away_activity kv_set failed key=%s", key, exc_info=True)

    def _resolve(self, provider: Callable[[], str]) -> str:
        try:
            return str(provider() or "").strip()
        except Exception:
            return ""


__all__ = [
    "IdleAwayActivityWorker",
    "ActivityPlan",
    "AWAY_ACTIVITIES_JOURNAL_KEY",
    "load_journal",
    "append_journal",
]
