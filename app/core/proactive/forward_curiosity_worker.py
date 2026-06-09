"""K34 — Forward curiosity worker ("I've been wondering ...").

The gap-return family already has two members: K28 ``turning_over``
surfaces what Aiko has been *thinking* about between sessions, and K36
``away_activities`` surfaces what she's been *doing*. K34 is the
forward-looking third sibling: it drafts a genuine question Aiko *wants
to ask the user* about their life ("did the espresso machine arrive?",
"how did your sister's move go?") and surfaces one on the first turn
back after a long typed absence.

This worker is the silent producer. During a quiet window it:

  * gathers candidate topics from the user's own ``future_plan``
    memories (upcoming things they mentioned) and recent ``callback``
    rows, biased by their K3 routine / usual-hours profile,
  * picks one that hasn't been drafted recently,
  * composes a short, natural forward question (deterministic template,
    optionally rephrased by the local worker LLM with a safe fallback),
  * appends ``{at, question, source, source_id}`` to a small kv_meta
    journal ring (``aiko.forward_curiosity``).

The consumer is :meth:`InnerLifeProvidersMixin._render_forward_curiosity_block`,
which on the first turn after a >= ``forward_curiosity_min_gap_hours``
gap folds the newest unseen question into the prompt as one optional,
casual "you've been wondering ..." line. This worker never speaks or
fires a proactive nudge.

Distinct from the existing curiosity systems: G3 ``IdleCuriosityWorker``
answers Aiko's *own* open questions via web search; K9
``CuriositySeedWorker`` proposes brand-new lateral topics; the
speaking-window ``CuriosityWorker`` drafts next-turn follow-ups; and
``FollowUpWorker`` fires time-window proactive nudges near an event's
``event_time``. K34 alone drafts forward questions about the *user's*
life and surfaces them passively on gap-return.

Paced by its own cooldown + daily cap (kv watermarks, local-midnight
reset). Every failure path is swallowed and logged at debug — the worst
case is a missed beat, never a broken insert or a crashed tick.
"""
from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.core.infra.user_profile import UserProfileStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.forward_curiosity_worker")


# kv_meta keys this worker owns (namespaced under ``forward_curiosity.``),
# plus the shared journal key the surfacing provider reads.
FORWARD_CURIOSITY_JOURNAL_KEY = "aiko.forward_curiosity"
_KV_LAST_FIRED_AT = "forward_curiosity.last_fired_at"
_KV_DAY = "forward_curiosity.day"
_KV_DAY_COUNT = "forward_curiosity.day_count"

# How many of the most recent ring entries to scan when de-duping a
# candidate by source id. Bounds the "don't re-draft the same plan"
# check to a small recent window.
_DEDUP_LOOKBACK = 16


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


