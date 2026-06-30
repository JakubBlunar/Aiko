"""K71 — Self-callback worker (silent producer).

During a quiet window this worker scans Aiko's own aged ``self`` /
``reflection`` memories, picks the oldest feeling / intention worth
revisiting (one that hasn't been surfaced recently), and — paced by a
multi-day cooldown — drafts ONE private cue into the ``aiko.self_callback``
kv ring. The consumer
:meth:`InnerLifeProvidersMixin._render_self_callback_block` surfaces the
newest unseen cue on a later turn (watermark-gated) so Aiko closes the
loop in her own words. This worker never speaks or fires a nudge.

Pacing (kv watermarks, all swallow-and-log):
  * ``self_callback.last_fired_at`` — wall-clock cooldown (days).

Per-memory de-dup is structural: each ring entry's ``signature`` is
``self:<memory_id>``, and the picker excludes the recent ring signatures,
so the same memory never re-drafts.

Every failure path is swallowed and logged at debug — the worst case is a
missed beat, never a crashed tick.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.affect import self_callback as _sc
from app.core.proactive.idle_worker import default_is_ready


if TYPE_CHECKING:
    from app.core.memory.memory_store import MemoryStore
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.self_callback_worker")

# Answer budget for the (rare) LLM selection pass — a tiny JSON object.
_SELECT_MAX_TOKENS = 200


_KV_LAST_FIRED_AT = "self_callback.last_fired_at"

# Aiko's own first-person memory kinds we mine for a past self-state.
_SELF_KINDS = ("self", "reflection")


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


class SelfCallbackWorker:
    """IdleWorker that drafts rare "close the loop on my own past" cues."""

    name = "self_callback"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        kv_get: Callable[[str], "str | None"],
        kv_set: Callable[[str, str], None],
        enabled_provider: Callable[[], bool] | None = None,
        interval_seconds: float = 21600.0,
        cooldown_days: float = 10.0,
        min_age_days: int = _sc.DEFAULT_MIN_AGE_DAYS,
        journal_max: int = 4,
        # Optional worker-model selection pass (more robust than the
        # regex prefilter). Falls back to the heuristic when absent.
        worker_client: "OllamaClient | None" = None,
        worker_model: str = "",
        cancel_event: "threading.Event | None" = None,
        llm_enabled_provider: Callable[[], bool] | None = None,
        user_name_provider: Callable[[], str] | None = None,
        llm_max_candidates: int = 12,
    ) -> None:
        self._memory_store = memory_store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._enabled_provider = enabled_provider
        self._interval_seconds = max(60.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_days) * 86400.0)
        self._min_age_days = max(1, int(min_age_days))
        self._journal_max = max(1, int(journal_max))
        self._worker_client = worker_client
        self._worker_model = worker_model
        self._cancel_event = cancel_event
        self._llm_enabled_provider = llm_enabled_provider
        self._user_name_provider = user_name_provider
        self._llm_max_candidates = max(1, int(llm_max_candidates))
        self._force_next = False

    # ── IdleWorker protocol ──────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return self._interval_seconds

    def is_ready(
        self, *, now: datetime, last_run_at: datetime | None,
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
                    return {"drafted": 0, "disabled": True}
            except Exception:
                pass

        now = _utcnow()
        forced = self._force_next
        self._force_next = False

        if not forced and not self._cooldown_elapsed(now):
            return {"drafted": 0, "skipped_cooldown": True}

        try:
            memories = self._memory_store.iter_by_kinds(_SELF_KINDS)
        except Exception:
            log.debug("self_callback iter_by_kinds failed", exc_info=True)
            return {"drafted": 0, "no_memories": True}

        excluded = _sc.recent_signatures(self._kv_get)
        source = "heuristic"
        candidate = None

        # LLM selection pass (more robust feeling/intention read; rejects
        # facts the regex false-positives). Best-effort -> heuristic.
        if self._llm_active():
            candidate = self._select_via_llm(memories, now, excluded)
            if candidate is not None:
                source = "llm"

        if candidate is None:
            candidate = _sc.select_candidate(
                memories,
                now=now,
                min_age_days=self._min_age_days,
                exclude_signatures=excluded,
            )
        if candidate is None:
            return {"drafted": 0, "no_candidate": True}

        _sc.append_callback(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "memory_id": candidate.memory_id,
                "kind": candidate.kind,
                "excerpt": candidate.excerpt,
                "age_days": candidate.age_days,
                "signature": candidate.signature,
                "source": source,
            },
            max_entries=self._journal_max,
        )
        self._mark_fired(now)
        log.info(
            "self-callback drafted: id=%s kind=%s age=%dd source=%s",
            candidate.memory_id,
            candidate.kind,
            candidate.age_days,
            source,
        )
        return {
            "drafted": 1,
            "memory_id": candidate.memory_id,
            "kind": candidate.kind,
            "age_days": candidate.age_days,
            "source": source,
        }

    # ── LLM selection ────────────────────────────────────────────────

    def _llm_active(self) -> bool:
        if self._worker_client is None or not self._worker_model:
            return False
        if self._llm_enabled_provider is not None:
            try:
                return bool(self._llm_enabled_provider())
            except Exception:
                return False
        return True

    def _select_via_llm(
        self, memories: Any, now: datetime, excluded: "set[str]",
    ) -> "Any | None":
        """Pick + classify a candidate via the worker model. None on any
        failure (caller falls back to the heuristic select)."""
        try:
            gathered = _sc.gather_aged_candidates(
                memories,
                now=now,
                min_age_days=self._min_age_days,
                exclude_signatures=excluded,
                max_candidates=self._llm_max_candidates,
            )
            if not gathered:
                return None
            user_name = "them"
            if self._user_name_provider is not None:
                try:
                    user_name = self._user_name_provider() or "them"
                except Exception:
                    user_name = "them"
            system, user = _sc.build_selection_prompt(
                gathered, user_display_name=user_name, assistant_name="Aiko",
            )
            chunks: list[str] = []
            stream = self._worker_client.chat_stream(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                options={"num_predict": _SELECT_MAX_TOKENS},
                model=self._worker_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface="self_callback",
            )
            for chunk in stream:
                chunks.append(chunk)
            if self._cancel_event is not None and self._cancel_event.is_set():
                return None
            pick = _sc.parse_selection(
                "".join(chunks), {c.memory_id for c in gathered},
            )
            if pick is None:
                return None
            chosen = next(
                (c for c in gathered if c.memory_id == pick["memory_id"]),
                None,
            )
            if chosen is None:
                return None
            return _sc.SelfCallbackCandidate(
                memory_id=chosen.memory_id,
                kind=pick["kind"],
                excerpt=chosen.excerpt,
                age_days=chosen.age_days,
                signature=chosen.signature,
            )
        except Exception:
            log.debug("self_callback llm selection failed", exc_info=True)
            return None

    # ── gates ────────────────────────────────────────────────────────

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
        """Arm a one-shot bypass of the cooldown gate (MCP debug)."""
        self._force_next = True

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug("self_callback kv_set failed key=%s", key, exc_info=True)
