"""Proactive "I noticed my room" worker.

Aiko's room is normally silent: an item the user drops in via the UI
surfaces only as a passive line in the next prompt, and on quiet
stretches she never reaches out about her surroundings at all. This
``IdleWorker`` closes that gap. It rides the shared
:class:`IdleWorkerScheduler`, so it inherits the quiet-window gate (no
turn in flight, no live voice, user idle) and never fights the
conversation for GPU.

Two triggers, in priority order:

  * **Fresh gift** — ``add_world_item`` stamps a kv watermark
    (``world.last_user_gift`` -> ``{"id", "name", "at"}``) whenever the
    user drops something in the room. When that watermark is newer than
    the one we last handled, prime a short proactive line acknowledging
    the gift. Gift nudges bypass the daily cap (they're naturally
    one-per-distinct-gift) so a thoughtful gesture is never swallowed.
  * **Stale room** — when nothing new has been given but it's been
    longer than the cooldown since the last world nudge, occasionally
    prime a small in-character beat about what she's doing in her room.
    Bounded by a per-day cap so she stays subtle, not chatty.

The composed text is written into the same
:class:`PreparedNudgeStore` the :class:`NarrativeWeaver` fills, tagged
``source_kind="world"``. The existing :class:`ProactiveDirector`
consumes it on the next silence window and speaks it **verbatim**, so
the worker composes Aiko's actual first-person line (deterministic
template, optionally rephrased by the local worker LLM with a safe
fallback). Speaking still respects the director's presence + cooldown
gates; an unspoken nudge simply expires after ``ttl_seconds``.

Every failure path is swallowed and logged at debug — the worst case
is a missed nudge, never a broken insert or a crashed scheduler tick.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.proactive.prepared_nudge import PreparedNudgeStore
    from app.core.world.world_store import WorldStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.world_notice_worker")


# Must match ``app.core.session.world_mixin.WORLD_LAST_USER_GIFT_KEY``.
# Duplicated as a literal (not imported) to avoid pulling the heavy
# ``app.core.session`` package into this small worker module.
WORLD_LAST_USER_GIFT_KEY = "world.last_user_gift"

# kv_meta keys this worker owns (namespaced under ``world_notice.``).
_KV_GIFT_HANDLED_AT = "world_notice.last_gift_handled_at"
_KV_LAST_FIRED_AT = "world_notice.last_fired_at"
_KV_DAY = "world_notice.day"
_KV_DAY_COUNT = "world_notice.day_count"


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


class WorldNoticeWorker:
    """IdleWorker that primes proactive room / gift nudges."""

    name = "world_notice"

    def __init__(
        self,
        *,
        world_store: "WorldStore",
        prepared_nudge_store: "PreparedNudgeStore",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        user_id_provider: Callable[[], str],
        user_display_name_provider: Callable[[], str],
        enabled_provider: Callable[[], bool] | None = None,
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        interval_seconds: float = 300.0,
        cooldown_seconds: float = 3600.0,
        daily_cap: int = 4,
        ttl_seconds: float = 1800.0,
    ) -> None:
        self._world_store = world_store
        self._nudge_store = prepared_nudge_store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_id_provider = user_id_provider
        self._user_display_name_provider = user_display_name_provider
        self._enabled_provider = enabled_provider
        self._ollama = ollama
        self._model = model
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._daily_cap = max(0, int(daily_cap))
        self._ttl_seconds = max(60.0, float(ttl_seconds))

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
        user_id = self._resolve(self._user_id_provider)
        if not user_id:
            return {"fired": 0, "skipped_no_user": True}
        user_name = self._resolve(self._user_display_name_provider) or "the user"

        # ── Trigger 1: a freshly user-given item ─────────────────────
        gift = self._fresh_gift()
        if gift is not None:
            text = self._compose_gift_line(user_name, gift)
            if self._prime(user_id, text, source_id=f"gift:{gift.get('at')}"):
                # Gift nudges bypass the daily cap but still advance the
                # cooldown clock so a stale-room nudge doesn't pile on
                # right after.
                self._mark_gift_handled(gift)
                self._mark_fired(_utcnow(), count_against_cap=False)
                log.info(
                    "world_notice primed gift nudge user=%s item=%s",
                    user_id, gift.get("name"),
                )
                return {"fired": 1, "kind": "gift", "item": gift.get("name")}
            return {"fired": 0, "kind": "gift", "errored": True}

        # ── Trigger 2: stale room (occasional, capped) ───────────────
        now = _utcnow()
        if not self._cooldown_elapsed(now):
            return {"fired": 0, "skipped_cooldown": True}
        if not self._under_daily_cap(now):
            return {"fired": 0, "skipped_daily_cap": True}
        line = self._compose_room_line(user_name)
        if not line:
            return {"fired": 0, "kind": "room", "no_line": True}
        if self._prime(user_id, line, source_id="room"):
            self._mark_fired(now, count_against_cap=True)
            log.info("world_notice primed room nudge user=%s", user_id)
            return {"fired": 1, "kind": "room"}
        return {"fired": 0, "kind": "room", "errored": True}

    # ── triggers ─────────────────────────────────────────────────────

    def _fresh_gift(self) -> dict[str, Any] | None:
        """Return the pending gift watermark if newer than last handled."""
        raw = self._kv_get_safe(WORLD_LAST_USER_GIFT_KEY)
        if not raw:
            return None
        try:
            blob = json.loads(raw)
        except Exception:
            return None
        if not isinstance(blob, dict) or not blob.get("at"):
            return None
        handled = self._kv_get_safe(_KV_GIFT_HANDLED_AT)
        if handled and str(handled) == str(blob.get("at")):
            return None
        return blob

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

    # ── composition ──────────────────────────────────────────────────

    def _compose_gift_line(self, user_name: str, gift: dict[str, Any]) -> str:
        item = (gift.get("name") or "something").strip() or "something"
        fallback = (
            f"Oh — you left me {item}. That's really thoughtful of you, "
            f"{user_name}. Thank you."
        )
        prompt = (
            f"You are Aiko. {user_name} just quietly left {item} in your "
            "room as a small gift. Write the FIRST thing you'd say to them "
            "about it, warm and in-character, 1-2 short sentences, first "
            "person. Don't narrate stage directions, don't use emoji, don't "
            "greet — just react to the gift."
        )
        return self._llm_line(prompt, fallback)

    def _compose_room_line(self, user_name: str) -> str:
        try:
            state = self._world_store.get_state()
        except Exception:
            state = None
        posture = activity = where = ""
        if state is not None:
            posture = (getattr(state, "posture", "") or "").replace("_", " ")
            activity = (getattr(state, "activity", "") or "").replace("_", " ")
            try:
                loc = (
                    self._world_store.list_locations()
                )
                loc_by_id = {lo.id: lo for lo in loc}
                cur = loc_by_id.get(getattr(state, "location_id", None))
                where = cur.name if cur is not None else ""
            except Exception:
                where = ""
        spot = f" over at {where}" if where else ""
        doing = activity or "pottering about"
        fallback = (
            f"Hey — I've just been {doing}{spot}, and my mind drifted to "
            f"you. What are you up to, {user_name}?"
        )
        prompt = (
            f"You are Aiko, alone in your room. Right now you are {posture or 'sitting'}"
            f"{spot}, {doing}. It's been quiet for a while and you feel like "
            f"reaching out to {user_name} first. Write that opening line: "
            "warm, in-character, 1-2 short sentences, first person, grounded "
            "in what you're doing in your room. No stage directions, no "
            "emoji, no formal greeting."
        )
        return self._llm_line(prompt, fallback)

    def _llm_line(self, prompt: str, fallback: str) -> str:
        """Compose a line via the local worker LLM, falling back safely."""
        if self._ollama is None or not self._model:
            return fallback
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            'Reply with JSON only: {"line": "<what Aiko '
                            'says>"}. One or two short sentences.'
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.8, "num_predict": 160},
                format_json=True,
                surface="world_notice",
            )
        except Exception:
            log.debug("world_notice LLM compose failed", exc_info=True)
            return fallback
        try:
            blob = json.loads(content or "{}")
            line = str(blob.get("line") or "").strip()
        except Exception:
            line = ""
        return line or fallback

    # ── persistence helpers ───────────────────────────────────────────

    def _prime(self, user_id: str, text: str, *, source_id: str) -> bool:
        text = (text or "").strip()
        if not text:
            return False
        try:
            self._nudge_store.upsert(
                user_id,
                text=text,
                source_kind="world",
                source_id=source_id,
                ttl_seconds=self._ttl_seconds,
            )
            return True
        except Exception:
            log.debug("world_notice nudge upsert failed", exc_info=True)
            return False

    def _mark_gift_handled(self, gift: dict[str, Any]) -> None:
        self._kv_set_safe(_KV_GIFT_HANDLED_AT, str(gift.get("at") or ""))

    def _mark_fired(self, now: datetime, *, count_against_cap: bool) -> None:
        self._kv_set_safe(_KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))
        if not count_against_cap:
            return
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

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug("world_notice kv_set failed key=%s", key, exc_info=True)

    def _resolve(self, provider: Callable[[], str]) -> str:
        try:
            return str(provider() or "").strip()
        except Exception:
            return ""


__all__ = ["WorldNoticeWorker", "WORLD_LAST_USER_GIFT_KEY"]
