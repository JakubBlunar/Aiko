"""Idle-triggered conversation summarizer.

Rather than running on every turn (which thrashes the GPU), the worker keeps
a rolling deadline that ``notify_turn_done`` pushes forward. Only when the
chat has been quiet for ``idle_seconds`` AND there are at least
``min_unsummarized_messages`` new messages does it actually run.

Uses the same chat model — no separate judge — so it never causes model
swapping. Stores results in the existing ``session_summaries`` table.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from app.core.chat_database import ChatDatabase
from app.llm.ollama_client import OllamaClient
from app.llm.token_utils import estimate_tokens


log = logging.getLogger("app.summary_worker")


_SYSTEM_PROMPT = (
    "You compress chat transcripts. Produce a concise (max 6 short bullet "
    "points) summary of the conversation, preserving names, key facts about "
    "the user, topics discussed, and the emotional arc. Write in third "
    "person. No introductions, no markdown, no headers. Just the bullets."
)


class SummaryWorker:
    def __init__(
        self,
        db: ChatDatabase,
        ollama: OllamaClient,
        *,
        model: str,
        is_busy: Callable[[], bool],
        idle_seconds: float = 30.0,
        min_unsummarized_messages: int = 10,
        timeout_seconds: float = 90.0,
    ) -> None:
        self._db = db
        self._ollama = ollama
        self._model = model
        self._is_busy = is_busy
        self._idle = float(idle_seconds)
        self._min_msgs = int(min_unsummarized_messages)
        self._timeout = float(timeout_seconds)

        self._cond = threading.Condition()
        self._deadline_ms: dict[str, float] = {}
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None

    # ── lifecycle ────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._shutdown.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="summary-worker",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._shutdown.set()
        with self._cond:
            self._cond.notify_all()

    def notify_turn_done(self, session_key: str) -> None:
        """Reset the idle deadline for ``session_key`` to now + ``idle_seconds``."""
        deadline = time.monotonic() * 1000.0 + self._idle * 1000.0
        with self._cond:
            self._deadline_ms[session_key] = deadline
            self._cond.notify_all()

    # ── loop ─────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._shutdown.is_set():
            wait_seconds = self._idle
            target_session: str | None = None
            with self._cond:
                if not self._deadline_ms:
                    self._cond.wait(timeout=self._idle * 2.0)
                    continue
                now_ms = time.monotonic() * 1000.0
                soonest = min(self._deadline_ms.items(), key=lambda kv: kv[1])
                ready_key, ready_ms = soonest
                if ready_ms <= now_ms:
                    target_session = ready_key
                    self._deadline_ms.pop(ready_key, None)
                else:
                    wait_seconds = max(0.5, (ready_ms - now_ms) / 1000.0)
                    self._cond.wait(timeout=wait_seconds)
                    continue
            if target_session is None:
                continue
            try:
                if self._is_busy():
                    log.debug("summary skipped: chat in progress")
                    self.notify_turn_done(target_session)
                    continue
                self._maybe_summarize(target_session)
            except Exception as exc:
                log.warning("summary worker tick failed: %s", exc)

    # ── work ─────────────────────────────────────────────────────────────

    def _maybe_summarize(self, session_key: str) -> None:
        latest = self._db.get_latest_summary(session_key)
        already_summarized = int(latest.messages_summarized) if latest else 0
        total = self._db.get_message_count(session_key)
        unsummarized = total - already_summarized
        if unsummarized < self._min_msgs:
            log.debug(
                "summary skipped (only %d new msgs, need %d) for %s",
                unsummarized, self._min_msgs, session_key[:8],
            )
            return

        # Pull the unsummarized window plus a small overlap from before so the
        # model can see continuity.
        offset = max(0, already_summarized - 4)
        rows = self._db.get_messages(session_key, offset=offset)
        if not rows:
            return

        transcript_lines: list[str] = []
        for row in rows:
            speaker = "User" if row.role == "user" else "Aiko"
            content = (row.content or "").strip()
            if content:
                transcript_lines.append(f"{speaker}: {content}")
        transcript = "\n".join(transcript_lines)

        prior = (latest.summary if latest else "").strip()
        user_prompt_parts: list[str] = []
        if prior:
            user_prompt_parts.append(f"Existing summary:\n{prior}")
        user_prompt_parts.append(f"New transcript:\n{transcript}")
        user_prompt_parts.append("Write the updated combined summary.")

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": "\n\n".join(user_prompt_parts)},
        ]

        t0 = time.monotonic()
        try:
            content, usage = self._ollama.chat_json(
                messages,
                model=self._model,
                timeout_seconds=self._timeout,
                options={"temperature": 0.3, "num_predict": 512},
                format_json=False,
            )
        except Exception as exc:
            log.warning("summary call failed: %s", exc)
            return

        # The model may have been asked for plain text (we used chat_json for
        # the keep-alive / non-streaming behaviour but the system prompt asks
        # for bullets, not JSON). Strip code fences just in case.
        text = content.strip().strip("`")
        if not text:
            log.info("summary returned empty for %s", session_key[:8])
            return

        self._db.save_summary(
            session_key=session_key,
            summary=text,
            summary_tokens=estimate_tokens(text),
            messages_summarized=total,
        )
        log.info(
            "summary saved (%d msgs, %d tokens, %.0f ms; usage %d/%d)",
            total,
            estimate_tokens(text),
            (time.monotonic() - t0) * 1000.0,
            usage.prompt_tokens,
            usage.completion_tokens,
        )
