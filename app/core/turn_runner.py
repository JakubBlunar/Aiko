"""The single-turn orchestrator.

Replaces the old ``_AgentWrapper`` + ``AgentController`` + ``TurnTriage`` +
``ReasonActReflect`` stack. One ``run()`` call:

  1. Build prompt via :class:`PromptAssembler`.
  2. (Optional) Run a non-streaming ``chat_with_tools`` pass: if the model
     emits tool calls, dispatch them and append the tool messages to the
     prompt before streaming.
  3. Stream from Ollama (cancellable via ``stop_requested``).
  4. Parse ``[[reaction:X]]`` once at the start of the stream.
  5. Strip meta tags for display; emit incremental text via ``on_token``.
  6. Chunk text into sentences for TTS via ``on_tts_chunk``.
  7. Persist the user + assistant messages.
  8. Kick off background workers (summary).
"""
from __future__ import annotations

import json
import logging
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, TYPE_CHECKING

from app.core.chat_database import ChatDatabase
from app.core.filler_injector import FillerInjector
from app.core.log_context import reset_turn_id, set_turn_id
from app.core.prompt_assembler import PromptAssembler, PromptTelemetry
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


_REMEMBER_TAG_RE = re.compile(
    r"\[\[remember(?::(?P<kind>self))?:(?P<body>[^\]]+?)\]\]",
    flags=re.IGNORECASE,
)


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
    telemetry: PromptTelemetry | None = None
    compactions_run: int = 0  # synchronous compactions invoked this turn
    first_token_ms: float | None = None  # Phase 1c: time-to-first-stream-delta
    filler_emitted: bool = False  # Phase 1c: did the slow-first-token filler fire?
    raw_text: str = ""  # Phase 4a: full pre-strip output (for tag extraction)


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
        max_prompt_tokens_pct: float = 0.8,
        on_memory_added: Callable[[object], None] | None = None,
        tool_registry: "Any | None" = None,
        on_tool_call: Callable[[str, dict[str, Any]], None] | None = None,
        on_tool_result: Callable[[str, str, bool], None] | None = None,
        filler_threshold_ms: int = 800,
        filler_enabled: bool = True,
        listen_extensions_provider: Callable[[], int] | None = None,
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
        self._max_prompt_tokens_pct = max(0.3, min(0.95, float(max_prompt_tokens_pct)))
        self._on_memory_added = on_memory_added
        self._tool_registry = tool_registry
        self._on_tool_call = on_tool_call
        self._on_tool_result = on_tool_result
        self._stop = threading.Event()
        # Phase 1c: slow-first-token filler.
        self._filler = FillerInjector(
            threshold_ms=filler_threshold_ms,
            enabled=filler_enabled,
        )
        # Best-effort carry-over of the previous reaction so the filler tone
        # matches recent texture. Updated at the end of each successful run.
        self._last_reaction: str | None = None
        # Phase 6 of listening_window_prefetch: callable that returns the
        # most recent live-capture extension count so the "turn done:"
        # log line can show how often hesitation extended the listening
        # window. Returns 0 when not in live voice mode.
        self._listen_extensions_provider = listen_extensions_provider

    def set_tool_registry(self, registry: "Any | None") -> None:
        self._tool_registry = registry

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
        max_prompt_tokens_pct: float | None = None,
        filler_threshold_ms: int | None = None,
        filler_enabled: bool | None = None,
    ) -> None:
        if model is not None:
            self._model = model
        if context_window is not None:
            self._context_window = max(2048, int(context_window))
        if max_tokens is not None:
            self._max_tokens = max(64, int(max_tokens))
        if temperature is not None:
            self._temperature = float(temperature)
        if max_prompt_tokens_pct is not None:
            self._max_prompt_tokens_pct = max(0.3, min(0.95, float(max_prompt_tokens_pct)))
        if filler_threshold_ms is not None or filler_enabled is not None:
            self._filler.update_runtime(
                threshold_ms=filler_threshold_ms,
                enabled=filler_enabled,
            )

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
        resume_user_message_id: int | None = None,
    ) -> TurnResult:
        # Allocate a short-lived correlation id so every nested log line
        # carries `turn=…`. Cleared in the finally below regardless of how
        # the inner body exits (success, return, exception).
        turn_id = secrets.token_hex(4)
        token = set_turn_id(turn_id)
        try:
            return self._run_inner(
                session_key=session_key,
                user_text=user_text,
                on_token=on_token,
                on_tts_chunk=on_tts_chunk,
                stop_requested=stop_requested,
                resume_user_message_id=resume_user_message_id,
            )
        finally:
            reset_turn_id(token)

    def _run_inner(
        self,
        *,
        session_key: str,
        user_text: str,
        on_token: TokenCallback | None,
        on_tts_chunk: TtsChunkCallback | None,
        stop_requested: StopPredicate | None,
        resume_user_message_id: int | None,
    ) -> TurnResult:
        self._stop.clear()
        cleaned_user = sanitize_user_text(user_text)
        if not cleaned_user:
            return TurnResult(text="", reaction="neutral")

        # ``resume_user_message_id`` set: caller (voice merge in
        # ``SessionController.process_live_capture``) already updated the
        # existing user row in the chat DB with the merged text, so we
        # must NOT insert a duplicate ``role="user"`` row here. The id
        # is captured purely for the structured log line below.
        if resume_user_message_id is None:
            self._db.add_message(
                session_id=session_key,
                role="user",
                content=cleaned_user,
                token_count=estimate_tokens(cleaned_user),
            )

        messages, telemetry = self._prompt.assemble_with_budget(
            session_key,
            cleaned_user,
            context_window=self._context_window,
            response_budget=self._max_tokens,
        )

        # On overflow: synchronous compaction → reassemble (aggressive) once.
        compactions_run = 0
        if telemetry.compaction_triggered and self._summary is not None:
            log.info(
                "context overflow projected (est=%d > budget=%d); compacting now",
                telemetry.prompt_tokens_estimate, telemetry.budget_tokens,
            )
            try:
                wrote = self._summary.compact_now(session_key)
            except Exception:
                log.exception("compact_now raised")
                wrote = False
            if wrote:
                compactions_run += 1
            messages, telemetry = self._prompt.assemble_with_budget(
                session_key,
                cleaned_user,
                context_window=self._context_window,
                response_budget=self._max_tokens,
                aggressive=True,
            )

        # Per plan: "turn start" is tweaking-only telemetry, redundant with the
        # end-of-turn structured INFO line. Stays DEBUG so default-INFO logs
        # carry one entry per turn rather than two.
        log.debug(
            "turn start: model=%s session=%s ctx=%d max=%d msgs=%d est=%d resume_id=%s",
            self._model, session_key[:8], self._context_window, self._max_tokens,
            len(messages), telemetry.prompt_tokens_estimate,
            resume_user_message_id if resume_user_message_id is not None else "-",
        )

        # ── Pass 1: tool calling (optional) ──────────────────────────────
        # If a tool registry is attached we let the model decide whether it
        # wants to call any tools before producing the spoken reply. Tool
        # results are appended as ``role="tool"`` messages and the prompt is
        # then sent through ``chat_stream`` for the user-facing reply.
        tool_usage = OllamaUsage()
        if self._tool_registry is not None and len(self._tool_registry) > 0:
            try:
                tool_usage = self._maybe_run_tool_pass(
                    messages, stop_requested=stop_requested,
                )
            except Exception:
                log.exception("tool pre-pass failed; falling back to plain stream")
            # Tool-pass appendments grow the prompt; refresh telemetry so the
            # post-turn metrics reflect what actually got streamed.
            if tool_usage.prompt_tokens or tool_usage.completion_tokens:
                tool_text_total = self._estimate_messages_tokens(messages)
                telemetry.tool_tokens = max(
                    0, tool_text_total - telemetry.prompt_tokens_estimate,
                )
                telemetry.prompt_tokens_estimate = tool_text_total

        # Streaming bookkeeping.
        accumulator: list[str] = []
        mood: str | None = None
        ui_sent_chars = 0       # chars of meta-stripped body already sent to UI
        tts_appended_chars = 0  # chars of meta-stripped body already routed to TTS
        tts_buffer = ""         # rolling buffer of body chars not yet spoken
        aborted = False
        first_delta_seen = False
        first_token_ms: float | None = None
        t0 = time.monotonic()

        # Phase 1c: arm slow-first-token filler. Disarmed on first delta.
        self._filler.arm(
            on_tts_chunk,
            carry_over_reaction=self._last_reaction,
        )

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
                if not first_delta_seen:
                    first_delta_seen = True
                    first_token_ms = (time.monotonic() - t0) * 1000.0
                    # Cancel filler watchdog. If it already fired, the
                    # filler chunk is in the TTS queue and the real reply
                    # will follow it; nothing else to do.
                    self._filler.disarm()
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
            self._filler.disarm()
            log.warning("stream failed: %s", exc)
            raise
        finally:
            # Belt-and-braces: ensure the watchdog is never left armed.
            self._filler.disarm()

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

        # Merge the tool-pass usage into the streaming-pass usage so the turn
        # totals reflect every Ollama call we made.
        usage = self._ollama.last_usage.merge(tool_usage)
        duration_ms = (time.monotonic() - t0) * 1000.0

        # Decide whether to schedule a proactive (background) compaction
        # because this turn left the prompt close to the limit.
        if (
            usage.prompt_tokens > 0
            and self._context_window > 0
            and self._summary is not None
        ):
            prompt_pct = usage.prompt_tokens / float(self._context_window)
            if prompt_pct >= self._max_prompt_tokens_pct:
                try:
                    self._summary.notify_compaction_soon(session_key)
                    log.info(
                        "prompt at %.0f%% of ctx; scheduling background compaction",
                        prompt_pct * 100.0,
                    )
                except Exception:
                    log.debug("notify_compaction_soon failed", exc_info=True)

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

        # Per plan: one structured INFO line per turn so a single grep
        # (e.g. `turn=abc12345`) yields the headline metrics. The order of
        # key=value pairs is part of the contract documented in
        # AGENTS.md "Debugging via logs".
        ctx_pct = (
            (usage.prompt_tokens / self._context_window) * 100.0
            if self._context_window and usage.prompt_tokens
            else 0.0
        )
        tool_calls = sum(1 for m in messages if m.get("role") == "tool")
        listen_extensions = 0
        if self._listen_extensions_provider is not None:
            try:
                listen_extensions = int(self._listen_extensions_provider() or 0)
            except Exception:
                listen_extensions = 0
        log.info(
            "turn done: chars=%d mood=%s prompt=%d completion=%d ctx_pct=%.1f "
            "first_token_ms=%s total_ms=%.0f eval_ms=%.0f tools=%d "
            "compactions=%d filler=%s aborted=%s "
            "rag_prefetch=%s prebuild=%s listen_extensions=%d",
            len(cleaned),
            mood or "neutral",
            usage.prompt_tokens,
            usage.completion_tokens,
            ctx_pct,
            f"{first_token_ms:.0f}" if first_token_ms is not None else "-",
            duration_ms,
            usage.eval_duration_ms,
            tool_calls,
            compactions_run,
            "1" if self._filler.fired else "0",
            "1" if aborted else "0",
            telemetry.rag_prefetch_event,
            telemetry.slice_cache_event,
            listen_extensions,
        )

        # Carry-over for the *next* turn's filler tone.
        self._last_reaction = mood or self._last_reaction

        return TurnResult(
            text=cleaned,
            reaction=mood or "neutral",
            usage=usage,
            aborted=aborted,
            duration_ms=duration_ms,
            telemetry=telemetry,
            compactions_run=compactions_run,
            first_token_ms=first_token_ms,
            filler_emitted=self._filler.fired,
            raw_text=full_raw,
        )

    # ── helpers ───────────────────────────────────────────────────────

    def _maybe_run_tool_pass(
        self,
        messages: list[dict[str, Any]],
        *,
        stop_requested: StopPredicate | None,
        max_rounds: int = 2,
    ) -> OllamaUsage:
        """Run up to ``max_rounds`` ``chat_with_tools`` passes and mutate
        ``messages`` in place by appending the assistant's tool_calls and
        the corresponding ``tool`` results.

        Returns the cumulative :class:`OllamaUsage` across all rounds so the
        caller can merge it into the streaming-pass usage (gives the user
        accurate token totals across the whole turn).

        We bail early if the model returns no tool calls, the stop event is
        set, or anything goes sideways (the streaming pass is the
        authoritative final reply, so silently dropping tool augmentation
        is fine).
        """
        total_usage = OllamaUsage()
        registry = self._tool_registry
        if registry is None:
            return total_usage
        tool_schemas = registry.to_ollama_tools()
        if not tool_schemas:
            return total_usage

        for round_idx in range(max_rounds):
            if self._is_stop_requested(stop_requested) or self._stop.is_set():
                return total_usage
            try:
                response = self._ollama.chat_with_tools(
                    messages,
                    options={
                        "temperature": self._temperature,
                        "num_ctx": self._context_window,
                        # Tool selection rarely needs a long completion.
                        "num_predict": min(self._max_tokens, 256),
                    },
                    tools=tool_schemas,
                    model=self._model,
                )
            except Exception:
                log.exception("chat_with_tools round %d failed", round_idx)
                return total_usage
            # OllamaClient stamps last_usage on every chat_with_tools call.
            tool_call_usage = getattr(self._ollama, "last_usage", None)
            if isinstance(tool_call_usage, OllamaUsage):
                total_usage = total_usage.merge(tool_call_usage)
            if not response.tool_calls:
                return total_usage

            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        }
                    }
                    for call in response.tool_calls
                ],
            }
            messages.append(assistant_msg)

            for call in response.tool_calls:
                if self._on_tool_call is not None:
                    try:
                        self._on_tool_call(call.name, dict(call.arguments))
                    except Exception:
                        log.exception("on_tool_call listener failed")
                result = registry.dispatch(
                    call.name,
                    call.arguments,
                    call_id=call.call_id,
                )
                if self._on_tool_result is not None:
                    try:
                        self._on_tool_result(result.name, result.content, result.ok)
                    except Exception:
                        log.exception("on_tool_result listener failed")
                tool_msg: dict[str, Any] = {
                    "role": "tool",
                    "name": result.name,
                    "content": result.content,
                }
                messages.append(tool_msg)
                log.info(
                    "tool dispatch: name=%s ok=%s len=%d",
                    result.name, result.ok, len(result.content),
                )
        return total_usage

    @staticmethod
    def _estimate_messages_tokens(messages: list[dict[str, Any]]) -> int:
        total = 0
        for msg in messages:
            content = str(msg.get("content") or "")
            total += estimate_tokens(content) + 4  # _MESSAGE_OVERHEAD
        return total

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
        seen: set[tuple[str, str]] = set()
        for match in _REMEMBER_TAG_RE.finditer(raw_text):
            content = (match.group("body") or "").strip()
            if not content or len(content) < 4:
                continue
            kind_marker = (match.group("kind") or "").strip().lower()
            # ``[[remember:self:...]]`` -> Aiko's own notes about herself,
            # surfaced separately in the prompt block. Plain ``[[remember:...]]``
            # remains a "self_tagged" Jacob fact (Aiko's explicit annotation).
            kind = "self" if kind_marker == "self" else "self_tagged"
            key = (kind, content.lower())
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
                    kind=kind,
                    embedding=embedding,
                    salience=self._self_tagged_salience,
                    source_session=session_key,
                    source_message_id=assistant_message_id,
                )
            except Exception as exc:
                log.debug("self-tagged memory insert failed: %s", exc)
                continue
            if memory is not None:
                log.info("%s memory: %s", kind, content)
                if self._on_memory_added is not None:
                    try:
                        self._on_memory_added(memory)
                    except Exception:
                        log.debug("on_memory_added listener raised", exc_info=True)