@dataclass(frozen=True)
class QuestionCandidate:
    """One topic Aiko could ask the user about, + its provenance."""

    source: str  # "future_plan" | "callback"
    source_id: str
    topic: str


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_questions(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the forward-curiosity journal ring (oldest -> newest)."""
    try:
        raw = kv_get(FORWARD_CURIOSITY_JOURNAL_KEY)
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


def append_question(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the journal ring, trimming to ``max_entries``."""
    ring = load_questions(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(FORWARD_CURIOSITY_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("forward_curiosity journal write failed", exc_info=True)


class ForwardCuriosityWorker:
    """IdleWorker that drafts forward questions about the user's life."""

    name = "forward_curiosity"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        user_id_provider: Callable[[], str],
        user_display_name_provider: Callable[[], str],
        user_profile_store: "UserProfileStore | None" = None,
        enabled_provider: Callable[[], bool] | None = None,
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        interval_seconds: float = 1800.0,
        cooldown_seconds: float = 3600.0,
        daily_cap: int = 4,
        journal_max: int = 8,
        rng: random.Random | None = None,
    ) -> None:
        self._memory_store = memory_store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_id_provider = user_id_provider
        self._user_display_name_provider = user_display_name_provider
        self._user_profile_store = user_profile_store
        self._enabled_provider = enabled_provider
        self._ollama = ollama
        self._model = model
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._cooldown_seconds = max(0.0, float(cooldown_seconds))
        self._daily_cap = max(0, int(daily_cap))
        self._journal_max = max(1, int(journal_max))
        self._rng = rng or random.Random()
        # MCP debug: arm a specific source_id for the next run().
        self._forced_source_id: str | None = None

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
                    return {"drafted": 0, "disabled": True}
            except Exception:
                pass
        now = _utcnow()
        if not self._cooldown_elapsed(now):
            return {"drafted": 0, "skipped_cooldown": True}
        if not self._under_daily_cap(now):
            return {"drafted": 0, "skipped_daily_cap": True}

        candidate = self._pick_candidate()
        if candidate is None:
            return {"drafted": 0, "no_candidate": True}

        user_name = self._resolve(self._user_display_name_provider) or "they"
        question = self._compose_question(user_name, candidate)
        if not question:
            return {"drafted": 0, "no_question": True}

        append_question(
            self._kv_get,
            self._kv_set,
            {
                "at": now.isoformat(timespec="seconds"),
                "question": question,
                "source": candidate.source,
                "source_id": candidate.source_id,
            },
            max_entries=self._journal_max,
        )
        self._mark_fired(now)
        log.info(
            "forward_curiosity drafted: source=%s source_id=%s",
            candidate.source,
            candidate.source_id,
        )
        return {
            "drafted": 1,
            "source": candidate.source,
            "source_id": candidate.source_id,
            "question": question,
        }

    # ── candidate selection ──────────────────────────────────────────

    def _pick_candidate(self) -> QuestionCandidate | None:
        already = self._recent_source_ids()
        candidates: list[QuestionCandidate] = []

        # Upcoming things the user mentioned (espresso machine, sister's
        # move, interview). These are the strongest forward-question
        # source: a concrete event with a "how did it go?" shape.
        for mem in self._safe_list_temporal("future_plan"):
            sid = str(getattr(mem, "id", "") or "")
            topic = (getattr(mem, "content", "") or "").strip()
            if not sid or not topic or sid in already:
                continue
            candidates.append(
                QuestionCandidate(
                    source="future_plan", source_id=sid, topic=topic,
                )
            )

        # Callbacks — things Aiko earlier flagged as worth circling back
        # to. Slightly weaker but still user-centred.
        for mem in self._safe_iter_kind("callback"):
            sid = str(getattr(mem, "id", "") or "")
            topic = (getattr(mem, "content", "") or "").strip()
            if not sid or not topic or sid in already:
                continue
            candidates.append(
                QuestionCandidate(
                    source="callback", source_id=sid, topic=topic,
                )
            )

        if not candidates:
            return None

        # MCP-forced source_id wins if it's among the live candidates.
        forced = self._forced_source_id
        self._forced_source_id = None
        if forced:
            for cand in candidates:
                if cand.source_id == forced:
                    return cand

        return self._rng.choice(candidates)

    # ── question composition ─────────────────────────────────────────

    def _compose_question(
        self, user_name: str, candidate: QuestionCandidate,
    ) -> str:
        fallback = self._fallback_question(candidate.topic)
        if self._ollama is None or not self._model:
            return fallback
        routines = self._routine_hint()
        routine_clause = (
            f" Their usual rhythms: {routines}." if routines else ""
        )
        prompt = (
            f"You are Aiko. Between conversations you've been wondering "
            f"about something in {user_name}'s life. Here's the note you "
            f"have: \"{candidate.topic}\".{routine_clause} Draft the gist "
            "of ONE warm, natural follow-up question you'd want to ask "
            f"{user_name} about it next time it fits — first person, no "
            "greeting, no preamble, ONE short question, no emoji. Keep it "
            "light and genuine, not an interrogation."
        )
        try:
            content, _usage = self._ollama.chat_json(
                [
                    {
                        "role": "system",
                        "content": (
                            'Reply with JSON only: {"question": "<one '
                            'short first-person question>"}.'
                        ),
                    },
                    {"role": "user", "content": prompt},
                ],
                model=self._model,
                options={"temperature": 0.8, "num_predict": 80},
                format_json=True,
                surface="forward_curiosity",
            )
        except Exception:
            log.debug("forward_curiosity LLM compose failed", exc_info=True)
            return fallback
        try:
            blob = json.loads(content or "{}")
            line = str(blob.get("question") or "").strip()
        except Exception:
            line = ""
        return line or fallback

    def _fallback_question(self, topic: str) -> str:
        snippet = (topic or "").strip()
        if len(snippet) > 100:
            snippet = snippet[:97].rsplit(" ", 1)[0] + "…"
        return f"how {snippet} is going" if snippet else ""

    def _routine_hint(self) -> str:
        store = self._user_profile_store
        if store is None:
            return ""
        try:
            user_id = self._resolve(self._user_id_provider)
            if not user_id:
                return ""
            fields = store.fields(user_id)
        except Exception:
            return ""
        parts: list[str] = []
        for key in ("routines", "usual_hours"):
            entry = fields.get(key)
            value = (getattr(entry, "value", "") or "").strip() if entry else ""
            if value:
                parts.append(value)
        return "; ".join(parts)

    # ── gates ────────────────────────────────────────────────────────

    def _recent_source_ids(self) -> set[str]:
        ring = load_questions(self._kv_get)
        recent = ring[-_DEDUP_LOOKBACK:] if ring else []
        return {
            str(e.get("source_id"))
            for e in recent
            if e.get("source_id")
        }

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

    def force_source(self, source_id: str | None) -> None:
        """Arm a specific source_id for the next ``run()`` (MCP debug)."""
        self._forced_source_id = source_id

    def _safe_list_temporal(self, temporal_type: str) -> list["Memory"]:
        try:
            return self._memory_store.list_by_temporal_type(temporal_type)
        except Exception:
            log.debug(
                "forward_curiosity list %s failed", temporal_type,
                exc_info=True,
            )
            return []

    def _safe_iter_kind(self, kind: str) -> list["Memory"]:
        try:
            return self._memory_store.iter_by_kind(kind)
        except Exception:
            log.debug(
                "forward_curiosity iter %s failed", kind, exc_info=True,
            )
            return []

    def _kv_get_safe(self, key: str) -> str | None:
        try:
            return self._kv_get(key)
        except Exception:
            return None

    def _kv_set_safe(self, key: str, value: str) -> None:
        try:
            self._kv_set(key, value)
        except Exception:
            log.debug(
                "forward_curiosity kv_set failed key=%s", key, exc_info=True,
            )

    def _resolve(self, provider: Callable[[], str]) -> str:
        try:
            return str(provider() or "").strip()
        except Exception:
            return ""


__all__ = [
    "ForwardCuriosityWorker",
    "QuestionCandidate",
    "FORWARD_CURIOSITY_JOURNAL_KEY",
    "load_questions",
    "append_question",
]
