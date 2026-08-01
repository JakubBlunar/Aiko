"""K71 — Self-callback worker (silent producer).

During a quiet window this worker scans Aiko's own aged ``self`` /
``reflection`` memories, picks the oldest feeling / intention worth
revisiting (one that hasn't been surfaced recently), and queues ONE
private cue into ``cue_pool``. The consumer
:meth:`InnerLifeProvidersMixin._render_self_callback_block` claims it on a
later turn so Aiko closes the loop in her own words. This worker never
speaks or fires a nudge.

Where the pacing went
---------------------
This cue is rare by *nature*, not by scarcity, and until the pool that
rarity was an accident of production: a ten-day ``last_fired_at``
cooldown here meant one cue drafted a fortnight, and one drafted meant
one surfaced. Deficit-driven scheduling removes the accident -- an empty
shelf is pressure, and pressure is admission -- so the rarity had to be
stated somewhere it actually belongs. It now lives on the type's
``CuePolicy.surface_cooldown_hours``, which paces how often Aiko *opens*
one of these rather than how often the worker thinks about it.

That swap is also a correction. A producer cooldown throttles the wrong
thing: it means that when the shelf runs dry the cue simply stops
existing for ten days, and a good moment during those ten days finds
nothing to say.

Per-memory de-dup is structural and doubled, matching the other pooled
workers: each ring entry's ``signature`` is ``self:<memory_id>`` and the
picker excludes the recent ring signatures, while the pool separately
refuses any excerpt it already holds a cue for in any state -- including
ones already used or expired unwanted, which the ring forgets.

The ``aiko.self_callback`` kv ring is still written. Nothing surfaces
from it any more; it stays because ``get_self_callback_state`` reads it
and because it is the signature source above.

Every failure path is swallowed and logged at debug — the worst case is a
missed beat, never a crashed tick.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import TYPE_CHECKING, Any, Callable

from app.core.affect import self_callback as _sc
from app.core.proactive.cue_producer import CueProducer, StoreProvider
from app.core.proactive.idle_worker import WorkSignal
from app.core.infra import timephrase


if TYPE_CHECKING:
    from app.core.memory.memory_store import MemoryStore
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.self_callback_worker")

# Answer budget for the (rare) LLM selection pass — a tiny JSON object.
_SELECT_MAX_TOKENS = 200


# Aiko's own first-person memory kinds we mine for a past self-state.
_SELF_KINDS = ("self", "reflection")


def _utcnow() -> datetime:
    return timephrase.utcnow()


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
        cue_store_provider: StoreProvider | None = None,
        interval_seconds: float = 21600.0,
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
        self._cues = CueProducer("self_callback", cue_store_provider)
        self._interval_seconds = max(60.0, float(interval_seconds))
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
        # Hard veto only. The interval is the heartbeat now; how much
        # stock is on the shelf decides the rest.
        return self._enabled()

    def demand(
        self, *, now: datetime, last_run_at: datetime | None,
    ) -> WorkSignal | None:
        """Pressure from an empty shelf, not from a wall-clock cooldown.

        Declares ``needs_llm`` honestly, because on the runs where the
        selection pass is active this worker contends with the chat model
        and the scheduler's LLM lane needs to know.
        """
        if not self._enabled():
            return WorkSignal(pressure=0.0, reason="disabled")
        return self._cues.demand(needs_llm=self._llm_active())

    def run(self) -> dict[str, Any]:
        forced = self._force_next
        self._force_next = False
        if not self._enabled():
            return {"drafted": 0, "disabled": True}

        now = _utcnow()
        try:
            memories = self._memory_store.iter_by_kinds(_SELF_KINDS)
        except Exception:
            log.debug("self_callback iter_by_kinds failed", exc_info=True)
            return {"drafted": 0, "no_memories": True}

        excluded = _sc.recent_signatures(self._kv_get)
        pooled = set() if forced else self._cues.spoken_for()
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
        if self._already_pooled(candidate, pooled):
            return {"drafted": 0, "already_pooled": True}

        entry = {
            "at": now.isoformat(timespec="seconds"),
            "memory_id": candidate.memory_id,
            "kind": candidate.kind,
            "excerpt": candidate.excerpt,
            "age_days": candidate.age_days,
            "signature": candidate.signature,
            "source": source,
        }
        _sc.append_callback(
            self._kv_get, self._kv_set, entry, max_entries=self._journal_max,
        )
        cue_id = self._cues.publish(
            candidate.excerpt,
            _sc.render_inner_life_block(
                candidate.kind,
                candidate.excerpt,
                candidate.age_days,
                user_display_name=self._user_name(),
            ),
            payload=entry,
        )
        log.info(
            "self-callback drafted: id=%s kind=%s age=%dd source=%s cue=%s",
            candidate.memory_id,
            candidate.kind,
            candidate.age_days,
            source,
            cue_id,
        )
        return {
            "drafted": 1,
            "memory_id": candidate.memory_id,
            "kind": candidate.kind,
            "age_days": candidate.age_days,
            "source": source,
            "cue_id": cue_id,
        }

    def _already_pooled(self, candidate: Any, pooled: "set[str]") -> bool:
        """Has the pool seen this excerpt before, in any state?

        Wider than the ring's signature check beside it, which only looks
        back eight entries. A memory whose callback was already *used*, or
        that expired because no moment for it ever came, is at least as
        poor a candidate as one still waiting.
        """
        if not pooled:
            return False
        from app.core.proactive.cue_store import normalise_subject

        return normalise_subject(candidate.excerpt) in pooled

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
            system, user = _sc.build_selection_prompt(
                gathered,
                user_display_name=self._user_name(),
                assistant_name="Aiko",
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

    # ── gates / helpers ──────────────────────────────────────────────

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _user_name(self) -> str:
        if self._user_name_provider is None:
            return "them"
        try:
            return self._user_name_provider() or "them"
        except Exception:
            return "them"

    def force_next(self) -> None:
        """Arm the next ``run()`` to ignore what the pool already holds."""
        self._force_next = True
