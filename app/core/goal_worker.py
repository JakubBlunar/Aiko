"""Long-term goals idle worker (K1 personality backlog).

Two-mode idle worker that keeps Aiko's small ring of personal long-term
goals alive:

1. **Cold-start bootstrap** — when :meth:`GoalStore.has_any_active`
   returns ``False`` and the persona / rolling summary have any
   substance, ask the local LLM to propose 3-5 candidate goals
   anchored on Aiko's persona traits. Survivors land via
   :meth:`GoalStore.add_goal` with ``source='worker_bootstrap'``.

2. **Reflection tick** — when at least one active goal exists, pick
   the oldest-touched goal via :meth:`GoalStore.pick_for_reflection`
   and ask the LLM for a single 1-3 sentence reflection on what she
   might have noticed / learned / wants to try next. The reflection
   lands as a ``goal_progress`` row via :meth:`GoalStore.add_progress`
   with ``source='worker'``, and the parent goal's mirror metadata
   (``last_reflected_at`` / ``last_progress_note`` / count) is
   refreshed in the same call.

Rate-limited via :class:`FactCheckRateLimiter` with
``state_key="goal_worker.rate_state"`` so a misbehaving worker can't
exhaust the daily LLM budget. Per-hour / per-day caps default to
small values (3 / 12) — goals evolve slowly, not chattily. Each
"would have called the LLM" gate increments the counter so a parse
failure still counts toward the budget.

Both branches are pure inner-life: no proactive nudge, no UI banner,
no chat write. The reflection only surfaces when Aiko brings the
goal up in conversation (via the K1 prompt block + the
``recall_goal_progress`` agent tool).
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.fact_check_rate_limiter import FactCheckRateLimiter
    from app.core.goal_store import GoalStore
    from app.core.settings import AgentSettings, MemorySettings
    from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.goal_worker")


# Bootstrap prompt -- asks the model for 3-5 candidate goals anchored on
# Aiko's persona. Returns a single JSON object (we use format_json) so
# parsing stays cheap and stable.
_BOOTSTRAP_SYSTEM_PROMPT = (
    "You are an inner-life worker for an AI companion named {assistant_name}. "
    "Propose long-term personal goals {assistant_name} is quietly working "
    "toward -- the things she would like to grow into / explore / become "
    "better at over months, NOT user-facing TODOs and NOT one-shot tasks. "
    "Lean toward modest, sensory, character-shaped goals (a craft she wants "
    "to keep at, a small ritual she wants to keep alive, a way of being she "
    "wants to lean into) over abstract self-improvement banalities. "
    "Reply with ONE JSON object on a single line and nothing else. "
    "Schema: {{\"goals\": [{{\"summary\": \"<= 160 chars, written in "
    "{assistant_name}'s warm voice as if she is naming the goal to herself\"}}, "
    "...] }}. Return between {min_goals} and {max_goals} entries."
)

_BOOTSTRAP_USER_TEMPLATE = (
    "PERSONA TRAITS:\n{persona}\n\n"
    "RECENT CONVERSATION (rolling summary, may be empty on a fresh install):\n"
    "{summary}\n\n"
    "Propose long-term goals now."
)


# Reflection prompt -- a single goal + the last few progress notes;
# returns one short reflection ("note") in JSON form.
_REFLECTION_SYSTEM_PROMPT = (
    "You are an inner-life worker for an AI companion named {assistant_name}. "
    "{assistant_name} is reflecting briefly on ONE of her long-term personal "
    "goals during a quiet moment. Write 1-3 short sentences in her warm "
    "internal voice: what she's noticed about this goal, how it's been "
    "going, or one small next step. Keep it grounded, no platitudes, no "
    "self-help filler. Honour her recent reflections (the LAST entry is the "
    "most recent) and avoid repeating them word-for-word. "
    "Reply with ONE JSON object on a single line and nothing else. "
    "Schema: {{\"note\": \"<= 280 chars\"}}."
)

_REFLECTION_USER_TEMPLATE = (
    "GOAL:\n{goal_summary}\n\n"
    "RECENT REFLECTIONS (oldest to newest, may be empty):\n{history}\n\n"
    "Write her next short reflection now."
)


_MIN_GOALS = 3
_MAX_GOALS = 5
_MAX_TOKENS_BOOTSTRAP = 320
_MAX_TOKENS_REFLECTION = 180
_MAX_SUMMARY_CHARS = 160
_MAX_NOTE_CHARS = 280
_MAX_PERSONA_CHARS = 800
_MAX_ROLLING_SUMMARY_CHARS = 900
_MAX_REFLECTION_HISTORY = 4


_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _trim(text: str | None, *, max_chars: int) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip(",;: ") + "…"


def _extract_persona_traits(raw: str) -> str:
    """Pluck the most goal-shaping persona lines.

    Mirrors the heuristic in :mod:`curiosity_seed_worker` but biased
    toward sections that hint at "what she cares about long-term":
    Self-image, Inner life, Curiosity, Voice, Mood. Falls back to the
    first ~800 chars when no section header is found.
    """
    if not raw:
        return ""
    lines = raw.splitlines()
    keep: list[str] = []
    capture = False
    for line in lines:
        stripped = line.strip()
        if not stripped:
            if capture:
                capture = False
            continue
        lower = stripped.lower().rstrip(":")
        if lower in {
            "self-image",
            "self image",
            "inner life",
            "voice",
            "tone",
            "curiosity",
            "interests",
            "novelty",
            "mood",
        }:
            capture = True
            keep.append(stripped)
            continue
        if capture:
            keep.append(stripped)
        if sum(len(line) + 1 for line in keep) > _MAX_PERSONA_CHARS:
            break
    if not keep:
        return _trim(raw, max_chars=_MAX_PERSONA_CHARS)
    joined = "\n".join(keep)
    return _trim(joined, max_chars=_MAX_PERSONA_CHARS)


class GoalWorker:
    """IdleWorker that keeps Aiko's long-term goals ring alive."""

    name = "goal_worker"

    def __init__(
        self,
        *,
        goal_store: "GoalStore",
        ollama: "OllamaClient",
        chat_model: str,
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        rate_limiter: "FactCheckRateLimiter",
        persona_provider: Callable[[], str] | None = None,
        rolling_summary_provider: Callable[[], str] | None = None,
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
        notify_memory_added: Callable[[dict[str, Any]], None] | None = None,
        notify_memory_updated: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._goal_store = goal_store
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._rate_limiter = rate_limiter
        self._persona_provider = persona_provider
        self._rolling_summary_provider = rolling_summary_provider
        self._user_display_name_provider = user_display_name_provider
        self._assistant_display_name_provider = assistant_display_name_provider
        self._notify_memory_added = notify_memory_added
        self._notify_memory_updated = notify_memory_updated
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ───────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings,
                "goal_reflection_interval_seconds",
                3600.0,
            )
        )

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not bool(getattr(self._agent_settings, "goals_enabled", True)):
            return False
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        ):
            return False
        return True

    def run(self) -> dict[str, Any]:
        if not bool(getattr(self._agent_settings, "goals_enabled", True)):
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}
        now = self._clock()

        try:
            has_any = self._goal_store.has_any_active()
        except Exception:
            log.debug("goal_worker has_any_active failed", exc_info=True)
            has_any = True  # fail closed -- skip bootstrap

        if not has_any:
            return self._run_bootstrap(now=now)
        return self._run_reflection(now=now)

    # ── bootstrap path ────────────────────────────────────────────────

    def _run_bootstrap(self, *, now: datetime) -> dict[str, Any]:
        if not bool(
            getattr(self._agent_settings, "goal_worker_bootstrap_enabled", True)
        ):
            return {"skipped": True, "reason": "bootstrap_disabled"}
        if not self._rate_limiter.allow(now=now):
            return {"skipped": True, "reason": "rate_limited"}

        persona_text = self._persona_block()
        summary_text = self._summary_block()

        t0 = time.monotonic()
        try:
            candidates = self._call_bootstrap_llm(
                persona_text=persona_text,
                summary_text=summary_text,
            )
        except Exception:
            log.warning("goal_worker bootstrap LLM raised", exc_info=True)
            return {"errored": True, "reason": "llm_call", "branch": "bootstrap"}
        llm_ms = (time.monotonic() - t0) * 1000.0
        if self._cancel_event.is_set():
            return {"cancelled": True, "branch": "bootstrap"}
        if not candidates:
            log.info(
                "goal_worker bootstrap: no candidates parsed (llm_ms=%.0f)",
                llm_ms,
            )
            return {
                "branch": "bootstrap",
                "checked": 0,
                "wrote": 0,
                "reason": "no_candidates",
                "llm_ms": int(llm_ms),
            }

        max_active = self._goal_store.max_active
        wrote: list[int] = []
        rejected_dup = 0
        for candidate in candidates:
            if len(wrote) >= max_active:
                break
            summary = _trim(
                candidate.get("summary"),
                max_chars=_MAX_SUMMARY_CHARS,
            )
            if not summary:
                continue
            try:
                mem = self._goal_store.add_goal(
                    summary=summary,
                    source="worker_bootstrap",
                )
            except Exception:
                log.debug("goal_worker add_goal failed", exc_info=True)
                continue
            if mem is None:
                rejected_dup += 1
                continue
            wrote.append(int(mem.id))
            if self._notify_memory_added is not None:
                try:
                    self._notify_memory_added(mem.to_dict())
                except Exception:
                    log.debug(
                        "goal_worker notify_added failed", exc_info=True,
                    )

        log.info(
            "goal_worker bootstrap done: wrote=%d candidates=%d rejected_dedupe=%d llm_ms=%.0f",
            len(wrote),
            len(candidates),
            rejected_dup,
            llm_ms,
        )
        return {
            "branch": "bootstrap",
            "checked": len(candidates),
            "wrote": len(wrote),
            "memory_ids": wrote,
            "rejected_dedupe": rejected_dup,
            "llm_ms": int(llm_ms),
        }

    # ── reflection path ───────────────────────────────────────────────

    def _run_reflection(self, *, now: datetime) -> dict[str, Any]:
        try:
            goal = self._goal_store.pick_for_reflection()
        except Exception:
            log.debug("goal_worker pick_for_reflection failed", exc_info=True)
            return {"errored": True, "reason": "pick_for_reflection"}
        if goal is None:
            return {"skipped": True, "reason": "no_active_goals"}
        meta = goal.metadata or {}
        summary = str(meta.get("summary") or goal.content or "").strip()
        if not summary:
            return {"skipped": True, "reason": "missing_summary"}
        if not self._rate_limiter.allow(now=now):
            return {"skipped": True, "reason": "rate_limited"}

        history = self._reflection_history(int(goal.id))

        t0 = time.monotonic()
        try:
            note = self._call_reflection_llm(
                goal_summary=summary,
                history=history,
            )
        except Exception:
            log.warning("goal_worker reflection LLM raised", exc_info=True)
            return {"errored": True, "reason": "llm_call", "branch": "reflection"}
        llm_ms = (time.monotonic() - t0) * 1000.0
        if self._cancel_event.is_set():
            return {"cancelled": True, "branch": "reflection"}
        cleaned = _trim(note, max_chars=_MAX_NOTE_CHARS)
        if not cleaned:
            log.info(
                "goal_worker reflection: empty note parsed (goal_id=%s llm_ms=%.0f)",
                goal.id,
                llm_ms,
            )
            return {
                "branch": "reflection",
                "goal_id": int(goal.id),
                "wrote": 0,
                "reason": "empty_note",
                "llm_ms": int(llm_ms),
            }

        try:
            progress = self._goal_store.add_progress(
                goal_id=int(goal.id),
                note=cleaned,
                source="worker",
            )
        except Exception:
            log.debug("goal_worker add_progress failed", exc_info=True)
            return {
                "branch": "reflection",
                "goal_id": int(goal.id),
                "wrote": 0,
                "reason": "add_progress_failed",
                "llm_ms": int(llm_ms),
            }
        if progress is None:
            return {
                "branch": "reflection",
                "goal_id": int(goal.id),
                "wrote": 0,
                "reason": "progress_dedupe",
                "llm_ms": int(llm_ms),
            }
        if self._notify_memory_added is not None:
            try:
                self._notify_memory_added(progress.to_dict())
            except Exception:
                log.debug(
                    "goal_worker notify_added failed", exc_info=True,
                )
        # The goal row's mirror metadata moved -- broadcast so the
        # Memory tab refreshes the "last reflection" line live.
        if self._notify_memory_updated is not None:
            try:
                refreshed = self._goal_store._memory_store.get(int(goal.id))
                if refreshed is not None:
                    self._notify_memory_updated(refreshed.to_dict())
            except Exception:
                log.debug(
                    "goal_worker notify_updated failed", exc_info=True,
                )

        log.info(
            "goal_worker reflection done: goal_id=%s progress_id=%s "
            "note=%r llm_ms=%.0f",
            goal.id,
            progress.id,
            cleaned[:120],
            llm_ms,
        )
        return {
            "branch": "reflection",
            "goal_id": int(goal.id),
            "progress_id": int(progress.id),
            "wrote": 1,
            "note": cleaned,
            "llm_ms": int(llm_ms),
        }

    # ── context pack ──────────────────────────────────────────────────

    def _persona_block(self) -> str:
        if self._persona_provider is None:
            return ""
        try:
            raw = self._persona_provider() or ""
        except Exception:
            log.debug("persona provider raised", exc_info=True)
            return ""
        return _extract_persona_traits(raw)

    def _summary_block(self) -> str:
        if self._rolling_summary_provider is None:
            return ""
        try:
            raw = self._rolling_summary_provider() or ""
        except Exception:
            log.debug("summary provider raised", exc_info=True)
            return ""
        return _trim(raw, max_chars=_MAX_ROLLING_SUMMARY_CHARS)

    def _reflection_history(self, goal_id: int) -> str:
        try:
            rows = self._goal_store.list_progress(int(goal_id))
        except Exception:
            log.debug("goal_worker list_progress failed", exc_info=True)
            return ""
        if not rows:
            return ""
        # newest-first from the store; reverse so the LLM reads the
        # oldest first and recognises the last entry as "most recent".
        recent = list(reversed(rows[:_MAX_REFLECTION_HISTORY]))
        lines: list[str] = []
        for mem in recent:
            note = (mem.metadata or {}).get("note") or mem.content or ""
            note = _trim(note, max_chars=_MAX_NOTE_CHARS)
            if note:
                lines.append(f"- {note}")
        return "\n".join(lines)

    # ── LLM ───────────────────────────────────────────────────────────

    def _call_bootstrap_llm(
        self,
        *,
        persona_text: str,
        summary_text: str,
    ) -> list[dict[str, Any]]:
        assistant_name = self._resolve_assistant_name()
        system = _BOOTSTRAP_SYSTEM_PROMPT.format(
            assistant_name=assistant_name,
            min_goals=_MIN_GOALS,
            max_goals=_MAX_GOALS,
        )
        user_payload = _BOOTSTRAP_USER_TEMPLATE.format(
            persona=persona_text or "(persona unavailable)",
            summary=summary_text or "(no recent summary)",
        )
        return self._call_json_llm(
            system=system,
            user=user_payload,
            max_tokens=_MAX_TOKENS_BOOTSTRAP,
            surface="goal_worker_bootstrap",
            parser=self._parse_bootstrap_response,
            temperature=0.85,
        )

    def _call_reflection_llm(
        self,
        *,
        goal_summary: str,
        history: str,
    ) -> str:
        assistant_name = self._resolve_assistant_name()
        system = _REFLECTION_SYSTEM_PROMPT.format(
            assistant_name=assistant_name,
        )
        user_payload = _REFLECTION_USER_TEMPLATE.format(
            goal_summary=goal_summary,
            history=history or "(no prior reflections yet)",
        )
        parsed = self._call_json_llm(
            system=system,
            user=user_payload,
            max_tokens=_MAX_TOKENS_REFLECTION,
            surface="goal_worker_reflection",
            parser=self._parse_reflection_response,
            temperature=0.7,
        )
        if isinstance(parsed, str):
            return parsed
        return ""

    def _call_json_llm(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int,
        surface: str,
        parser: Callable[[str], Any],
        temperature: float,
    ) -> Any:
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        chunks: list[str] = []
        try:
            stream = self._ollama.chat_stream(
                messages,
                options={
                    "num_predict": int(max_tokens),
                    "temperature": float(temperature),
                },
                model=self._chat_model,
                stop_event=self._cancel_event,
                format_json=True,
                surface=surface,
            )
            for chunk in stream:
                chunks.append(chunk)
        except Exception:
            log.warning(
                "goal_worker chat_stream raised (surface=%s)",
                surface,
                exc_info=True,
            )
            return None
        if self._cancel_event.is_set():
            return None
        raw = "".join(chunks).strip()
        if not raw:
            return None
        try:
            return parser(raw)
        except Exception:
            log.debug(
                "goal_worker parser raised (surface=%s)", surface, exc_info=True,
            )
            return None

    @staticmethod
    def _parse_bootstrap_response(raw: str) -> list[dict[str, Any]]:
        text = raw.strip()
        match = _JSON_OBJECT_RE.search(text)
        if match is None:
            return []
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, dict):
            return []
        goals = parsed.get("goals")
        if not isinstance(goals, list):
            return []
        out: list[dict[str, Any]] = []
        for entry in goals[:_MAX_GOALS]:
            if not isinstance(entry, dict):
                continue
            summary = str(entry.get("summary") or "").strip()
            if not summary:
                continue
            out.append({"summary": summary})
        return out

    @staticmethod
    def _parse_reflection_response(raw: str) -> str:
        text = raw.strip()
        match = _JSON_OBJECT_RE.search(text)
        if match is None:
            return ""
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return ""
        if not isinstance(parsed, dict):
            return ""
        note = str(parsed.get("note") or "").strip()
        return note

    # ── name resolution ───────────────────────────────────────────────

    def _resolve_user_name(self) -> str:
        if self._user_display_name_provider is None:
            return "the user"
        try:
            name = self._user_display_name_provider() or "the user"
        except Exception:
            return "the user"
        return name or "the user"

    def _resolve_assistant_name(self) -> str:
        if self._assistant_display_name_provider is None:
            return "Aiko"
        try:
            name = self._assistant_display_name_provider() or "Aiko"
        except Exception:
            return "Aiko"
        return name or "Aiko"


__all__ = ["GoalWorker"]
