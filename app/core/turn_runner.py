"""The single-turn orchestrator.

Replaces the old ``_AgentWrapper`` + ``AgentController`` + ``TurnTriage`` +
``ReasonActReflect`` stack. One ``run()`` call:

  1. Build prompt via :class:`PromptAssembler`.
  2. Stream from Ollama (cancellable via ``stop_requested``).
  3. Parse ``[[reaction:X]]`` once at the start of the stream.
  4. Strip meta tags for display; emit incremental text via ``on_token``.
  5. Chunk text into sentences for TTS via ``on_tts_chunk``.
  6. Persist the user + assistant messages.
  7. Kick off background workers (summary, learner profile).

No tool-calling in v1 -- that hooks in later through
``OllamaClient.chat_with_tools`` without touching the rest of the pipeline.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

from app.core.chat_database import ChatDatabase
from app.core.learner_profile import LearnerProfile
from app.core.prompt_assembler import PromptAssembler
from app.core.services.response_text_service import (
    parse_reaction_at_start,
    strip_all_meta_tags,
)
from app.core.session_text_utils import (
    drain_tts_stream_chunks,
    prepare_tts_text,
    sanitize_assistant_text,
    sanitize_user_text,
)
from app.core.summary_worker import SummaryWorker
from app.llm.ollama_client import OllamaClient, OllamaUsage
from app.llm.token_utils import estimate_tokens


log = logging.getLogger("app.turn_runner")


TokenCallback = Callable[[str], None]
TtsChunkCallback = Callable[[str, str], None]
"""Signature: ``(prepared_text, reaction)``."""

StopPredicate = Callable[[], bool]


@dataclass(slots=True)
class TurnResult:
    text: str
    reaction: str
    usage: OllamaUsage = field(default_factory=OllamaUsage)
    aborted: bool = False
    duration_ms: float = 0.0


class TurnRunner:
    def __init__(
        self,
        ollama: OllamaClient,
        db: ChatDatabase,
        prompt_assembler: PromptAssembler,
        *,
        model: str,
        context_window: int,
        max_tokens: int,
        temperature: float,
        learner_profile: LearnerProfile | None = None,
        summary_worker: SummaryWorker | None = None,
    ) -> None:
        self._ollama = ollama
        self._db = db
        self._prompt = prompt_assembler
        self._model = model
        self._context_window = max(2048, int(context_window))
        self._max_tokens = max(64, int(max_tokens))
        self._temperature = float(temperature)
        self._profile = learner_profile
        self._summary = summary_worker
        self._stop = threading.Event()

    # ── public ────────────────────────────────────────────────────────

    @property
    def model(self) -> str:
        return self._model

    def update_runtime(
        self,
        *,
        model: str | None = None,
        context_window: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if context_window is not None:
            self._context_window = max(2048, int(context_window))
        if max_tokens is not None:
            self._max_tokens = max(64, int(max_tokens))
        if temperature is not None:
            self._temperature = float(temperature)

    def request_stop(self) -> None:
        self._stop.set()

    def run(
        self,
        session_key: str,
        user_text: str,
        *,
        on_token: TokenCallback | None = None,
        on_tts_chunk: TtsChunkCallback | None = None,
        stop_requested: StopPredicate | None = None,
        save_user_message: bool = True,
    ) -> TurnResult:
        self._stop.clear()
        cleaned_user = sanitize_user_text(user_text)
        if not cleaned_user:
            return TurnResult(text="", reaction="neutral")

        if save_user_message:
            self._db.add_message(
                session_key=session_key,
                role="user",
                content=cleaned_user,
                token_count=estimate_tokens(cleaned_user),
            )

        messages = self._prompt.build(
            session_key,
            cleaned_user,
            context_window=self._context_window,
            response_budget=self._max_tokens,
        )

        log.info(
            "turn start: model=%s session=%s ctx=%d max=%d msgs=%d",
            self._model, session_key[:8], self._context_window, self._max_tokens, len(messages),
        )

        # Streaming bookkeeping.
        accumulator: list[str] = []
        mood: str | None = None
        ui_sent_chars = 0       # chars of meta-stripped body already sent to UI
        tts_appended_chars = 0  # chars of meta-stripped body already routed to TTS
        tts_buffer = ""         # rolling buffer of body chars not yet spoken
        aborted = False
        t0 = time.monotonic()

        try:
            stream = self._ollama.chat_stream(
                messages,
                options={
                    "temperature": self._temperature,
                    "num_predict": self._max_tokens,
                    "num_ctx": self._context_window,
                },
                model=self._model,
                stop_event=self._stop,
            )
            for delta in stream:
                if self._is_stop_requested(stop_requested):
                    aborted = True
                    self._stop.set()
                    break
                accumulator.append(delta)
                full = "".join(accumulator)

                # Strip the leading [[reaction:X]] tag once (and only once),
                # then operate on the body text afterwards.
                if mood is None:
                    parsed_mood, body_after_react = parse_reaction_at_start(full)
                    if parsed_mood is not None:
                        mood = parsed_mood
                    body = body_after_react if parsed_mood is not None else full
                else:
                    _m, body = parse_reaction_at_start(full)
                    if _m is None:
                        body = full

                visible = strip_all_meta_tags(body)

                if on_token is not None and len(visible) > ui_sent_chars:
                    on_token(visible[ui_sent_chars:])
                    ui_sent_chars = len(visible)

                if on_tts_chunk is not None and mood is not None:
                    new_tts_chars = visible[tts_appended_chars:]
                    if new_tts_chars:
                        tts_buffer += new_tts_chars
                        tts_appended_chars = len(visible)
                        chunks, tts_buffer = drain_tts_stream_chunks(
                            tts_buffer, flush=False,
                        )
                        for chunk in chunks:
                            prepared = prepare_tts_text(chunk)
                            if prepared:
                                on_tts_chunk(prepared, mood or "neutral")

        except Exception as exc:
            log.warning("stream failed: %s", exc)
            raise

        full_raw = "".join(accumulator)
        if mood is None:
            parsed_mood, full_raw = parse_reaction_at_start(full_raw)
            if parsed_mood is not None:
                mood = parsed_mood
        body_text = strip_all_meta_tags(full_raw)
        cleaned = sanitize_assistant_text(body_text)

        # Flush any trailing TTS buffer (final sentence without terminator).
        if on_tts_chunk is not None and not aborted and tts_buffer.strip():
            chunks, _ = drain_tts_stream_chunks(tts_buffer, flush=True)
            for chunk in chunks:
                prepared = prepare_tts_text(chunk)
                if prepared:
                    on_tts_chunk(prepared, mood or "neutral")

        usage = self._ollama.last_usage
        duration_ms = (time.monotonic() - t0) * 1000.0

        if cleaned and not aborted:
            self._db.add_message(
                session_key=session_key,
                role="assistant",
                content=cleaned,
                token_count=usage.completion_tokens or estimate_tokens(cleaned),
            )
            self._post_turn(session_key)

        log.info(
            "turn done: %d chars, mood=%s, %d/%d tokens, %.0f ms (eval %.0f ms)%s",
            len(cleaned),
            mood or "neutral",
            usage.prompt_tokens,
            usage.completion_tokens,
            duration_ms,
            usage.eval_duration_ms,
            " [aborted]" if aborted else "",
        )

        return TurnResult(
            text=cleaned,
            reaction=mood or "neutral",
            usage=usage,
            aborted=aborted,
            duration_ms=duration_ms,
        )

    # ── helpers ───────────────────────────────────────────────────────

    def _is_stop_requested(self, predicate: StopPredicate | None) -> bool:
        if predicate is None:
            return False
        try:
            return bool(predicate())
        except Exception:
            return False

    # ── post-turn jobs ────────────────────────────────────────────────

    def _post_turn(self, session_key: str) -> None:
        if self._summary is not None:
            try:
                self._summary.notify_turn_done(session_key)
            except Exception as exc:
                log.debug("summary notify failed: %s", exc)
        if self._profile is None:
            return
        try:
            count = self._db.get_message_count(session_key)
        except Exception:
            return
        # ``count`` is total messages stored; one turn = (user, assistant).
        turns = count // 2
        every_n = max(1, int(self._profile.update_every_n_turns))
        if turns > 0 and turns % every_n == 0:
            self._profile.maybe_update_async(session_key)
