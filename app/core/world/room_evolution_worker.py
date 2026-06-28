"""RoomEvolutionWorker — H20, a room that quietly accrues a history.

A low-cadence :class:`IdleWorker` that, during quiet windows, applies ONE
small bounded micro-state transition to a seeded room item and broadcasts
the ``world_updated`` patch so the World tab shows the drift:

* **tea pot** — cycles full → half → empty → (brews a fresh flavour).
* **cookie jar** — refilled with a fresh batch once it runs low/empty
  (closes the loop with the away-beat "snack" that consumes it).
* **sci-fi paperback** — gains reading progress and, on finishing, flips to
  a brand-new book and emits a takeaway **seed** through the shared H17
  idle-seed cue ("finally finished X — that ending!").

Deterministic transition math lives in the pure
:mod:`app.core.world.room_evolution` module; this worker just picks an
applicable transition, applies it to the live store, and paces itself with
a wall-clock floor so the room drifts gradually rather than thrashing.
"""
from __future__ import annotations

import json
import logging
import random
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready
from app.core.world import room_evolution as evo
from app.core.world.idle_activity_worker import append_idle_seed

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.world.world_store import WorldStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.room_evolution")

# Wall-clock gate so the room drifts on its own slow clock independent of
# the idle-tick cadence. Namespaced under ``aiko.*`` like the other
# idle-life kv state.
KV_LAST_EVOLVED_AT = "aiko.room_evolution_at"

# Cookie jar is "low" (refill candidate) at or below this quantity.
_COOKIE_LOW_AT = 1
_COOKIE_REFILL_QTY = 3


