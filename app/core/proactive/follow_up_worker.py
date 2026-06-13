"""Schedule follow-up *cues* for user-mentioned future plans (schema v10).

When the user tells Aiko about something upcoming ("gym tonight at 8",
"job interview on Thursday"), the :class:`MemoryExtractor` writes that
as a memory with ``temporal_type='future_plan'`` and an absolute
``event_time``. The :class:`MemoryDecayWorker` later flips the row to
``past_event`` once the time has passed.

The piece in between — Aiko bringing it back up ("how was it?") at the
*right* moment — is what this worker covers. **It is a silent producer,
not a speaker** (the K34 ``ForwardCuriosityWorker`` pattern). It does
NOT write a verbatim line into the prepared-nudge slot — an earlier
version did, which leaked an internal directive ("if the conversation
drifts there, ask how it went — don't open with it") straight into the
chat as if it were Aiko's reply. Instead it drafts a **cue** into a
small ``kv_meta`` journal ring (``aiko.follow_up_cues``).

The consumer is
:meth:`InnerLifeProvidersMixin._render_follow_up_block`, which folds the
newest unseen cue into the next turn's system prompt as one optional,
private "you can ask how it went" hint. Aiko then does the talking
herself, in her own voice, when it fits the flow — exactly like the
``(was planned for … — should be done by now)`` retrieval tag, but
guaranteed to surface by *time* rather than by RAG relevance.

It runs through the shared :class:`IdleWorkerScheduler` so it inherits
the quiet-window gate (no fighting for GPU during a turn).

Behaviour:

  - On each tick, scan ``MemoryStore`` for ``future_plan`` rows whose
    ``event_time`` is within a short window centred on now (default
    lookahead 30 min / lookback 4 h). The window catches a tick that
    fires shortly after the moment without re-triggering for plans that
    already had their cue drafted.
  - For each match, draft a cue ``{at, plan, clock, question,
    source_id, event_time}`` and append it to the journal ring. The
    ``plan`` is a deterministic second-person reshaping of the memory
    ("you were planning to take a bath …"); the optional ``question``
    is a natural retrospective phrasing drafted by the local worker LLM
    (safe empty fallback). Neither is ever spoken verbatim — the
    consumer renders the cue as a private prompt hint.
  - Stamp ``metadata.followup_fired_at`` on the memory so subsequent
    ticks skip it. Idempotent.

Failure modes are tolerated everywhere: a missing memory store, a
malformed ``event_time``, a kv write failure — all get logged at debug
and the worker moves on. The worst outcome is a missed cue, never a
corrupt DB row.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.memory.memory_store import Memory, MemoryStore
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.follow_up_worker")


# Shared journal key the surfacing provider reads.
FOLLOW_UP_JOURNAL_KEY = "aiko.follow_up_cues"

# Window around ``event_time`` during which a tick will draft the cue.
# Wide enough that a typical 5-15 minute scheduler interval catches the
# moment, narrow enough that a plan only triggers once.
_DEFAULT_LOOKAHEAD = timedelta(minutes=30)
_DEFAULT_LOOKBACK = timedelta(hours=4)
# Maximum cues drafted per sweep so a backlog (e.g. after a long offline
# gap) doesn't flood the ring with check-ins.
_DEFAULT_MAX_PER_RUN = 3
_DEFAULT_JOURNAL_MAX = 8


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


def _humanize_clock(when: datetime) -> str:
    """Render an event_time as a short, friendly clock string."""
    return when.astimezone().strftime("%H:%M").lstrip("0") or "now"


# ── journal helpers (shared with the surfacing provider) ────────────────


def load_follow_up_cues(
    kv_get: Callable[[str], str | None],
) -> list[dict[str, Any]]:
    """Return the follow-up cue journal ring (oldest -> newest)."""
    try:
        raw = kv_get(FOLLOW_UP_JOURNAL_KEY)
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


def append_follow_up_cue(
    kv_get: Callable[[str], str | None],
    kv_set: Callable[[str, str], None],
    entry: dict[str, Any],
    *,
    max_entries: int,
) -> None:
    """Append ``entry`` to the cue ring, trimming to ``max_entries``."""
    ring = load_follow_up_cues(kv_get)
    ring.append(entry)
    if max_entries > 0 and len(ring) > max_entries:
        ring = ring[-max_entries:]
    try:
        kv_set(FOLLOW_UP_JOURNAL_KEY, json.dumps(ring))
    except Exception:
        log.debug("follow_up journal write failed", exc_info=True)


# Pronoun subjects we strip from the front of a third-person memory
# before reshaping it into a second-person line. The user's display
# name is handled separately (it's dynamic).
_SUBJECT_PREFIXES: tuple[str, ...] = ("they ", "he ", "she ", "the user ")

# Ordered (leading-phrase -> past-tense second-person) rewrites. The
# MemoryExtractor writes future_plan content in third person
# ("Jacob plans to take a bath …", "Jacob has a meeting"); once the
# subject is stripped we convert the leading verb so the cue reads as a
# natural retrospective check-in. Longest / most specific prefixes first
# so e.g. "is going to" wins over "is".
_PLAN_VERB_REWRITES: tuple[tuple[str, str], ...] = (
    ("is planning to ", "were planning to "),
    ("was planning to ", "were planning to "),
    ("is going to ", "were going to "),
    ("are going to ", "were going to "),
    ("is about to ", "were about to "),
    ("is hoping to ", "were hoping to "),
    ("is having ", "were having "),
    ("is meeting ", "were meeting "),
    ("plans to ", "were planning to "),
    ("planned to ", "were planning to "),
    ("plan to ", "were planning to "),
    ("intends to ", "were planning to "),
    ("intended to ", "were planning to "),
    ("wants to ", "wanted to "),
    ("wanted to ", "wanted to "),
    ("would like to ", "wanted to "),
    ("hopes to ", "were hoping to "),
    ("hoped to ", "were hoping to "),
    ("needs to ", "needed to "),
    ("needed to ", "needed to "),
    ("has to ", "needed to "),
    ("will be ", "were "),
    ("will ", "were going to "),
    ("has a ", "had a "),
    ("have a ", "had a "),
    ("had a ", "had a "),
)


def _to_second_person_plan(content: str, user_display_name: str) -> str | None:
    """Reshape a third-person future_plan memory into a second-person
    predicate, or ``None`` when it can't be done cleanly.

    "Jacob plans to take a bath and watch anime later this evening."
        -> "you were planning to take a bath and watch anime later this
            evening"
    "Jacob has a meeting" -> "you had a meeting"
    """
    text = (content or "").strip().rstrip(".!?").strip()
    if not text:
        return None
    low = text.lower()
    name = (user_display_name or "").strip()
    stripped: str | None = None
    if name and low.startswith(name.lower() + " "):
        stripped = text[len(name) + 1 :]
    else:
        for pre in _SUBJECT_PREFIXES:
            if low.startswith(pre):
                stripped = text[len(pre) :]
                break
    if stripped is None:
        return None
    s_low = stripped.lower()
    for prefix, replacement in _PLAN_VERB_REWRITES:
        if s_low.startswith(prefix):
            return ("you " + replacement + stripped[len(prefix) :]).strip()
    return None


def _plan_summary(content: str, user_display_name: str) -> str:
    """Best-effort second-person plan summary for the cue.

    Falls back to a trimmed snippet of the raw content when the memory
    isn't phrased in a way we can cleanly reshape.
    """
    predicate = _to_second_person_plan(content, user_display_name)
    if predicate:
        if len(predicate) > 160:
            predicate = predicate[:157].rsplit(" ", 1)[0] + "…"
        return predicate
    snippet = (content or "").strip().rstrip(".!?").strip()
    if len(snippet) > 140:
        snippet = snippet[:137].rsplit(" ", 1)[0] + "…"
    return f"had this coming up: {snippet}" if snippet else "had something planned"


class FollowUpWorker:
    """IdleWorker that drafts follow-up cues for past future-plans."""

    name = "follow_up"

    def __init__(
        self,
        *,
        memory_store: "MemoryStore",
        kv_get: Callable[[str], str | None],
        kv_set: Callable[[str, str], None],
        user_id_provider: Callable[[], str],
        user_display_name_provider: Callable[[], str],
        enabled_provider: Callable[[], bool] | None = None,
        ollama: "ChatClient | None" = None,
        model: str | None = None,
        interval_seconds: float = 300.0,
        lookahead: timedelta = _DEFAULT_LOOKAHEAD,
        lookback: timedelta = _DEFAULT_LOOKBACK,
        max_per_run: int = _DEFAULT_MAX_PER_RUN,
        journal_max: int = _DEFAULT_JOURNAL_MAX,
    ) -> None:
        self._memory_store = memory_store
        self._kv_get = kv_get
        self._kv_set = kv_set
        self._user_id_provider = user_id_provider
        self._user_display_name_provider = user_display_name_provider
        self._enabled_provider = enabled_provider
        self._ollama = ollama
        self._model = model
        self._interval_seconds = max(30.0, float(interval_seconds))
        self._lookahead = lookahead
        self._lookback = lookback
        self._max_per_run = max(1, int(max_per_run))
        self._journal_max = max(1, int(journal_max))
        # MCP debug: arm a specific memory id whose cue should be drafted
        # on the next run() regardless of the event-time window / already-
        # fired gate.
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
        if not self._enabled():
            return False
        return default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        )

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"fired": 0, "disabled": True}
        now = _utcnow()
        try:
            user_id = self._resolve_user_id()
            user_name = self._resolve_user_name()
        except Exception:
            log.debug("follow_up: identity resolution failed", exc_info=True)
            return {"fired": 0, "skipped_no_user": True}
        if not user_id:
            return {"fired": 0, "skipped_no_user": True}

        try:
            candidates = self._memory_store.list_by_temporal_type(
                "future_plan",
            )
        except Exception:
            log.debug("follow_up: list future_plan failed", exc_info=True)
            return {"fired": 0, "errored": True}

        forced = self._forced_source_id
        self._forced_source_id = None

        fired = 0
        considered = 0
        skipped_already_fired = 0
        skipped_out_of_window = 0
        for mem in candidates:
            if fired >= self._max_per_run:
                break
            considered += 1
            is_forced = forced is not None and str(mem.id) == forced
            metadata = mem.metadata or {}
            if not is_forced and metadata.get("followup_fired_at"):
                skipped_already_fired += 1
                continue
            event_dt = _parse_iso(mem.event_time)
            if event_dt is None:
                # Plans without a precise event_time don't get a cue —
                # there's no moment to anchor to. Forced repro uses "now".
                if is_forced:
                    event_dt = now
                else:
                    skipped_out_of_window += 1
                    continue
            if not is_forced:
                delta = event_dt - now
                if delta > self._lookahead:
                    # Still too far in the future.
                    skipped_out_of_window += 1
                    continue
                if delta < -self._lookback:
                    # Too far in the past — the decay worker will flip the
                    # row to past_event and we'll never need to follow up.
                    # Mark it fired-equivalent so we stop scanning it.
                    self._mark_fired(mem, when=now, dropped=True)
                    continue

            try:
                plan = _plan_summary(mem.content or "", user_name)
                clock = _humanize_clock(event_dt)
                question = self._draft_question(
                    user_name, mem.content or "", clock,
                )
                append_follow_up_cue(
                    self._kv_get,
                    self._kv_set,
                    {
                        "at": now.isoformat(timespec="seconds"),
                        "plan": plan,
                        "clock": clock,
                        "question": question,
                        "source_id": str(mem.id),
                        "event_time": event_dt.isoformat(),
                    },
                    max_entries=self._journal_max,
                )
            except Exception:
                log.debug(
                    "follow_up: cue draft failed for memory id=%s",
                    mem.id,
                    exc_info=True,
                )
                continue

            self._mark_fired(mem, when=now)
            fired += 1
            log.info(
                "follow_up cue primed for memory id=%s (%s @ %s)",
                mem.id,
                (mem.content or "")[:80],
                event_dt.isoformat(),
            )

        return {
            "fired": fired,
            "considered": considered,
            "skipped_already_fired": skipped_already_fired,
            "skipped_out_of_window": skipped_out_of_window,
        }

    # ── question composition ─────────────────────────────────────────

    def _draft_question(self, user_name: str, content: str, clock: str) -> str:
        """Draft a natural retrospective question on the local worker LLM.

        Returns ``""`` on any failure / no client — the consumer renders
        a perfectly good cue from ``plan`` alone, so the LLM phrasing is
        a nicety, never load-bearing.
        """
        if self._ollama is None or not self._model:
            return ""
        snippet = (content or "").strip()
        if len(snippet) > 200:
            snippet = snippet[:197].rsplit(" ", 1)[0] + "…"
        if not snippet:
            return ""
        prompt = (
            f"You are Aiko. Earlier {user_name} mentioned: \"{snippet}\" "
            f"(around {clock}). That time has now passed. Draft the gist of "
            "ONE warm, natural follow-up question you'd ask to check how it "
            "went — first person, no greeting, no preamble, ONE short "
            "question, no emoji."
        )
        try:
            content_out, _usage = self._ollama.chat_json(
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
                options={"temperature": 0.7, "num_predict": 80},
                format_json=True,
                surface="follow_up",
            )
        except Exception:
            log.debug("follow_up LLM compose failed", exc_info=True)
            return ""
        try:
            blob = json.loads(content_out or "{}")
            line = str(blob.get("question") or "").strip()
        except Exception:
            line = ""
        return line

    # ── helpers ──────────────────────────────────────────────────────

    def force_source(self, source_id: str | None) -> None:
        """Arm a specific memory id for the next ``run()`` (MCP debug).

        When set, that memory's cue is drafted on the next run regardless
        of the event-time window or the ``followup_fired_at`` gate.
        """
        self._forced_source_id = str(source_id) if source_id else None

    def _enabled(self) -> bool:
        if self._enabled_provider is None:
            return True
        try:
            return bool(self._enabled_provider())
        except Exception:
            return True

    def _resolve_user_id(self) -> str:
        try:
            return str(self._user_id_provider() or "").strip()
        except Exception:
            return ""

    def _resolve_user_name(self) -> str:
        try:
            name = str(self._user_display_name_provider() or "").strip()
        except Exception:
            name = ""
        return name or "the user"

    def _mark_fired(
        self,
        memory: "Memory",
        *,
        when: datetime,
        dropped: bool = False,
    ) -> None:
        """Stamp ``metadata.followup_fired_at`` so we don't fire again.

        ``dropped=True`` records that the moment is too far in the past
        for a useful cue (the decay worker is expected to flip the row to
        past_event soon anyway). The metadata key is the same either way
        — we just want subsequent ticks to skip the row.
        """
        try:
            self._memory_store.update(
                memory.id,
                metadata={
                    "followup_fired_at": when.isoformat(),
                    "followup_dropped": bool(dropped),
                },
                metadata_merge=True,
            )
        except Exception:
            log.debug(
                "follow_up: mark_fired update failed for id=%s",
                memory.id,
                exc_info=True,
            )


__all__ = [
    "FollowUpWorker",
    "FOLLOW_UP_JOURNAL_KEY",
    "load_follow_up_cues",
    "append_follow_up_cue",
]
