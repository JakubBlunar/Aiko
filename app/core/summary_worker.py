"""Idle-triggered conversation summarizer + long-term memory extractor.

Rather than running on every turn (which thrashes the GPU), the worker keeps
a rolling deadline that ``notify_turn_done`` pushes forward. Only when the
chat has been quiet for ``idle_seconds`` AND there are at least
``min_unsummarized_messages`` new messages does it actually run.

After a successful summary, it kicks the optional :class:`MemoryExtractor`
to mine durable facts from the same window. Both run on the same chat model
so there's no extra GPU swap.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, TYPE_CHECKING

from app.core.chat_database import ChatDatabase
from app.llm.ollama_client import OllamaClient
from app.llm.token_utils import estimate_tokens

if TYPE_CHECKING:
    from app.core.memory_extractor import MemoryExtractor


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
        idle_seconds: float = 15.0,
        min_unsummarized_messages: int = 6,
        target_tokens: int = 600,
        timeout_seconds: float = 90.0,
        memory_extractor: "MemoryExtractor | None" = None,
    ) -> None:
        self._db = db
        self._ollama = ollama
        self._model = model
        self._is_busy = is_busy
        self._idle = float(idle_seconds)
        self._min_msgs = int(min_unsummarized_messages)
        self._target_tokens = max(120, int(target_tokens))
        self._timeout = float(timeout_seconds)
        self._memory_extractor = memory_extractor

        self._cond = threading.Condition()
        self._deadline_ms: dict[str, float] = {}
        self._shutdown = threading.Event()
        self._thread: threading.Thread | None = None
        self._compactions_total = 0
        self._last_compaction_at: float | None = None  # monotonic seconds

    def set_memory_extractor(self, extractor: "MemoryExtractor | None") -> None:
        self._memory_extractor = extractor

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

    def notify_compaction_soon(self, session_key: str) -> None:
        """Push the deadline to *now* so the next loop tick processes it.

        Used by :class:`TurnRunner` when the just-finished turn left the
        prompt above ``max_prompt_tokens_pct`` of the context window — we
        don't want to wait the full idle window before compacting.
        """
        with self._cond:
            self._deadline_ms[session_key] = time.monotonic() * 1000.0
            self._cond.notify_all()

    def compactions_total(self) -> int:
        return self._compactions_total

    def last_compaction_age_seconds(self) -> float | None:
        if self._last_compaction_at is None:
            return None
        return max(0.0, time.monotonic() - self._last_compaction_at)

    # ── synchronous entry point (overflow squish) ────────────────────────

    def compact_now(self, session_key: str) -> bool:
        """Force an immediate, synchronous summarisation pass.

        Lowers the ``min_unsummarized_messages`` bar to 2 so we always make
        progress when called. Returns ``True`` if a summary was actually
        written. Safe to call from the turn thread; the model call happens
        inline (so this blocks for `timeout_seconds`).
        """
        try:
            wrote = self._maybe_summarize(session_key, min_msgs_override=2)
        except Exception as exc:
            log.warning("compact_now failed: %s", exc)
            return False
        if wrote:
            self._compactions_total += 1
            self._last_compaction_at = time.monotonic()
        return bool(wrote)

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

    def _maybe_summarize(
        self, session_key: str, *, min_msgs_override: int | None = None,
    ) -> bool:
        threshold = self._min_msgs if min_msgs_override is None else int(min_msgs_override)
        latest = self._db.get_latest_summary(session_key)
        already_summarized = int(latest.messages_summarized) if latest else 0
        total = self._db.get_message_count(session_key)
        unsummarized = total - already_summarized
        if unsummarized < threshold:
            log.debug(
                "summary skipped (only %d new msgs, need %d) for %s",
                unsummarized, threshold, session_key[:8],
            )
            return False

        # Pull the unsummarized window plus a small overlap from before so the
        # model can see continuity.
        offset = max(0, already_summarized - 4)
        rows = self._db.get_messages(session_key, offset=offset)
        if not rows:
            return False

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
                options={"temperature": 0.3, "num_predict": self._target_tokens},
                format_json=False,
            )
        except Exception as exc:
            log.warning("summary call failed: %s", exc)
            return False

        # The model may have been asked for plain text (we used chat_json for
        # the keep-alive / non-streaming behaviour but the system prompt asks
        # for bullets, not JSON). Strip code fences just in case.
        text = content.strip().strip("`")
        if not text:
            log.info("summary returned empty for %s", session_key[:8])
            return False

        self._db.save_summary(
            session_id=session_key,
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

        if self._memory_extractor is not None:
            try:
                inserted = self._memory_extractor.extract_for_session(session_key)
                if inserted:
                    log.info(
                        "memory extractor added %d new memories for %s",
                        inserted, session_key[:8],
                    )
            except Exception as exc:
                log.warning("memory extractor failed: %s", exc)
        return True
