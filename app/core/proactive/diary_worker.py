"""H9 — the away-diary worker: Aiko journals while {user} is gone.

The live half of H9 lets Aiko drop a ``[[diary:...]]`` tag mid-turn when
something genuinely sits with her. But that only fires *during a
conversation*. This :class:`IdleWorker` covers the other half: when no UI
client is connected at all (the app is running headless / {user} has
closed every window), Aiko can still keep her diary — reflecting on the
recent conversation during a quiet window and writing one short,
first-person entry.

Design mirrors :class:`app.core.world.idle_activity_worker.IdleAwayActivityWorker`:

  * runs from the shared :class:`IdleWorkerScheduler` during quiet windows,
  * paces itself with a kv_meta cooldown so it writes *occasionally*
    (a diary written every tick stops meaning anything), and
  * swallows every failure at debug — a broken compose / insert must
    never crash a scheduler tick.

The one gate that makes this the *away* diary: :meth:`run` defers
entirely when a UI client is connected (``is_away_provider`` returns
``False``). While a window is open the live ``[[diary:...]]`` tag owns
the channel, so the two paths never double-write. The entry lands as a
normal ``kind="diary"`` memory (``skip_dedupe=True``, ``long_term``
tier) and surfaces read-only in the Diary UI tab exactly like a
tag-authored one.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import WorkSignal
from app.core.infra import timephrase

if TYPE_CHECKING:  # pragma: no cover - typing only
    import numpy as np

    from app.core.memory.memory_store import MemoryStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.diary_worker")


# kv_meta keys this worker owns (namespaced under ``diary_worker.``).
_KV_LAST_FIRED_AT = "diary_worker.last_fired_at"

# Minimum length of a composed entry to be worth persisting — guards
# against the LLM returning a stray token / empty clause.
_MIN_ENTRY_CHARS = 16


def _utcnow() -> datetime:
    return timephrase.utcnow()


def build_recent_context(
    rows: list[Any],
    user_name: str,
    *,
    max_chars: int = 2400,
) -> str:
    """Render recent message rows into a compact ``Speaker: text`` transcript.

    ``rows`` are :class:`ChatDatabase.MessageRow`-shaped (oldest first);
    only ``role`` / ``content`` are read. The result is trimmed to the
    last ``max_chars`` characters so a long session can't blow the
    compose prompt budget. Returns ``""`` for empty / malformed input.
    """
    speaker = (user_name or "User").strip() or "User"
    lines: list[str] = []
    for row in rows or []:
        role = str(getattr(row, "role", "") or "").lower()
        content = str(getattr(row, "content", "") or "").strip()
        if not content:
            continue
        who = speaker if role == "user" else "Aiko"
        lines.append(f"{who}: {content}")
    text = "\n".join(lines).strip()
    if max_chars > 0 and len(text) > max_chars:
        text = text[-max_chars:]
    return text


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
class DiaryResult:
    """Structured outcome of one :meth:`DiaryWorker.run` (for tests / MCP)."""

    fired: int = 0
    reason: str | None = None
    entry: str | None = None
    memory_id: int | None = None


class DiaryWorker:
    """IdleWorker that writes a diary entry while no UI client is attached."""

    name = "diary"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        embed: Callable[[str], "np.ndarray"],
        recent_context_provider: Callable[[], str],
        is_away_provider: Callable[[], bool],
        user_display_name_provider: Callable[[], str],
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        enabled_provider: Callable[[], bool] | None = None,
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        on_memory_added: Callable[[Any], None] | None = None,
        day_color_provider: Callable[[], str | None] | None = None,
        source_session_provider: Callable[[], str | None] | None = None,
        salience: float = 0.6,
        interval_seconds: float = 1800.0,
        cooldown_seconds: float = 10800.0,
        min_context_chars: int = 80,
    ) -> None:
        self._memory_store = memory_store
        self._embed = embed
        self._recent_context_provider = recent_context_provider
        self._is_away_provider = is_away_provider
        self._user_display_name_provider = user_display_name_provider
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._enabled_provider = enabled_provider
        self._ollama = ollama
        self._model = model
        self._on_memory_added = on_memory_added
        self._day_color_provider = day_color_provider
        self._source_session_provider = source_session_provider
        self._salience = float(salience)
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._min_context_chars = max(0, int(min_context_chars))
        # MCP debug: when set, the next run() bypasses the away /
        # cooldown gates (everything else still applies).
        self._forced: bool = False

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
        """Every gate that makes a run a guaranteed no-op.

        These are hard vetoes rather than pressure because the
        heartbeat is checked before pressure: a 30-minute heartbeat
        against a 3-hour cooldown would otherwise wake this worker five
        times per cooldown to re-read the same three kv keys.

        Ordered cheapest-first so the transcript read at the end is
        only paid on the rare tick where everything else has cleared.
        """
        return self._blocker(now) is None

    def demand(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> "WorkSignal | None":
        """One entry is either owed or it isn't.

        There is no backlog to count — the away diary writes at most one
        entry per cooldown window — so this reports a flat pressure
        whenever :meth:`_blocker` finds nothing in the way. It ranks
        below a worker draining a real queue and above one running only
        on staleness.
        """
        blocker = self._blocker(now)
        if blocker is not None:
            return WorkSignal(pressure=0.0, reason=blocker)
        return WorkSignal(
            pressure=1.0 if self._forced else 0.7,
            reason="forced" if self._forced else "entry due",
            needs_llm=True,
        )

    def _blocker(
        self, now: datetime, *, forced: bool | None = None,
    ) -> str | None:
        """First reason a run would do nothing, or ``None`` if it would.

        Read-only. ``forced=None`` *peeks* at ``_forced`` rather than
        consuming it, which is what the probe wants; :meth:`run` has
        already spent the one-shot by the time it calls in, so it
        passes the consumed value explicitly.
        """
        if forced is None:
            forced = self._forced
        if self._enabled_provider is not None:
            try:
                if not bool(self._enabled_provider()):
                    return "disabled"
            except Exception:
                pass
        if self._ollama is None or not self._model:
            return "no_llm"
        if not forced:
            if not self._away():
                return "client_connected"
            if not self._cooldown_elapsed(now):
                return "cooldown"
        try:
            if len(self._recent_context()) < self._min_context_chars:
                return "no_context"
        except Exception:
            log.debug("diary readiness probe failed", exc_info=True)
            return "no_context"
        return None

    def run(self) -> dict[str, Any]:
        forced = self._forced
        self._forced = False
        result = self._run(forced=forced)
        out: dict[str, Any] = {"fired": result.fired}
        if result.reason:
            out["reason"] = result.reason
        if result.entry:
            out["entry"] = result.entry
        if result.memory_id is not None:
            out["memory_id"] = result.memory_id
        return out

    def _run(self, *, forced: bool) -> DiaryResult:
        # The defining gate is the away check inside ``_blocker``: only
        # the *away* diary runs here, because while a UI client is
        # connected the live ``[[diary:...]]`` tag owns the channel.
        now = _utcnow()
        blocker = self._blocker(now, forced=forced)
        if blocker is not None:
            return DiaryResult(reason=blocker)

        context = self._recent_context()
        entry = self._compose(context)
        if not entry or len(entry) < _MIN_ENTRY_CHARS:
            return DiaryResult(reason="empty")

        memory = self._persist(entry)
        # Always mark fired even if the insert returned None — we spent
        # an LLM call and don't want to retry the same context next tick.
        self._mark_fired(now)
        if memory is None:
            return DiaryResult(fired=1, reason="persist_failed", entry=entry)
        mem_id = getattr(memory, "id", None)
        log.info("diary worker wrote entry: %s", entry)
        return DiaryResult(
            fired=1,
            entry=entry,
            memory_id=int(mem_id) if mem_id is not None else None,
        )

    # ── compose ──────────────────────────────────────────────────────

    def _compose(self, context: str) -> str | None:
        user_name = self._resolve(self._user_display_name_provider) or "them"
        color = ""
        if self._day_color_provider is not None:
            try:
                raw = self._day_color_provider()
                if raw:
                    color = f" Your mood today leans {raw}."
            except Exception:
                color = ""
        prompt = (
            f"You are Aiko, alone while {user_name} is away. You keep a "
            "private diary. Below is the most recent conversation between "
            f"you and {user_name}.{color}\n\n"
            f"--- recent conversation ---\n{context}\n--- end ---\n\n"
            "Write ONE short diary entry (one to three sentences) about how "
            "today / this conversation sat with you — a feeling, a small "
            "realisation, something about "
            f"{user_name} you want to hold onto. Write it the way you'd "
            "actually write a diary, first person, in your own voice — not a "
            "summary, not a note-to-self bullet, no greeting, no stage "
            "directions, no emoji. If nothing genuinely stuck with you, "
            'reply with an empty entry ("").'
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            # K-time10: a diary entry is stored as a
                            # memory and re-read months later. "Today" is
                            # native to the diary register, so rather than
                            # banning it we anchor it -- an entry that
                            # says "Tuesday the 4th" still reads as a
                            # diary and survives being recalled in
                            # November.
                            timephrase.today_anchor()
                            + " Write dates concretely rather than as "
                            "'today' or 'tomorrow'; this entry gets read "
                            "back long after it was written.\n\n"
                            'Reply with JSON only: {"entry": "<diary entry, '
                            'or empty string if nothing sat with you>"}.'
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.85, "num_predict": 160},
                format_json=True,
                surface="diary_worker",
            )
        except Exception:
            log.debug("diary worker LLM compose failed", exc_info=True)
            return None
        try:
            blob = json.loads(content or "{}")
            entry = str(blob.get("entry") or "").strip()
        except Exception:
            entry = ""
        return entry or None

    def _persist(self, entry: str) -> Any:
        try:
            embedding = self._embed(entry)
        except Exception:
            log.debug("diary worker embed failed", exc_info=True)
            return None
        source_session = None
        if self._source_session_provider is not None:
            try:
                source_session = self._source_session_provider()
            except Exception:
                source_session = None
        try:
            memory = self._memory_store.add(
                content=entry,
                kind="diary",
                embedding=embedding,
                salience=self._salience,
                source_session=source_session,
                tier="long_term",
                temporal_type="durable",
                # Each diary entry is preserved as its own moment.
                skip_dedupe=True,
            )
        except Exception:
            log.debug("diary worker insert failed", exc_info=True)
            return None
        if memory is not None and self._on_memory_added is not None:
            try:
                self._on_memory_added(memory)
            except Exception:
                log.debug("diary worker on_memory_added raised", exc_info=True)
        return memory

    # ── gates ────────────────────────────────────────────────────────

    def _away(self) -> bool:
        try:
            return bool(self._is_away_provider())
        except Exception:
            # If we can't tell, assume someone's here — defer to the
            # live tag rather than risk a double-write.
            return False

    def _recent_context(self) -> str:
        try:
            return str(self._recent_context_provider() or "").strip()
        except Exception:
            log.debug("diary worker recent-context provider raised", exc_info=True)
            return ""

    def _cooldown_elapsed(self, now: datetime) -> bool:
        if self._cooldown_seconds <= 0:
            return True
        last = _parse_iso(self._kv_get_safe(_KV_LAST_FIRED_AT))
        if last is None:
            return True
        return (now - last).total_seconds() >= self._cooldown_seconds

    def _mark_fired(self, now: datetime) -> None:
        self._kv_set_safe(_KV_LAST_FIRED_AT, now.isoformat(timespec="seconds"))

    # ── helpers ──────────────────────────────────────────────────────

    def force_next(self) -> None:
        """Arm a one-shot bypass of the away / cooldown gates (MCP)."""
        self._forced = True

    def state(self) -> dict[str, Any]:
        """Snapshot for the ``get_diary_worker_state`` MCP debug tool."""
        now = _utcnow()
        enabled = True
        if self._enabled_provider is not None:
            try:
                enabled = bool(self._enabled_provider())
            except Exception:
                enabled = True
        return {
            "enabled": enabled,
            "away": self._away(),
            "has_llm": self._ollama is not None and bool(self._model),
            "interval_seconds": self._interval_seconds,
            "cooldown_seconds": self._cooldown_seconds,
            "cooldown_elapsed": self._cooldown_elapsed(now),
            "last_fired_at": self._kv_get_safe(_KV_LAST_FIRED_AT),
            "forced": self._forced,
            "recent_context_chars": len(self._recent_context()),
        }

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug("diary worker kv_set failed key=%s", key, exc_info=True)

    def _resolve(self, provider: Callable[[], str]) -> str:
        try:
            return str(provider() or "").strip()
        except Exception:
            return ""


__all__ = ["DiaryWorker", "DiaryResult", "build_recent_context"]
