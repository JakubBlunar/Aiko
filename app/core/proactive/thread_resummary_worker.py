"""Fresh-eyes thread re-summary idle worker (K21 personality backlog).

Compaction (the :class:`SummaryWorker`) compresses *old* history into a
rolling bullet summary when context overflows. It does NOT periodically
step back and re-synthesise "what is this ongoing thread actually about
now?" for Aiko's inner voice. After enough new turns (or once a day,
whichever comes first) this worker drafts a short, present-tense
"where this conversation stands now" note — three sentences plus a
<=6-word title — and upserts it onto the session.

Two consumers:

1. **Prompt.** The note surfaces as its own small T2 block right after
   the rolling summary (``PromptAssembler``), giving Aiko a clean,
   recently-refreshed read of the thread without paying a per-turn
   token cost to regenerate it.
2. **Sidebar.** ``ChatDatabase.list_sessions`` prefers the note's
   ``title`` as the conversation's human-readable label.

Single LLM call per successful tick on the local worker model, bounded
by a :class:`FactCheckRateLimiter`. Opt-out via
``agent.thread_resummary_enabled``. Triggers (in ``is_ready``):

* the session has at least ``thread_resummary_min_messages`` messages, AND
* one of: no note yet · ``thread_resummary_message_interval`` new
  messages since the note's watermark · the note is older than
  ``thread_resummary_max_age_hours``.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Callable

from app.core.proactive.idle_worker import default_is_ready

if TYPE_CHECKING:
    from app.core.infra.chat_database import ChatDatabase
    from app.core.infra.settings import AgentSettings, MemorySettings
    from app.core.memory.fact_check_rate_limiter import FactCheckRateLimiter
    from app.llm.chat_client import ChatClient


log = logging.getLogger("app.thread_resummary_worker")


_SYSTEM_PROMPT = (
    "You are an inner-life worker for an AI companion named "
    "{assistant_name}. Read the recent conversation with {user_name} and "
    "write {assistant_name}'s own fresh read of where this thread stands "
    "RIGHT NOW. Present tense, first person ({assistant_name}'s voice), "
    "warm and concrete. Capture the live topic, the emotional register, "
    "and anything left open or promised. Do not recap turn-by-turn. "
    "Reply with ONE JSON object on a single line and nothing else. "
    "Schema: {{\"title\": \"<=6 word label\", \"note\": \"<=3 short "
    "sentences\"}}."
)


_USER_TEMPLATE = (
    "EARLIER (rolling summary):\n{summary}\n\n"
    "PREVIOUS NOTE (your last read, may be stale):\n{prev}\n\n"
    "RECENT MESSAGES (oldest first):\n{transcript}\n\n"
    "Write the fresh read now."
)


_MAX_TOKENS = 320
_MAX_TITLE_CHARS = 60
_MAX_NOTE_CHARS = 500
_MAX_SUMMARY_CHARS = 900
_MAX_TRANSCRIPT_MSGS = 40
_MAX_MSG_CHARS = 300

_JSON_OBJECT_RE = re.compile(r"\{.*\}", flags=re.DOTALL)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _trim(text: str | None, *, max_chars: int) -> str:
    if not text:
        return ""
    flat = " ".join(str(text).split())
    if len(flat) <= max_chars:
        return flat
    return flat[: max_chars - 1].rstrip(",;: ") + "\u2026"


def parse_thread_note(raw: str) -> tuple[str, str]:
    """Parse the ``{"title": ..., "note": ...}`` JSON object.

    Tolerant: pulls the first ``{...}`` span out of the raw text. Returns
    ``(title, note)``, both trimmed; either may be empty on a partial
    parse. Returns ``("", "")`` on total parse failure.
    """
    text = (raw or "").strip()
    match = _JSON_OBJECT_RE.search(text)
    if match is None:
        return "", ""
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError:
        return "", ""
    if not isinstance(parsed, dict):
        return "", ""
    title = parsed.get("title")
    note = parsed.get("note")
    title_s = _trim(title, max_chars=_MAX_TITLE_CHARS) if isinstance(title, str) else ""
    note_s = _trim(note, max_chars=_MAX_NOTE_CHARS) if isinstance(note, str) else ""
    return title_s, note_s


class ThreadResummaryWorker:
    """IdleWorker that drafts + upserts a fresh-eyes note for the active session."""

    name = "thread_resummary"

    def __init__(
        self,
        *,
        chat_db: "ChatDatabase",
        ollama: "ChatClient",
        chat_model: str,
        cancel_event: threading.Event,
        agent_settings: "AgentSettings",
        memory_settings: "MemorySettings",
        rate_limiter: "FactCheckRateLimiter",
        session_key_provider: Callable[[], str],
        user_display_name_provider: Callable[[], str] | None = None,
        assistant_display_name_provider: Callable[[], str] | None = None,
        notify_thread_note: Callable[[dict[str, Any]], None] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._chat_db = chat_db
        self._ollama = ollama
        self._chat_model = chat_model
        self._cancel_event = cancel_event
        self._agent_settings = agent_settings
        self._memory_settings = memory_settings
        self._rate_limiter = rate_limiter
        self._session_key_provider = session_key_provider
        self._user_display_name_provider = user_display_name_provider
        self._assistant_display_name_provider = assistant_display_name_provider
        self._notify_thread_note = notify_thread_note
        self._clock = clock or _utcnow

    # ── IdleWorker protocol ───────────────────────────────────────────

    @property
    def interval_seconds(self) -> float:
        return float(
            getattr(
                self._memory_settings, "thread_resummary_interval_seconds", 3600,
            )
        )

    def _enabled(self) -> bool:
        return bool(getattr(self._agent_settings, "thread_resummary_enabled", True))

    def is_ready(
        self,
        *,
        now: datetime,
        last_run_at: datetime | None,
    ) -> bool:
        if not self._enabled():
            return False
        if not default_is_ready(
            self.interval_seconds, now=now, last_run_at=last_run_at,
        ):
            return False
        snapshot = self._rate_limiter.snapshot(now)
        if snapshot["hour_used"] >= snapshot["hour_cap"]:
            return False
        if snapshot["day_used"] >= snapshot["day_cap"]:
            return False
        try:
            session_key = self._session_key()
            msg_count = self._chat_db.get_message_count(session_key)
        except Exception:
            log.debug("thread_resummary readiness probe failed", exc_info=True)
            return False
        return self._should_redraft(session_key, msg_count, now)

    def run(self) -> dict[str, Any]:
        if not self._enabled():
            return {"skipped": True, "reason": "disabled"}
        if self._cancel_event.is_set():
            return {"skipped": True, "reason": "cancelled_before_start"}

        now = self._clock()
        session_key = self._session_key()
        try:
            msg_count = self._chat_db.get_message_count(session_key)
        except Exception:
            log.warning("thread_resummary message count failed", exc_info=True)
            return {"errored": True, "reason": "count"}

        min_messages = max(
            1, int(getattr(self._agent_settings, "thread_resummary_min_messages", 12)),
        )
        if msg_count < min_messages:
            return {"skipped": True, "reason": "too_short", "messages": msg_count}
        if not self._should_redraft(session_key, msg_count, now):
            return {"skipped": True, "reason": "not_due", "messages": msg_count}

        if not self._rate_limiter.allow(now):
            return {"skipped": True, "reason": "rate_limited"}

        try:
            transcript = self._build_transcript(session_key)
        except Exception:
            log.warning("thread_resummary transcript build failed", exc_info=True)
            return {"errored": True, "reason": "transcript"}
        if not transcript:
            return {"skipped": True, "reason": "no_transcript"}

        summary_text = self._summary_block(session_key)
        prev_note = self._prev_note_block(session_key)

        t0 = time.monotonic()
        try:
            title, note = self._draft(
                transcript=transcript,
                summary_text=summary_text,
                prev_note=prev_note,
            )
        except Exception:
            log.warning("thread_resummary draft call raised", exc_info=True)
            return {"errored": True, "reason": "draft_call"}
        if self._cancel_event.is_set():
            return {"cancelled": True}
        llm_ms = (time.monotonic() - t0) * 1000.0

        if not note:
            log.info("thread_resummary: empty note (llm_ms=%.0f)", llm_ms)
            return {"wrote": False, "reason": "empty_note", "llm_ms": int(llm_ms)}

        if not title:
            # Fall back to the first few words of the note so the sidebar
            # still gets a label.
            title = _trim(" ".join(note.split()[:6]), max_chars=_MAX_TITLE_CHARS)

        try:
            self._chat_db.save_thread_note(session_key, title, note, msg_count)
        except Exception:
            log.warning("thread_resummary save failed", exc_info=True)
            return {"errored": True, "reason": "save"}

        if self._notify_thread_note is not None:
            try:
                self._notify_thread_note({
                    "session_id": session_key,
                    "title": title,
                    "note": note,
                    "messages_at": msg_count,
                })
            except Exception:
                log.debug("thread_resummary notify failed", exc_info=True)

        log.info(
            "thread_resummary wrote: session=%s messages=%d title=%r llm_ms=%.0f",
            session_key, msg_count, title, llm_ms,
        )
        return {
            "wrote": True,
            "session_id": session_key,
            "title": title,
            "messages_at": msg_count,
            "llm_ms": int(llm_ms),
        }

    # ── trigger logic ─────────────────────────────────────────────────

    def _should_redraft(
        self, session_key: str, msg_count: int, now: datetime,
    ) -> bool:
        min_messages = max(
            1, int(getattr(self._agent_settings, "thread_resummary_min_messages", 12)),
        )
        if msg_count < min_messages:
            return False
        try:
            note = self._chat_db.get_thread_note(session_key)
        except Exception:
            log.debug("get_thread_note failed", exc_info=True)
            return False
        if note is None or not (note.note or "").strip():
            return True
        interval = max(
            1,
            int(getattr(self._agent_settings, "thread_resummary_message_interval", 50)),
        )
        if msg_count - int(note.messages_at or 0) >= interval:
            return True
        max_age_h = float(
            getattr(self._agent_settings, "thread_resummary_max_age_hours", 24.0),
        )
        if max_age_h > 0:
            age_h = self._note_age_hours(note.updated_at, now)
            if age_h is not None and age_h >= max_age_h:
                return True
        return False

    @staticmethod
    def _note_age_hours(updated_at: str | None, now: datetime) -> float | None:
        if not updated_at:
            return None
        try:
            ts = datetime.fromisoformat(updated_at)
        except ValueError:
            return None
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return (now - ts).total_seconds() / 3600.0

    # ── context pack ──────────────────────────────────────────────────

    def _session_key(self) -> str:
        return (self._session_key_provider() or "").strip()

    def _build_transcript(self, session_key: str) -> str:
        rows = self._chat_db.get_messages(
            session_key, limit=_MAX_TRANSCRIPT_MSGS,
        )
        user_name = self._resolve_user_name()
        assistant_name = self._resolve_assistant_name()
        lines: list[str] = []
        for row in rows:
            role = getattr(row, "role", "") or ""
            content = (getattr(row, "content", "") or "").strip()
            if not content:
                continue
            who = (
                assistant_name if role == "assistant"
                else user_name if role == "user"
                else role or "system"
            )
            lines.append(f"{who}: {_trim(content, max_chars=_MAX_MSG_CHARS)}")
        return "\n".join(lines)

    def _summary_block(self, session_key: str) -> str:
        try:
            row = self._chat_db.get_latest_summary(session_key)
        except Exception:
            return ""
        if row is None or not (row.summary or "").strip():
            return ""
        return _trim(row.summary, max_chars=_MAX_SUMMARY_CHARS)

    def _prev_note_block(self, session_key: str) -> str:
        try:
            row = self._chat_db.get_thread_note(session_key)
        except Exception:
            return ""
        if row is None or not (row.note or "").strip():
            return ""
        return _trim(row.note, max_chars=_MAX_NOTE_CHARS)

    # ── LLM ───────────────────────────────────────────────────────────

    def _draft(
        self,
        *,
        transcript: str,
        summary_text: str,
        prev_note: str,
    ) -> tuple[str, str]:
        system = _SYSTEM_PROMPT.format(
            assistant_name=self._resolve_assistant_name(),
            user_name=self._resolve_user_name(),
        )
        user_payload = _USER_TEMPLATE.format(
            summary=summary_text or "(none yet)",
            prev=prev_note or "(none yet)",
            transcript=transcript,
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user_payload},
        ]
        raw = self._ollama.chat(
            messages,
            options={"num_predict": _MAX_TOKENS, "temperature": 0.5},
            model=self._chat_model,
            surface="thread_resummary_worker",
        )
        return parse_thread_note(raw or "")

    # ── name resolution ───────────────────────────────────────────────

    def _resolve_user_name(self) -> str:
        if self._user_display_name_provider is None:
            return "the user"
        try:
            return (self._user_display_name_provider() or "the user") or "the user"
        except Exception:
            return "the user"

    def _resolve_assistant_name(self) -> str:
        if self._assistant_display_name_provider is None:
            return "the assistant"
        try:
            return (
                self._assistant_display_name_provider() or "the assistant"
            ) or "the assistant"
        except Exception:
            return "the assistant"


__all__ = [
    "ThreadResummaryWorker",
    "parse_thread_note",
]