class RoomEvolutionWorker:
    """IdleWorker that slowly evolves the room's consumables + book."""

    name = "room_evolution"

    def __init__(
        self,
        *,
        world_store: "WorldStore",
        chat_db: "ChatDatabase",
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        user_display_name_provider: Callable[[], str],
        notify: Callable[[dict[str, Any]], None] | None = None,
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        idle_seed_max_ring: int = 6,
        rng: random.Random | None = None,
    ) -> None:
        self._world = world_store
        self._chat_db = chat_db
        self._agent = agent_settings
        self._mem = memory_settings
        self._user_display_name_provider = user_display_name_provider
        self._notify = notify
        self._ollama = ollama
        self._model = model
        self._idle_seed_max_ring = max(1, int(idle_seed_max_ring))
        self._rng = rng or random.Random()
        self._force = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(self._mem, "room_evolution_interval_seconds", 21600)
        )

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> bool:
        if not bool(getattr(self._agent, "room_evolution_enabled", True)):
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent, "room_evolution_enabled", True)):
            return {"skipped": True, "reason": "disabled"}

        now = self._utcnow()
        force = self._force
        self._force = False
        if not force and not self._gap_elapsed(now):
            return {"skipped": True, "reason": "min_gap"}

        # Build the list of applicable transitions, then pick one.
        candidates: list[Callable[[datetime], dict[str, Any] | None]] = []
        items = {i.slug: i for i in self._world.list_items()}

        if evo.TEA_POT_SLUG in items:
            candidates.append(lambda n: self._evolve_tea(items[evo.TEA_POT_SLUG]))
        if evo.BOOK_SLUG in items:
            candidates.append(lambda n: self._evolve_book(items[evo.BOOK_SLUG], n))
        # Cookies: refill candidate only when low or missing entirely.
        jar = items.get(evo.COOKIE_JAR_SLUG)
        if jar is None or jar.quantity <= _COOKIE_LOW_AT:
            candidates.append(lambda n: self._refill_cookies(jar))

        if not candidates:
            return {"skipped": True, "reason": "no_candidates"}

        chosen = self._rng.choice(candidates)
        result = chosen(now)
        # Stamp the wall-clock gate only when something actually changed.
        if result is not None:
            self._stamp(now)
            return {"evolved": True, **result}
        return {"skipped": True, "reason": "noop"}

    # ── transitions ──────────────────────────────────────────────────

    def _evolve_tea(self, item: Any) -> dict[str, Any] | None:
        new_state, new_desc, _event = evo.next_tea(item.state, self._rng)
        updated = self._world.update_item(
            item.id, description=new_desc, state=new_state,
        )
        if updated is None:
            return None
        self._broadcast({"item": updated.to_dict()})
        log.info(
            "room-evolution tea: fullness=%s flavor=%s",
            new_state.get("fullness"), new_state.get("flavor"),
        )
        return {"kind": "tea", "fullness": new_state.get("fullness")}

    def _refill_cookies(self, jar: Any) -> dict[str, Any] | None:
        prev_flavor = None
        if jar is not None:
            prev_flavor = (jar.state or {}).get("flavor")
        desc, state = evo.fresh_cookie_batch(prev_flavor, self._rng)
        if jar is not None:
            updated = self._world.update_item(
                jar.id,
                description=desc,
                quantity=_COOKIE_REFILL_QTY,
                state=state,
            )
            if updated is None:
                return None
            self._broadcast({"item": updated.to_dict()})
        else:
            # The jar was consumed to nothing earlier — re-create it.
            loc_id = self._kitchenette_id()
            res = self._world.add_item(
                name="cookies",
                slug=evo.COOKIE_JAR_SLUG,
                kind="food",
                description=desc,
                location_id=loc_id,
                consumable=True,
                quantity=_COOKIE_REFILL_QTY,
                state=state,
                given_by="aiko",
            )
            if res is None:
                return None
            item, _created = res
            self._broadcast({"item": item.to_dict()})
        log.info("room-evolution cookies: refilled flavor=%s", state.get("flavor"))
        return {"kind": "cookies", "flavor": state.get("flavor")}

    def _evolve_book(self, item: Any, now: datetime) -> dict[str, Any] | None:
        new_state, new_name, new_desc, finished = evo.advance_book(
            item.state, self._rng,
        )
        updated = self._world.update_item(
            item.id, name=new_name, description=new_desc, state=new_state,
        )
        if updated is None:
            return None
        self._broadcast({"item": updated.to_dict()})

        if finished:
            seed = self._compose_book_seed(finished, new_name)
            if seed:
                self._emit_seed(now, f"reading {finished}", seed)
            log.info(
                "room-evolution book finished: %s -> %s", finished, new_name,
            )
            return {"kind": "book", "finished": finished, "next": new_name}

        log.info(
            "room-evolution book: progress=%s/%s",
            new_state.get("progress"), new_state.get("total"),
        )
        return {"kind": "book", "progress": new_state.get("progress")}

    # ── helpers ───────────────────────────────────────────────────────

    def _compose_book_seed(
        self, finished_title: str, next_title: str,
    ) -> str | None:
        """Compose the "finished X" takeaway. LLM if available, else template."""
        if self._ollama is not None and self._model:
            try:
                name = self._user_display_name_provider() or "you"
            except Exception:
                name = "you"
            system = (
                f"You are Aiko's quiet inner voice. You just finished reading "
                f'"{finished_title}" and you\'re about to start "{next_title}". '
                "In ONE short sentence (max ~20 words), write a single "
                "spoiler-free reaction or thought about finishing it that you "
                f"might bring up to {name} later. First person, casual. No "
                'quotes, no preamble. Return JSON {"seed": "<the thought>"}.'
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
                    surface="room_evolution_book",
                )
                if content:
                    blob = json.loads(content)
                    if isinstance(blob, dict):
                        seed = str(blob.get("seed") or "").strip()
                        if seed:
                            return seed[:240]
            except Exception:
                log.debug("book seed compose failed", exc_info=True)
        # Deterministic fallback — finishing a book is worth a seed even
        # without a worker model.
        return (
            f"I finally finished \"{finished_title}\" — that ending! "
            "I want to tell someone about it."
        )

    def _emit_seed(self, now: datetime, activity: str, seed: str) -> None:
        append_idle_seed(
            self._chat_db.kv_get,
            self._chat_db.kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "activity": activity,
                "key": "room_evolution",
                "seed": seed,
            },
            max_entries=self._idle_seed_max_ring,
        )

    def _kitchenette_id(self) -> int | None:
        try:
            for loc in self._world.list_locations():
                if getattr(loc, "slug", "") == "kitchenette":
                    return loc.id
        except Exception:
            pass
        return None

    def _gap_elapsed(self, now: datetime) -> bool:
        min_hours = float(
            getattr(self._mem, "room_evolution_min_hours", 8.0)
        )
        if min_hours <= 0:
            return True
        try:
            raw = self._chat_db.kv_get(KV_LAST_EVOLVED_AT)
        except Exception:
            raw = None
        if not raw:
            return True
        try:
            last = datetime.fromisoformat(str(raw))
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
        except Exception:
            return True
        return (now - last).total_seconds() >= min_hours * 3600.0

    def _stamp(self, now: datetime) -> None:
        try:
            self._chat_db.kv_set(
                KV_LAST_EVOLVED_AT, now.isoformat(timespec="seconds")
            )
        except Exception:
            log.debug("room_evolution stamp failed", exc_info=True)

    def _broadcast(self, patch: dict[str, Any]) -> None:
        if self._notify is None:
            return
        try:
            self._notify(patch)
        except Exception:
            log.debug("room_evolution broadcast failed", exc_info=True)

    @staticmethod
    def _utcnow() -> datetime:
        return datetime.now(timezone.utc)


__all__ = ["RoomEvolutionWorker", "KV_LAST_EVOLVED_AT"]
