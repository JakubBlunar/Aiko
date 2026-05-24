"""The single-turn orchestrator.

Replaces the old ``_AgentWrapper`` + ``AgentController`` + ``TurnTriage`` +
``ReasonActReflect`` stack. One ``run()`` call:

  1. Build prompt via :class:`PromptAssembler`.
  2. Stream from Ollama (cancellable via ``stop_requested``).
  3. Parse ``[[reaction:X]]`` once at the start of the stream.
  4. Strip meta tags for display; emit incremental text via ``on_token``.
  5. Chunk text into sentences for TTS via ``on_tts_chunk``.
  6. Persist the user + assistant messages.
  7. Kick off background workers (summary).

No tool-calling in v1 -- that hooks in later through
``OllamaClient.chat_with_tools`` without touching the rest of the pipeline.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, TYPE_CHECKING

from app.core.chat_database import ChatDatabase
from app.core.prompt_assembler import PromptAssembler
from app.core.services.response_text_service import (
    parse_reaction_at_start,
    safe_visible_prefix,
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

if TYPE_CHECKING:
    from app.core.memory_store import MemoryStore
    from app.llm.embedder import Embedder


_REMEMBER_TAG_RE = re.compile(r"\[\[remember:([^\]]+?)\]\]", flags=re.IGNORECASE)


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
        summary_worker: SummaryWorker | None = None,
        memory_store: "MemoryStore | None" = None,
        embedder: "Embedder | None" = None,
        self_tagged_salience: float = 0.7,
        on_memory_added: Callable[[object], None] | None = None,
    ) -> None:
        self._ollama = ollama
        self._db = db
        self._prompt = prompt_assembler
        self._model = model
        self._context_window = max(2048, int(context_window))
        self._max_tokens = max(64, int(max_tokens))
        self._temperature = float(temperature)
        self._summary = summary_worker
        self._memory_store = memory_store
        self._embedder = embedder
        self._self_tagged_salience = max(0.0, min(1.0, float(self_tagged_salience)))
        self._on_memory_added = on_memory_added
        self._stop = threading.Event()

    def set_memory(
        self,
        store: "MemoryStore | None",
        embedder: "Embedder | None",
    ) -> None:
        self._memory_store = store
        self._embedder = embedder

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
                session_id=session_key,
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

                # Use the streaming-safe prefix so partial tokens like "[[spo"
                # never reach the UI / TTS. Anything past the last unresolved
                # `[` is held back until the next delta arrives.
                visible = safe_visible_prefix(body)

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
        # Final-flush: emit any tail that the streaming holdback was sitting on
        # (e.g. a "[[" that turned out NOT to be a tag) so the UI bubble lands
        # in the same state as the persisted message.
        if on_token is not None and len(body_text) > ui_sent_chars:
            on_token(body_text[ui_sent_chars:])
            ui_sent_chars = len(body_text)
        if (
            on_tts_chunk is not None
            and mood is not None
            and len(body_text) > tts_appended_chars
        ):
            tts_buffer += body_text[tts_appended_chars:]
            tts_appended_chars = len(body_text)
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

        assistant_message_id: int | None = None
        if cleaned and not aborted:
            assistant_message_id = self._db.add_message(
                session_id=session_key,
                role="assistant",
                content=cleaned,
                token_count=usage.completion_tokens or estimate_tokens(cleaned),
            )
            self._extract_self_tagged_memories(
                full_raw,
                session_key=session_key,
                assistant_message_id=assistant_message_id,
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

    def _extract_self_tagged_memories(
        self,
        raw_text: str,
        *,
        session_key: str,
        assistant_message_id: int | None,
    ) -> None:
        """Harvest ``[[remember:...]]`` tags from the assistant's raw output.

        Only runs when both a memory store and embedder are configured. Each
        unique tag becomes one ``self_tagged`` memory. Failures are logged
        but never raised back into the turn -- a broken extractor must not
        kill the chat.
        """
        if (
            self._memory_store is None
            or self._embedder is None
            or not raw_text
        ):
            return
        seen: set[str] = set()
        for match in _REMEMBER_TAG_RE.finditer(raw_text):
            content = match.group(1).strip()
            if not content or len(content) < 4:
                continue
            key = content.lower()
            if key in seen:
                continue
            seen.add(key)
            try:
                embedding = self._embedder.embed(content)
            except Exception as exc:
                log.debug("self-tagged memory embed failed: %s", exc)
                continue
            try:
                memory = self._memory_store.add(
                    content=content,
                    kind="self_tagged",
                    embedding=embedding,
                    salience=self._self_tagged_salience,
                    source_session=session_key,
                    source_message_id=assistant_message_id,
                )
            except Exception as exc:
                log.debug("self-tagged memory insert failed: %s", exc)
                continue
            if memory is not None:
                log.info("self-tagged memory: %s", content)
                if self._on_memory_added is not None:
                    try:
                        self._on_memory_added(memory)
                    except Exception:
                        log.debug("on_memory_added listener raised", exc_info=True)
