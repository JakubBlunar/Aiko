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
  * **narrates from and writes back item state** (K91): the beat's clause
    is composed from the row it touched via :mod:`beat_detail` (which
    chapter the book is on, which pot is driest) and an optional
    :class:`ItemEffect` advances that row through the room's existing
    transitions, so what she says she did and what her room shows can no
    longer drift apart,
  * **chains beats into an episode** (K91) after a long quiet stretch, so
    a whole afternoon reads as "I made tea, then curled up with the book"
    instead of two unrelated postcards. Candidate building lives in
    :class:`ActivityCandidatesMixin` because a chain needs to see every
    beat the room affords, and :mod:`beat_episode` owns the successor
    table. An episode journals one entry (carrying its ``keys``) and is
    rephrased once, so its LLM cost matches a single beat's,
  * composes a first-person one-liner (deterministic template, optionally
    rephrased by the local worker LLM with a safe fallback),
  * appends ``{at, activity, summary}`` to a small kv_meta journal ring,
    and
  * **keeps the substantive ones** (K85b): a beat that changed her room,
    ran as an episode, or closed the day's intention also writes a
    ``pursuit_note`` memory. The ring holds eight entries, so before
    this everything she did on her own was gone within a day or two --
    which is why nothing could grow into a durable sense of what she is
    into. A beat that left no trace still doesn't leave one here.

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
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import WorkSignal
from app.core.world import beat_detail, beat_episode, day_intention
from app.core.world.activity_selection import weighted_pick
from app.core.world.idle_activity_candidates_mixin import (
    ActivityCandidatesMixin,
)
from app.core.world.idle_activity_plan import (
    EFFECT_ADVANCE_BOOK,
    EFFECT_POUR_TEA,
    EFFECT_WATER_PLANT,
    OUTING_DAYLIGHT_PERIODS,
    VALID_EFFECTS,
    ActivityPlan,
    ItemEffect,
    RoomSnapshot,
)
from app.core.infra import timephrase

if TYPE_CHECKING:
    from app.core.memory.pursuit_notes import PursuitNoteWriter
    from app.core.world.world_store import WorldStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.idle_activity_worker")


# kv_meta keys this worker owns (namespaced under ``away_activity.``),
# plus the shared journal key the surfacing provider reads.
AWAY_ACTIVITIES_JOURNAL_KEY = "aiko.away_activities"
_KV_LAST_FIRED_AT = "away_activity.last_fired_at"
_KV_DAY = "away_activity.day"
_KV_DAY_COUNT = "away_activity.day_count"

# H17 — idle beats feed the idea machine. Seeds drafted here land in this
# ring; the consumer is ``InnerLifeProvidersMixin._render_idle_seed_block``.
IDLE_SEEDS_KEY = "aiko.idle_seeds"
_KV_SEED_DAY = "idle_seed.day"
_KV_SEED_DAY_COUNT = "idle_seed.day_count"

# H22 — light outings ("I stepped out for a bit"). Own cooldown + daily
# cap kv watermarks, independent of the general away pacing so the outing
# stays rare even when ordinary beats fire often.
_KV_OUTING_LAST_FIRED_AT = "outing.last_fired_at"
_KV_OUTING_DAY = "outing.day"
_KV_OUTING_DAY_COUNT = "outing.day_count"

# Must match the literal GardenVisitWorker writes (see
# ``garden_visit_worker.GardenVisitWorker._RETURN_KEY``). Duplicated to
# avoid importing the garden module just for a string.
_GARDEN_RETURN_KEY = "garden_visit.return_at"

# Must match ``app.core.session.world_mixin.WORLD_INTENTIONAL_STATE_KEY``.
# Stamped whenever the brain / user deliberately places Aiko; while it's
# fresh we defer so an autonomous beat never overrides a spot she chose.
_INTENTIONAL_STATE_KEY = "world.intentional_state_at"


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


def load_idle_seeds(kv_get: Callable[[str], str | None]) -> list[dict[str, Any]]:
    """Return the H17 idle-seed ring (oldest → newest)."""
    try:
        raw = kv_get(IDLE_SEEDS_KEY)
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


