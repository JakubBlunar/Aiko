"""Live-mode-only proactive nudger.

When Live mode is active and the microphone has been silent for
``silence_seconds``, the user can be assumed to be listening but not yet
ready to talk. This module fires a SHORT one-shot turn (no streaming) that
is routed straight to TTS to keep the conversation alive.

Phase 4c addition: if the :class:`PreparedNudgeStore` has a fresh entry
(woven during a previous speaking window by :class:`NarrativeWeaver`),
we speak that directly and skip the LLM round-trip entirely. This makes
the silence-break feel instant *and* lets the line draw on Aiko's
inner-life surfaces (callbacks, open questions, promises, agenda items)
instead of cold-rolling something from history.

Differences from the legacy ProactiveDirector:
- No periodic heartbeat thread. Driven by an explicit
  :func:`notify_silence` call from LiveWorker.
- No persona evolution, no autonomy goal switching, no separate "brain" model.
- Single LLM call against the same chat model with a tight token cap.
- Hard cooldown enforced in-memory, plus "don't speak while a turn is in
  flight or TTS is playing" guards via callables provided by the owner.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from app.core.chat_database import ChatDatabase
from app.core.session_text_utils import (
    prepare_tts_text,
    sanitize_assistant_text,
)
from app.core.services.response_text_service import (
    parse_reaction_at_start,
    strip_all_meta_tags,
)
from app.core.prompt_assembler import PromptAssembler
from app.llm.ollama_client import OllamaClient


log = logging.getLogger("app.proactive")


SpeakCallback = Callable[[str, str], None]
"""Signature: ``(prepared_text, reaction)``."""

NotifyMessageCallback = Callable[[str, str], None]
"""Signature: ``(speaker, text)`` -- routes the proactive line into the chat
transcript so the React UI / desktop log show what Aiko said unprompted."""

BoolPredicate = Callable[[], bool]


_PROACTIVE_HINT = (
    "[Aiko speaks first, briefly, because Jacob has been quiet for a moment. "
    "Pick up a thread from the recent conversation, or ask a casual short "
    "question to keep the chat going. ONE OR TWO SENTENCES MAXIMUM. Don't "
    "greet, don't restart the conversation. Continue naturally.]"
)


class ProactiveDirector:
    def __init__(
        self,
        ollama: OllamaClient,
        db: ChatDatabase,
        prompt_assembler: PromptAssembler,
        *,
        model: str,
        speak: SpeakCallback,
        is_busy: BoolPredicate,
        is_live_mode: BoolPredicate,
        cooldown_seconds: float = 120.0,
        max_tokens: int = 80,
        timeout_seconds: float = 30.0,
        context_window: int = 8192,
        notify_message: NotifyMessageCallback | None = None,
        prepared_nudge_store: object | None = None,
        user_id: str = "default",
    ) -> None:
        self._ollama = ollama
        self._db = db
        self._prompt = prompt_assembler
        self._model = model
        self._speak = speak
        self._is_busy = is_busy
        self._is_live = is_live_mode
        self._cooldown = float(cooldown_seconds)
        self._max_tokens = int(max_tokens)
        self._timeout = float(timeout_seconds)
        self._context_window = int(context_window)
        self._notify_message = notify_message
        self._prepared_store = prepared_nudge_store
        self._user_id = user_id or "default"

        self._lock = threading.Lock()
        self._last_run_monotonic = 0.0
        self._inflight = False
        self._prepared_consumed = 0
        self._llm_path_used = 0

    # ── public ────────────────────────────────────────────────────────

    def notify_silence(self, session_key: str) -> None:
        """Possibly speak a proactive line. No-op if guards reject."""
        if not session_key:
            return
        if not self._is_live():
            return
        if self._is_busy():
            log.debug("proactive skip: chat in progress")
            return
        with self._lock:
            since = time.monotonic() - self._last_run_monotonic
            if since < self._cooldown:
                log.debug("proactive skip: cooldown %.1fs/%.1fs", since, self._cooldown)
                return
            if self._inflight:
                log.debug("proactive skip: already running")
                return
            self._inflight = True
        threading.Thread(
            target=self._run_safe,
            args=(session_key,),
            daemon=True,
            name="proactive-director",
        ).start()

    def update_runtime(
        self,
        *,
        model: str | None = None,
        cooldown_seconds: float | None = None,
        context_window: int | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if cooldown_seconds is not None:
            self._cooldown = float(cooldown_seconds)
        if context_window is not None:
            self._context_window = int(context_window)

    # ── internals ─────────────────────────────────────────────────────

    def _run_safe(self, session_key: str) -> None:
        try:
            self._run(session_key)
        except Exception as exc:
            log.warning("proactive run failed: %s", exc)
        finally:
            with self._lock:
                self._inflight = False
                self._last_run_monotonic = time.monotonic()

    def _run(self, session_key: str) -> None:
        if self._db.get_message_count(session_key) <= 0:
            log.debug("proactive skip: no history yet")
            return

        # Phase 4c: prefer a prepared nudge if one is fresh.
        prepared = self._consume_prepared_nudge()
        if prepared is not None and self._speak_prepared(session_key, prepared):
            return

        messages = self._prompt.build(
            session_key,
            _PROACTIVE_HINT,
            context_window=self._context_window,
            response_budget=self._max_tokens,
        )

        t0 = time.monotonic()
        try:
            content, usage = self._ollama.chat_json(
                messages,
                model=self._model,
                timeout_seconds=self._timeout,
                options={"temperature": 0.7, "num_predict": self._max_tokens},
                format_json=False,
            )
        except Exception as exc:
            log.info("proactive call failed: %s", exc)
            return

        # Re-check guards before speaking (the user may have started typing).
        if self._is_busy() or not self._is_live():
            log.debug("proactive: discarding (state changed mid-call)")
            return

        mood, body = parse_reaction_at_start(content or "")
        body = strip_all_meta_tags(body)
        cleaned = sanitize_assistant_text(body)
        if not cleaned:
            log.debug("proactive: empty output")
            return

        # Persist as an assistant turn so the model remembers what it said.
        self._db.add_message(
            session_id=session_key,
            role="assistant",
            content=cleaned,
            token_count=usage.completion_tokens,
        )
        # Surface the line in the chat transcript using a distinguishable
        # speaker so the React UI can render it differently if it wants.
        if self._notify_message is not None:
            try:
                self._notify_message("Assistant (proactive)", cleaned)
            except Exception:
                log.debug("notify_message raised", exc_info=True)
        prepared = prepare_tts_text(cleaned)
        if prepared:
            self._speak(prepared, mood or "calm")
        self._llm_path_used += 1
        log.info(
            "proactive spoke (%d chars, %d/%d tokens, %.0f ms)",
            len(cleaned),
            usage.prompt_tokens,
            usage.completion_tokens,
            (time.monotonic() - t0) * 1000.0,
        )

    # ── prepared-nudge fast path (Phase 4c) ──────────────────────────────

    def _consume_prepared_nudge(self):
        store = self._prepared_store
        if store is None:
            return None
        try:
            return store.consume(self._user_id)
        except Exception:
            log.debug("prepared nudge consume raised", exc_info=True)
            return None

    def _speak_prepared(self, session_key: str, nudge: object) -> bool:
        text = getattr(nudge, "text", "")
        if not text:
            return False
        # Re-run guards before speaking (state may have changed).
        if self._is_busy() or not self._is_live():
            log.debug("proactive prepared: discarding (state changed)")
            return False
        cleaned = sanitize_assistant_text(text)
        if not cleaned:
            return False
        try:
            self._db.add_message(
                session_id=session_key,
                role="assistant",
                content=cleaned,
                token_count=0,
            )
        except Exception:
            log.debug("prepared nudge persist failed", exc_info=True)
        if self._notify_message is not None:
            try:
                self._notify_message("Assistant (proactive)", cleaned)
            except Exception:
                log.debug("notify_message raised", exc_info=True)
        prepared_text = prepare_tts_text(cleaned)
        if prepared_text:
            try:
                self._speak(prepared_text, "calm")
            except Exception:
                log.debug("speak callback raised", exc_info=True)
                return False
        self._prepared_consumed += 1
        log.info(
            "proactive spoke prepared nudge (kind=%s, %d chars)",
            getattr(nudge, "source_kind", "?"),
            len(cleaned),
        )
        return True

    def stats(self) -> dict[str, int]:
        return {
            "prepared_consumed": int(self._prepared_consumed),
            "llm_path_used": int(self._llm_path_used),
        }