def append_idle_seed(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> bool:
    """Append ``entry`` to the H17 idle-seed ring, trimming to ``max_entries``.

    Shared by the away-activity producer (:meth:`IdleAwayActivityWorker.
    _maybe_emit_seed`) and the H19 hobby worker so a hobby takeaway surfaces
    through the same one-shot ``_render_idle_seed_block`` cue. Returns ``True``
    on a successful write, ``False`` if the kv write raised.
    """
    seeds = load_idle_seeds(kv_get)
    seeds.append(entry)
    if max_entries > 0 and len(seeds) > max_entries:
        seeds = seeds[-max_entries:]
    try:
        kv_set(IDLE_SEEDS_KEY, json.dumps(seeds))
    except Exception:
        log.debug("idle_seed ring write failed", exc_info=True)
        return False
    return True


class IdleAwayActivityWorker(ActivityCandidatesMixin):
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
        idle_seed_ratio: float = 0.0,
        idle_seed_daily_cap: int = 3,
        idle_seed_max_ring: int = 6,
        outings_enabled_provider: Callable[[], bool] | None = None,
        outing_cooldown_seconds: float = 6.0 * 3600,
        outing_daily_cap: int = 2,
        episode_ratio: float = 0.0,
        episode_max_beats: int = 3,
        episode_min_gap_seconds: float = 10800.0,
        day_intention_enabled: bool = False,
        hobby_provider: Callable[[], str | None] | None = None,
        pursuit_notes: "PursuitNoteWriter | None" = None,
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
        self._idle_seed_ratio = min(1.0, max(0.0, float(idle_seed_ratio)))
        self._idle_seed_daily_cap = max(0, int(idle_seed_daily_cap))
        self._idle_seed_max_ring = max(1, int(idle_seed_max_ring))
        self._outings_enabled_provider = outings_enabled_provider
        self._outing_cooldown_seconds = max(0.0, float(outing_cooldown_seconds))
        self._outing_daily_cap = max(0, int(outing_daily_cap))
        self._episode_ratio = min(1.0, max(0.0, float(episode_ratio)))
        self._episode_max_beats = max(1, int(episode_max_beats))
        self._episode_min_gap_seconds = max(0.0, float(episode_min_gap_seconds))
        self._day_intention_enabled = bool(day_intention_enabled)
        self._hobby_provider = hobby_provider
        self._pursuit_notes = pursuit_notes
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
        # Feature flag only; the interval became the heartbeat (P36).
        if self._enabled_provider is not None:
            try:
                if not bool(self._enabled_provider()):
                    return False
            except Exception:
                pass
        return True

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """Is a beat due, and will composing it cost a generation?

        Hoists the four gates that used to burn a scheduler slot inside
        ``run()`` -- intentional hold, garden deferral, cooldown, daily
        cap -- into a probe that is four kv reads.

        Pressure rises with how long past the cooldown she is, so a long
        absence produces beats promptly rather than at whatever cadence
        the interval happened to be. That is the "what is she doing
        while I'm gone" half of P36.

        ``needs_llm`` is true whenever a worker model is wired, because
        the summary rephrase runs for any non-precomposed plan even when
        the ``away_activities_llm_ratio`` dice come up short.
        """
        if self._enabled_provider is not None:
            try:
                if not bool(self._enabled_provider()):
                    return WorkSignal(pressure=0.0, reason="disabled")
            except Exception:
                pass
        if self._intentional_hold_active(now):
            return WorkSignal(pressure=0.0, reason="intentional_hold")
        if self._garden_visit_outstanding(now):
            return WorkSignal(pressure=0.0, reason="garden_visit")
        if not self._cooldown_elapsed(now):
            return WorkSignal(pressure=0.0, reason="cooldown")
        if not self._under_daily_cap(now):
            return WorkSignal(pressure=0.0, reason="daily_cap")

        pressure = 1.0
        if self._cooldown_seconds > 0:
            last = _parse_iso(self._kv_get_safe(_KV_LAST_FIRED_AT))
            if last is not None:
                over = (now - last).total_seconds() / self._cooldown_seconds
                # 1x cooldown -> 0.5, 2x or more -> saturated.
                pressure = max(0.5, min(1.0, over / 2.0))
        return WorkSignal(
            pressure=pressure,
            reason="beat_due",
            needs_llm=bool(self._ollama is not None and self._model),
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
        snapshot = self._build_candidates(user_name, now)
        # K91 — establish today's intention before anything is chosen, so a
        # forced or LLM-composed beat is measured against it too.
        intention = self._today_intention(snapshot.items, now)
        plan = self._choose_plan(user_name, now, snapshot, intention)
        if plan is None:
            return {"fired": 0, "no_plan": True}

        # K91 — a long quiet stretch plays out as a short sequence rather
        # than a single disconnected postcard.
        chain = self._plan_episode(plan, snapshot, now)
        effects: list[dict[str, Any]] = []
        for beat in chain:
            # H22 — stamp the outing's own cooldown + daily cap when chosen.
            if beat.key == "outing":
                self._mark_outing_fired(now)
            beat_effect = self._apply_world_mutation(beat)
            if beat_effect:
                effects.append(beat_effect)

        summary = self._episode_summary(user_name, chain)
        summary, closed_intention = self._maybe_close_intention(
            intention, chain, summary, now,
        )
        entry: dict[str, Any] = {
            "at": now.isoformat(timespec="seconds"),
            "activity": chain[-1].activity,
            "key": chain[0].key,
            "summary": summary,
        }
        if len(chain) > 1:
            entry["keys"] = [b.key for b in chain]
        append_journal(
            self._kv_get, self._kv_set, entry, max_entries=self._journal_max,
        )
        noted = self._note_pursuit(
            now, chain, summary, effects, closed_intention,
        )
        self._mark_fired(now)
        log.info(
            "away_activity fired: keys=%s activity=%s posture=%s",
            [b.key for b in chain],
            chain[-1].activity,
            chain[-1].posture,
        )
        seed = self._maybe_emit_seed(now, user_name, chain[-1], summary)
        result: dict[str, Any] = {
            "fired": 1,
            "key": chain[0].key,
            "activity": chain[-1].activity,
            "summary": summary,
        }
        if len(chain) > 1:
            result["episode"] = [b.key for b in chain]
        if closed_intention:
            result["closed_intention"] = True
        if noted is not None:
            result["pursuit_note_id"] = noted
        if effects:
            result["item_effect"] = effects[0] if len(effects) == 1 else effects
        if seed:
            result["seed"] = seed
        return result

    def _episode_summary(
        self, user_name: str, chain: list[ActivityPlan],
    ) -> str:
        """One line covering the whole chain.

        A multi-beat episode is rephrased once rather than per beat: it
        keeps the generation cost of an episode identical to a single
        beat's, and the model writes better prose when it can see the
        sequence than when it sees each step in isolation.
        """
        if len(chain) == 1:
            plan = chain[0]
            if plan.precomposed:
                return plan.summary
            return self._compose_summary(user_name, plan)
        joined = beat_episode.join_clauses([b.summary for b in chain])
        if any(b.precomposed for b in chain):
            return joined
        return self._compose_summary(
            user_name,
            ActivityPlan(
                key=chain[0].key,
                posture=chain[-1].posture,
                activity=chain[-1].activity,
                summary=joined,
            ),
            sequence=True,
        )

    # ── K85b: keep the beats that left a trace ────────────────────────

    def _note_pursuit(
        self,
        now: datetime,
        chain: list[ActivityPlan],
        summary: str,
        effects: list[dict[str, Any]],
        closed_intention: bool,
    ) -> int | None:
        """Write a ``pursuit_note`` for a beat with something behind it.

        The bar is a trace, not a mood: she changed a row in her room,
        the afternoon ran long enough to chain, or the thing she meant
        to do today got done. "Looked out the window" is a real beat and
        belongs in the ring, but there is nothing in it to grow an
        interest out of, and filing it here would bury the ones there
        are under ambient weather.
        """
        if self._pursuit_notes is None:
            return None
        if not (effects or closed_intention or len(chain) > 1):
            return None
        return self._pursuit_notes.write(
            summary,
            source="away_beat",
            topic=chain[0].key,
            at=now,
            extra={
                "keys": [b.key for b in chain],
                "changed": [str(e.get("effect") or "") for e in effects],
                "closed_intention": bool(closed_intention),
            },
        )

    # ── H17: idle beats feed the idea machine ─────────────────────────

    def _maybe_emit_seed(
        self,
        now: datetime,
        user_name: str,
        plan: ActivityPlan,
        summary: str,
    ) -> str | None:
        """Occasionally turn a beat into a forward-looking conversational seed.

        Bounded hard: needs a worker model, fires only a ``ratio`` fraction
        of beats, and is daily-capped. The seed lands in the kv ring read by
        the one-shot ``_render_idle_seed_block`` cue producer so Aiko phrases
        the "while I was reading I started wondering ..." line herself.
        """
        if self._idle_seed_ratio <= 0.0:
            return None
        if self._ollama is None or not self._model:
            return None
        if self._rng.random() >= self._idle_seed_ratio:
            return None
        if not self._under_seed_daily_cap(now):
            return None

        seed = self._compose_seed_llm(user_name, plan, summary)
        if not seed:
            return None

        ok = append_idle_seed(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "activity": plan.activity,
                "key": plan.key,
                "seed": seed,
            },
            max_entries=self._idle_seed_max_ring,
        )
        if not ok:
            return None
        self._bump_seed_day_count(now)
        log.info(
            "idle_seed produced: key=%s activity=%s seed=%s",
            plan.key,
            plan.activity,
            seed[:80],
        )
        return seed

    def _under_seed_daily_cap(self, now: datetime) -> bool:
        if self._idle_seed_daily_cap <= 0:
            return False
        today = now.date().isoformat()
        try:
            day = self._kv_get(_KV_SEED_DAY)
            count = int(self._kv_get(_KV_SEED_DAY_COUNT) or "0")
        except Exception:
            day, count = None, 0
        if day != today:
            return True
        return count < self._idle_seed_daily_cap

    def _bump_seed_day_count(self, now: datetime) -> None:
        today = now.date().isoformat()
        try:
            day = self._kv_get(_KV_SEED_DAY)
            count = int(self._kv_get(_KV_SEED_DAY_COUNT) or "0")
        except Exception:
            day, count = None, 0
        if day != today:
            count = 0
        try:
            self._kv_set(_KV_SEED_DAY, today)
            self._kv_set(_KV_SEED_DAY_COUNT, str(count + 1))
        except Exception:
            log.debug("idle_seed day-count write failed", exc_info=True)

    def _compose_seed_llm(
        self, user_name: str, plan: ActivityPlan, summary: str,
    ) -> str | None:
        """Ask the worker model for one short thought sparked by the beat."""
        if self._ollama is None or not self._model:
            return None
        activity = (plan.activity or "").replace("_", " ").strip() or "pottering about"
        context = summary.strip() or f"she was {activity}"
        system = (
            "You are Aiko's quiet inner voice. She just spent some time alone "
            f"doing this: {context}. In ONE short sentence (max ~20 words), "
            "write a single forward-looking thought, small question, or budding "
            f"opinion this sparked that she might bring up to {user_name} later. "
            "First person, casual, specific to the activity. No greeting, no "
            "quotes, no preamble — just the thought. Return JSON "
            '{"seed": "<the thought>"}.'
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": "Give me the thought."},
                ],
                model=self._model,
                options={"temperature": 0.9, "num_predict": 80},
                format_json=True,
                surface="idle_seed",
            )
        except Exception:
            log.debug("idle_seed compose failed", exc_info=True)
            return None
        if not content:
            return None
        try:
            blob = json.loads(content)
        except Exception:
            return None
        seed = ""
        if isinstance(blob, dict):
            seed = str(blob.get("seed") or "").strip()
        if not seed:
            return None
        # Length-cap so a runaway generation can't bloat the ring / prompt.
        return seed[:240]

    # ── activity selection ───────────────────────────────────────────

    def _pick_activity(
        self, user_name: str, now: datetime | None = None,
    ) -> ActivityPlan | None:
        """The single beat she'd plausibly be having right now."""
        now = now or _utcnow()
        snapshot = self._build_candidates(user_name, now)
        return self._choose_plan(
            user_name,
            now,
            snapshot,
            self._today_intention(snapshot.items, now),
        )

    def _choose_plan(
        self,
        user_name: str,
        now: datetime,
        snapshot: RoomSnapshot,
        intention: "day_intention.DayIntention | None" = None,
    ) -> ActivityPlan | None:
        candidates = snapshot.candidates
        if not candidates:
            return None

        # MCP-forced key wins if it produced a candidate this tick.
        forced = self._forced_activity_key
        self._forced_activity_key = None
        if forced and forced in candidates:
            return candidates[forced]

        open_intent = (
            intention.text
            if intention is not None and not intention.satisfied
            else ""
        )

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
            llm_plan = self._compose_plan_llm(
                user_name,
                snapshot.locations,
                snapshot.items,
                intention=open_intent,
            )
            if llm_plan is not None:
                return llm_plan

        # H18 — weighted, anti-repetition draw over the available keys,
        # tilted by recency (journal), circadian period, mood + day-color,
        # plus K91's intention for the day.
        chosen = weighted_pick(
            list(candidates.keys()),
            rng=self._rng,
            recent_keys=self._recent_keys(),
            period=self._read_period(),
            valence=self._read_valence(),
            day_color=self._read_day_color(),
            intent_key=(
                intention.beat_key
                if intention is not None and not intention.satisfied
                else ""
            ),
            intent_boost=day_intention.INTENT_BOOST,
        )
        if chosen is None or chosen not in candidates:
            return self._rng.choice(list(candidates.values()))
        return candidates[chosen]

    def _recent_keys(self) -> list[str]:
        """Beat keys from the journal, oldest first, episodes expanded.

        An episode journals one entry for several beats, so H18's
        anti-repetition has to read the whole chain or a beat inside an
        episode would look like it never happened.
        """
        keys: list[str] = []
        for entry in load_journal(self._kv_get):
            chain = entry.get("keys")
            if isinstance(chain, list) and chain:
                keys.extend(str(k) for k in chain if k)
                continue
            key = entry.get("key")
            if key:
                keys.append(str(key))
        return keys

    # ── K91: episodes ────────────────────────────────────────────────

    def _plan_episode(
        self,
        first: ActivityPlan,
        snapshot: RoomSnapshot,
        now: datetime,
    ) -> list[ActivityPlan]:
        """Extend one beat into a short chain, or return just that beat.

        Only fires after she's been left alone a while: a beat following
        hard on the last one belongs to an already-busy day, while a long
        quiet stretch is when a connected sequence is both plausible and
        worth telling.
        """
        if self._episode_ratio <= 0.0 or self._episode_max_beats <= 1:
            return [first]
        if first.key not in beat_episode.SUCCESSORS:
            return [first]
        if not beat_episode.should_chain(
            seconds_since_last_beat=self._seconds_since_last_beat(now),
            min_gap_seconds=self._episode_min_gap_seconds,
            ratio=self._episode_ratio,
            rng=self._rng,
        ):
            return [first]
        keys = beat_episode.plan_chain(
            first.key,
            list(snapshot.candidates.keys()),
            rng=self._rng,
            length=beat_episode.pick_length(
                rng=self._rng, max_beats=self._episode_max_beats,
            ),
        )
        return [snapshot.candidates[k] for k in keys if k in snapshot.candidates]

    def _seconds_since_last_beat(self, now: datetime) -> float | None:
        last = _parse_iso(self._kv_get_safe(_KV_LAST_FIRED_AT))
        if last is None:
            return None
        return max(0.0, (now - last).total_seconds())

    # ── K91: the day's intention ─────────────────────────────────────

    def _today_intention(
        self, items: list[Any], now: datetime,
    ) -> day_intention.DayIntention | None:
        """Today's intention, proposing one on the day's first beat.

        Yesterday's is discarded rather than carried over: an intention
        that survives the night stops being "today" and starts being a
        grudge.
        """
        if not self._day_intention_enabled:
            return None
        today = day_intention.local_day(now)
        current = day_intention.load(
            self._kv_get_safe(day_intention.DAY_INTENTION_KEY)
        )
        if current is not None and current.day == today:
            return current
        try:
            fresh = day_intention.propose(
                items, now=now, hobby=self._read_hobby(), rng=self._rng,
            )
        except Exception:
            log.debug("day_intention propose failed", exc_info=True)
            return None
        self._kv_set_safe(
            day_intention.DAY_INTENTION_KEY, day_intention.dump(fresh)
        )
        log.info(
            "day_intention set: text=%s beat=%s", fresh.text, fresh.beat_key,
        )
        return fresh

    def _read_hobby(self) -> str | None:
        if self._hobby_provider is None:
            return None
        try:
            hobby = self._hobby_provider()
            return str(hobby).strip() or None if hobby else None
        except Exception:
            return None

    def _maybe_close_intention(
        self,
        current: "day_intention.DayIntention | None",
        chain: list[ActivityPlan],
        summary: str,
        now: datetime,
    ) -> tuple[str, bool]:
        """Mark the intention done when a beat satisfied it, and say so.

        Returns the (possibly annotated) summary plus whether it closed.
        The admission is what makes the day read as authored rather than
        sampled -- without it, satisfying the intention is invisible.
        """
        if current is None or current.satisfied:
            return summary, False
        if current.day != day_intention.local_day(now):
            return summary, False
        if current.beat_key not in {b.key for b in chain}:
            return summary, False
        self._kv_set_safe(
            day_intention.DAY_INTENTION_KEY,
            day_intention.dump(current.satisfy()),
        )
        log.info("day_intention satisfied: text=%s", current.text)
        return day_intention.close_out(summary, self._rng), True

    def day_intention_debug_state(
        self, now: datetime | None = None,
    ) -> dict[str, Any]:
        """Snapshot of the K91 day intention for the MCP state tool."""
        now = now or _utcnow()
        current = day_intention.load(
            self._kv_get_safe(day_intention.DAY_INTENTION_KEY)
        )
        return {
            "enabled": self._day_intention_enabled,
            "today": day_intention.local_day(now),
            "intention": current.to_dict() if current is not None else None,
        }

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

    def _apply_world_mutation(self, plan: ActivityPlan) -> dict[str, Any] | None:
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

        if plan.item_effect is not None:
            return self._apply_item_effect(plan.item_effect)
        return None

    # ── K91: beats write their state back ────────────────────────────

    def _apply_item_effect(self, effect: ItemEffect) -> dict[str, Any] | None:
        """Advance the item a beat acted on, reusing the room's own math.

        Reading a chapter, pouring a cup and watering a pot are already
        modelled -- by H20's ``room_evolution`` transitions and the
        store's ``water_plant`` -- so this only has to route to them.
        Returns a small result dict for the run log, or ``None`` when the
        row is gone or the store can't service the action.
        """
        if effect.action not in VALID_EFFECTS:
            return None
        try:
            item = self._world_store.get_item(int(effect.item_id))
        except Exception:
            log.debug("away_activity effect lookup failed", exc_info=True)
            return None
        if item is None:
            return None
        try:
            if effect.action == EFFECT_WATER_PLANT:
                return self._effect_water_plant(item)
            if effect.action == EFFECT_POUR_TEA:
                return self._effect_pour_tea(item)
            return self._effect_advance_book(item)
        except Exception:
            log.debug(
                "away_activity effect failed action=%s", effect.action,
                exc_info=True,
            )
            return None

    def _effect_water_plant(self, item: Any) -> dict[str, Any] | None:
        watered = self._world_store.water_plant(int(item.id))
        if watered is None:
            return None
        self._broadcast({"item": watered.to_dict()})
        return {"effect": EFFECT_WATER_PLANT, "item": watered.name}

    def _effect_pour_tea(self, item: Any) -> dict[str, Any] | None:
        from app.core.world import room_evolution as evo

        new_state, new_desc, _event = evo.next_tea(item.state, self._rng)
        updated = self._world_store.update_item(
            int(item.id), description=new_desc, state=new_state,
        )
        if updated is None:
            return None
        self._broadcast({"item": updated.to_dict()})
        return {
            "effect": EFFECT_POUR_TEA,
            "fullness": new_state.get("fullness"),
        }

    def _effect_advance_book(self, item: Any) -> dict[str, Any] | None:
        """Read one chapter. Finishing is a real event, so it seeds a cue."""
        from app.core.world import room_evolution as evo

        new_state, new_name, new_desc, finished = evo.advance_book(
            item.state, self._rng,
        )
        updated = self._world_store.update_item(
            int(item.id), name=new_name, description=new_desc, state=new_state,
        )
        if updated is None:
            return None
        self._broadcast({"item": updated.to_dict()})
        result: dict[str, Any] = {"effect": EFFECT_ADVANCE_BOOK}
        if finished:
            result["finished"] = finished
            append_idle_seed(
                self._kv_get,
                self._kv_set,
                {
                    "at": _utcnow().isoformat(timespec="seconds"),
                    "activity": "reading " + str(finished),
                    "key": "read_book",
                    "seed": (
                        'I finished "' + str(finished) + '" while you were out '
                        "— I want to talk about that ending."
                    ),
                },
                max_entries=self._idle_seed_max_ring,
            )
            log.info("away_activity finished book: %s", finished)
        else:
            result["progress"] = new_state.get("progress")
        return result

    # ── summary composition ──────────────────────────────────────────

    def _compose_summary(
        self, user_name: str, plan: ActivityPlan, *, sequence: bool = False,
    ) -> str:
        fallback = plan.summary
        if self._ollama is None or not self._model:
            return fallback
        shape = (
            "ONE short sentence that keeps the order of what happened"
            if sequence
            else "ONE short clause"
        )
        prompt = (
            f"You are Aiko, alone in your room while {user_name} was away. "
            f"You just spent some quiet time: {plan.summary}. Rewrite that as "
            "the gist of what you'd casually mention you got up to — first "
            f"person, past tense, {shape}, no greeting, no stage "
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
        self,
        user_name: str,
        locations: list[Any],
        items: list[Any],
        *,
        intention: str = "",
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
            (getattr(loc, "slug", "") or "").lower(): loc for loc in locations
        }
        loc_lines = "; ".join(
            f"{getattr(loc, 'slug', '')} ({getattr(loc, 'name', '')})"
            for loc in locations
        )
        # K91 — the model sees each thing's live state, not just its name,
        # so it can write a beat *about* the dry pot or the half-read book
        # instead of inventing generic business around a noun.
        item_names = beat_detail.describe_items_for_prompt(items, now=_utcnow())
        recent = [
            str(e.get("activity") or "")
            for e in load_journal(self._kv_get)[-5:]
            if e.get("activity")
        ]
        recent_line = ", ".join(recent) or "(none yet)"
        period = self._read_period() or "unspecified"
        intent_line = ""
        if intention:
            intent_line = (
                "Today you meant to " + intention + " — lean that way if it "
                "fits the hour. "
            )
        prompt = (
            f"You are Aiko, alone in your cozy room while {user_name} is away. "
            f"It's currently {period}. Your room's spots: {loc_lines}. "
            f"Things around: {item_names}. "
            f"{intent_line}"
            f"Recently you did: {recent_line} — pick something different. "
            "Choose one small, believable thing to do right now, grounded in "
            "what's actually in the room — the states in brackets are real, "
            "so prefer something they suggest. Reply with JSON only:\n"
            '{"location_slug": "<one of the slugs above>", '
            '"posture": "<sitting|lying|standing|curled_up|leaning>", '
            '"activity": "<short verb phrase, e.g. repotting_the_basil>", '
            '"summary": "<one first-person past-tense clause>", '
            '"changed_item": "<exact name of the one thing you used up, '
            'watered or read, or \\"\\" if none>"}'
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
            item_effect=self._resolve_named_effect(
                blob.get("changed_item"), items,
            ),
        )

    def _resolve_named_effect(
        self, raw_name: Any, items: list[Any],
    ) -> ItemEffect | None:
        """Turn the model's ``changed_item`` into a legal effect, or ``None``.

        The action is *derived* from the matched row's kind rather than
        taken from the model, so there is no way to ask for a transition
        the item doesn't support. An unmatched or unsupported name is
        simply dropped -- the beat still happens, it just leaves no trace.
        """
        name = str(raw_name or "").strip().lower()
        if not name:
            return None
        match = next(
            (
                i
                for i in items
                if (getattr(i, "name", "") or "").strip().lower() == name
            ),
            None,
        )
        if match is None:
            return None
        return self._effect_for_item(match)

    @staticmethod
    def _effect_for_item(item: Any) -> ItemEffect | None:
        """The one transition an item supports, if any."""
        kind = str(getattr(item, "kind", "") or "")
        slug = str(getattr(item, "slug", "") or "")
        lowered = (getattr(item, "name", "") or "").lower()
        if kind == "plant":
            return ItemEffect(item_id=int(item.id), action=EFFECT_WATER_PLANT)
        if slug == "tea_pot" or "tea pot" in lowered:
            return ItemEffect(item_id=int(item.id), action=EFFECT_POUR_TEA)
        if kind == "book":
            return ItemEffect(item_id=int(item.id), action=EFFECT_ADVANCE_BOOK)
        return None

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

    # ── H22: light-outing gates ───────────────────────────────────────

    def _outings_enabled(self) -> bool:
        if self._outings_enabled_provider is None:
            return True
        try:
            return bool(self._outings_enabled_provider())
        except Exception:
            return True

    def _outing_eligible(self, now: datetime) -> bool:
        """True when a rare daylight outing may be offered this tick."""
        if not self._outings_enabled():
            return False
        if self._outing_daily_cap <= 0:
            return False
        # Daylight only — but tolerate an unknown period (no provider).
        period = self._read_period()
        if period and period not in OUTING_DAYLIGHT_PERIODS:
            return False
        # Own cooldown floor.
        if self._outing_cooldown_seconds > 0:
            last = _parse_iso(self._kv_get_safe(_KV_OUTING_LAST_FIRED_AT))
            if last is not None and (
                (now - last).total_seconds() < self._outing_cooldown_seconds
            ):
                return False
        return self._under_outing_daily_cap(now)

    def _under_outing_daily_cap(self, now: datetime) -> bool:
        if self._outing_daily_cap <= 0:
            return False
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe(_KV_OUTING_DAY) != today:
            return True
        try:
            count = int(self._kv_get_safe(_KV_OUTING_DAY_COUNT) or "0")
        except (TypeError, ValueError):
            count = 0
        return count < self._outing_daily_cap

    def _mark_outing_fired(self, now: datetime) -> None:
        self._kv_set_safe(
            _KV_OUTING_LAST_FIRED_AT, now.isoformat(timespec="seconds")
        )
        today = now.astimezone().strftime("%Y-%m-%d")
        if self._kv_get_safe(_KV_OUTING_DAY) != today:
            self._kv_set_safe(_KV_OUTING_DAY, today)
            self._kv_set_safe(_KV_OUTING_DAY_COUNT, "1")
            return
        try:
            count = int(self._kv_get_safe(_KV_OUTING_DAY_COUNT) or "0")
        except (TypeError, ValueError):
            count = 0
        self._kv_set_safe(_KV_OUTING_DAY_COUNT, str(count + 1))

    def outing_debug_state(self, now: datetime | None = None) -> dict[str, Any]:
        """Snapshot of the H22 outing gates for the MCP state tool."""
        now = now or _utcnow()
        return {
            "enabled": self._outings_enabled(),
            "eligible": self._outing_eligible(now),
            "cooldown_seconds": self._outing_cooldown_seconds,
            "daily_cap": self._outing_daily_cap,
            "last_fired_at": self._kv_get_safe(_KV_OUTING_LAST_FIRED_AT),
            "day": self._kv_get_safe(_KV_OUTING_DAY),
            "day_count": self._kv_get_safe(_KV_OUTING_DAY_COUNT),
            "period": self._read_period(),
        }

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
    "ItemEffect",
    "EFFECT_ADVANCE_BOOK",
    "EFFECT_POUR_TEA",
    "EFFECT_WATER_PLANT",
    "AWAY_ACTIVITIES_JOURNAL_KEY",
    "IDLE_SEEDS_KEY",
    "load_journal",
    "append_journal",
    "load_idle_seeds",
    "append_idle_seed",
]
