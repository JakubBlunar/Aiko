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
from typing import Any, Callable, Iterable, TYPE_CHECKING

from app.core.infra.chat_database import ChatDatabase
from app.core.voice.filler_injector import FillerInjector
from app.core.infra.log_context import reset_turn_id, set_turn_id
from app.core.session.prompt_assembler import PromptAssembler, PromptTelemetry
from app.core.services.response_text_service import (
    parse_reaction_at_start,
    parse_reaction_stack_at_start,
    safe_visible_prefix,
    split_text_with_stage_directions,
    strip_all_meta_tags,
)
from app.core.session.session_text_utils import (
    drain_tts_stream_chunks,
    prepare_tts_text,
    sanitize_assistant_text,
    sanitize_user_text,
)
from app.core.proactive.summary_worker import SummaryWorker
from app.core.session.tool_pass_gate import (
    BRAIN_CORE_FAMILIES,
    GateContext,
    GateDecision,
    select_active_tool_names,
    should_run_tool_pass,
)
from app.llm.ollama_client import OllamaClient, OllamaUsage
from app.llm.token_utils import estimate_tokens

if TYPE_CHECKING:
    from app.core.memory.memory_store import MemoryStore
    from app.llm.embedder import Embedder


_REMEMBER_TAG_RE = re.compile(
    r"\[\[remember(?::(?P<kind>self))?:(?P<body>[^\]]+?)\]\]",
    flags=re.IGNORECASE,
)

# ── forced-choice escape tool ─────────────────────────────────────────
# Chatty models (gpt-5-mini at any reasoning_effort) lose the implicit
# "text vs tool" coin-flip on ``tool_choice="auto"``: they narrate their
# intent ("I'll list the folders") instead of emitting the call. The fix
# is to force ``tool_choice="required"`` so the model MUST pick a tool,
# and to add this synthetic no-op tool as the "I don't actually need a
# tool" escape hatch. That reframes the decision from "text vs tool"
# (which the conversational prior always wins) to "which tool" (where the
# right tool for "what files can you see?" is obviously list_file_roots).
# It is never registered in the ToolRegistry; the turn pass strips it out
# and, when it's the only call, proceeds straight to narration.
_RESPOND_DIRECTLY_TOOL = "respond_directly"
_RESPOND_DIRECTLY_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": _RESPOND_DIRECTLY_TOOL,
        "description": (
            "Select this when NO other tool is needed and you can reply to "
            "the user directly from the conversation -- normal chat, "
            "opinions, banter, reactions, or anything you already know. "
            "This is the 'no tool needed' choice and does nothing on its "
            "own. Pick a real tool instead ONLY when the user needs "
            "information or an action you cannot produce yourself: listing "
            "or reading files, the current time/date, web facts, your saved "
            "memories, or changing your room."
        ),
        "parameters": {"type": "object", "properties": {}},
    },
}


# Stable header fragments emitted by ``app.core.tasks.cue_render``. When
# one is present in the system prompt a finished-task result is already
# in front of the model, so we relax ``tool_choice`` from "required" to
# "auto" — forcing a pick there just tempts the model to re-run the task
# it already finished (the exact bug the reply-on-complete work fixes).
# These MUST be distinctive enough that the persona's *explanation* of the
# finished-task cue (data/persona/aiko_companion.txt teaches Aiko what the
# block looks like, quoting "...reply now using the result below") does NOT
# trip the detector. The persona's paraphrase stops at "result below"; the
# real ``cue_render`` headers continue past it, so we key off that tail and
# the exact success-header (with its trailing colon). Keep these in lockstep
# with ``_REPLY_HEADER`` / ``_SUCCESS_HEADER`` in app.core.tasks.cue_render.
_FINISHED_TASK_SENTINELS = (
    "reply now using the result below. Do NOT start the task again",
    "Tasks that finished since your last message:",
)


def _messages_have_finished_task_block(
    messages: list[dict[str, Any]],
) -> bool:
    """True when a finished-task cue/reply block is in the system prompt."""
    for msg in messages:
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str) and any(
            sentinel in content for sentinel in _FINISHED_TASK_SENTINELS
        ):
            return True
    return False


log = logging.getLogger("app.turn_runner")


TokenCallback = Callable[[str], None]
TtsChunkCallback = Callable[[str, str], None]
# Phase 1c: stage-direction earcon callback. Receives the kind name
# (``laugh`` / ``sigh`` / ``gasp`` / ``hum`` / ``tsk``) at the *exact*
# point in the stream where it appeared, so the TTS pipeline can
# splice the earcon between text chunks.
EarconCallback = Callable[[str], None]
"""Signature: ``(prepared_text, reaction)``."""

# Alexia bundle: ``[[overlay:NAME]]`` callback. Receives the overlay
# name (``sweat`` / ``blush`` / ``dizzy`` / ...) at the same point in
# the stream as the corresponding tag — TurnRunner forwards it to a
# WS event so the renderer pulses the matching parameter.
OverlayCallback = Callable[[str], None]

# ``[[outfit:NAME]]`` callback — sticky outfit override (pajamas / day).
# Sent to the SessionController which decides whether to apply it
# (might be ignored if the user has manually forced an outfit).
OutfitCallback = Callable[[str], None]

# ``[[motion:NAME]]`` callback — fire-and-forget motion playback.
# SessionController resolves the motion-file index in the rig and
# broadcasts an ``avatar_motion`` WS event for the renderer.
MotionCallback = Callable[[str], None]

# K31 soft physicality: ``[[touch:KIND]]`` callback — fires for
# every touch tag in stream order. B7 widened it to carry the two
# optional fields ``(kind, emoji, label)``: built-ins ignore the extra
# fields, invented kinds use them for the badge. The SessionController
# threads this to ``avatar_mixin._emit_avatar_touch`` which (on
# dispatch) broadcasts an ``avatar_touch`` WS event AND records the
# gesture descriptor on the assistant message row's ``gestures`` column.
TouchCallback = Callable[[str, str, str], None]

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
    # True when the LLM omitted ``[[reaction:X]]`` and we had to default to
    # ``neutral`` so the response still got spoken / side-channels still
    # dispatched. Surfaced by the MCP ``get_last_response_detail`` tool so
    # we can see at a glance whether grammar compliance is degrading.
    mood_fallback: bool = False
    # H1 + K4: the persisted ``messages`` row id for this turn's
    # assistant reply, exposed so post-turn flows can stamp the
    # parsed arc onto ``messages.arc`` without re-querying the DB.
    # ``None`` when the reply was empty / aborted and never persisted.
    assistant_message_id: int | None = None


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
        tool_pass_gate_enabled: bool = True,
        tasks_active_provider: Callable[[], bool] | None = None,
        skill_router_enabled: bool = False,
        brain_core_families: "Iterable[str] | None" = None,
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
        # ── P14: heuristic tool-pass gate ─────────────────────────────
        # When enabled, banter turns with no tool-shaped signal skip the
        # forced ``chat_with_tools`` decision pass entirely (the largest
        # avoidable TTFT contributor). ``tasks_active_provider`` is an
        # optional continuity hook injected by SessionController — True
        # when any task is running / awaiting_input / paused, in which
        # case the pass always runs (the user's message may be the
        # answer a pending task is waiting for).
        self._tool_pass_gate_enabled = bool(tool_pass_gate_enabled)
        self._tasks_active_provider = tasks_active_provider
        # True when the *previous* turn dispatched at least one real
        # tool — follow-ups like "and the other folder?" carry no
        # tool-shaped token of their own, so the gate lets them through.
        self._last_turn_dispatched_tool = False
        # One-shot MCP bypass (``force_tool_pass``): consumed on the
        # next turn whether or not the pass dispatches anything.
        self._tool_gate_force_next = False
        # Observability counters for the MCP ``get_tool_gate_state``
        # surface. ``_tool_pass_ms_total`` / ``_tool_pass_count`` give a
        # rolling average pass cost so skipped passes can be priced.
        self._tool_gate_last: GateDecision | None = None
        self._tool_gate_turns = 0
        self._tool_gate_skips = 0
        self._tool_pass_ms_total = 0.0
        self._tool_pass_count = 0
        # ── brain-lane skill router (progressive tool disclosure) ──────
        # When enabled, a tool-shaped turn exposes only the matched
        # families' tools plus the always-on core (time / recall / world),
        # instead of the whole registry. Off = today's behaviour (all
        # tools every gated turn). World is in the core so Aiko's
        # spontaneous room actions survive on any turn.
        self._skill_router_enabled = bool(skill_router_enabled)
        self._brain_core_families: frozenset[str] = (
            frozenset(brain_core_families)
            if brain_core_families is not None
            else BRAIN_CORE_FAMILIES
        )
        # Names sent on the last run pass (for MCP get_tool_gate_state).
        self._last_active_tools: list[str] | None = None

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
        client: Any | None = None,
        model: str | None = None,
        context_window: int | None = None,
        max_tokens: int | None = None,
        temperature: float | None = None,
        max_prompt_tokens_pct: float | None = None,
        filler_threshold_ms: int | None = None,
        filler_enabled: bool | None = None,
        tool_pass_gate_enabled: bool | None = None,
        skill_router_enabled: bool | None = None,
        brain_core_families: "Iterable[str] | None" = None,
    ) -> None:
        # ``client`` lets ``SessionController.reconfigure_chat_llm`` swap
        # the chat-LLM backend (e.g. Ollama -> Gemini) without recreating
        # the TurnRunner. Existing in-flight turns keep their reference
        # because the attribute is captured into local variables at the
        # top of ``run()``; new turns pick up the new client.
        if client is not None:
            self._ollama = client
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
        if tool_pass_gate_enabled is not None:
            self._tool_pass_gate_enabled = bool(tool_pass_gate_enabled)
        if skill_router_enabled is not None:
            self._skill_router_enabled = bool(skill_router_enabled)
        if brain_core_families is not None:
            self._brain_core_families = frozenset(brain_core_families)

    def request_stop(self) -> None:
        self._stop.set()

    @staticmethod
    def _dispatch_chunk_with_earcons(
        chunk: str,
        *,
        mood: str,
        on_tts_chunk: TtsChunkCallback,
        on_earcon: EarconCallback | None,
        on_overlay: OverlayCallback | None = None,
        on_outfit: OutfitCallback | None = None,
        on_motion: MotionCallback | None = None,
        on_touch: TouchCallback | None = None,
    ) -> None:
        """Phase 1c: split a sentence into text + stage-direction
        earcons and emit each piece in order.

        ``prepare_tts_text`` strips every ``[[...]]`` indiscriminately,
        so we MUST extract the earcon side-channel here before that
        runs. Pieces are dispatched in stream order so a sentence like
        "Yeah [[laugh]] right" plays "Yeah" → laugh-earcon → "right".

        Phase 3c: ``[[correct]]old[[/correct]]new`` blocks emit a
        synthetic ``tsk`` earcon at the correction boundary (right
        before the new text) so the avatar audibly catches itself,
        then the new text is spoken normally.

        Alexia bundle: ``[[overlay:NAME]]`` (transient pulse),
        ``[[outfit:NAME]]`` (sticky outfit override), and
        ``[[motion:NAME]]`` (Live2D motion playback) markers are all
        extracted in the same stream pass and forwarded via their
        respective callbacks, then stripped so the TTS pipeline
        never sees them.
        """
        # Phase 3c: turn each [[correct]]…[[/correct]] into a tsk-earcon
        # marker so it lands in the same earcon channel everything else
        # uses. The replacement happens BEFORE
        # ``split_text_with_stage_directions`` so the boundary marker
        # interleaves with any other earcons in stream order.
        from app.core.services.response_text_service import (
            _CORRECTION_BLOCK_PATTERN,
            _MOTION_TAG_PATTERN,
            _OUTFIT_TAG_PATTERN,
            _OVERLAY_TAG_PATTERN,
            _TOUCH_TAG_PATTERN,
        )

        # Fire avatar side-channel markers first (overlay / outfit /
        # motion / touch are all stage directions, not spoken).
        # Stripping them before the earcon split keeps the TTS flow
        # oblivious.
        if on_overlay is not None:
            for match in _OVERLAY_TAG_PATTERN.finditer(chunk):
                try:
                    on_overlay(match.group(1).strip().lower())
                except Exception:
                    log.debug("on_overlay raised", exc_info=True)
        chunk = _OVERLAY_TAG_PATTERN.sub("", chunk)
        if on_outfit is not None:
            for match in _OUTFIT_TAG_PATTERN.finditer(chunk):
                try:
                    on_outfit(match.group(1).strip().lower())
                except Exception:
                    log.debug("on_outfit raised", exc_info=True)
        chunk = _OUTFIT_TAG_PATTERN.sub("", chunk)
        if on_motion is not None:
            for match in _MOTION_TAG_PATTERN.finditer(chunk):
                try:
                    on_motion(match.group(1).strip().lower())
                except Exception:
                    log.debug("on_motion raised", exc_info=True)
        chunk = _MOTION_TAG_PATTERN.sub("", chunk)
        # K31 soft physicality: ``[[touch:KIND]]`` is a stage direction
        # (avatar lean-in + bubble badge); strip from the TTS path and
        # dispatch to the side-channel here.
        if on_touch is not None:
            for match in _TOUCH_TAG_PATTERN.finditer(chunk):
                try:
                    on_touch(
                        match.group(1).strip().lower(),
                        (match.group(2) or "").strip(),
                        (match.group(3) or "").strip(),
                    )
                except Exception:
                    log.debug("on_touch raised", exc_info=True)
        chunk = _TOUCH_TAG_PATTERN.sub("", chunk)

        chunk_with_tsk = _CORRECTION_BLOCK_PATTERN.sub("[[tsk]]", chunk)
        pieces = split_text_with_stage_directions(chunk_with_tsk)
        if not pieces:
            return
        for kind, content in pieces:
            if kind == "text":
                prepared = prepare_tts_text(content)
                if prepared:
                    on_tts_chunk(prepared, mood)
            elif kind == "earcon" and on_earcon is not None:
                try:
                    on_earcon(content)
                except Exception:
                    log.debug("on_earcon raised", exc_info=True)

    def run(
        self,
        session_key: str,
        user_text: str,
        *,
        on_token: TokenCallback | None = None,
        on_tts_chunk: TtsChunkCallback | None = None,
        on_earcon: EarconCallback | None = None,
        on_overlay: OverlayCallback | None = None,
        on_outfit: OutfitCallback | None = None,
        on_motion: MotionCallback | None = None,
        on_touch: TouchCallback | None = None,
        stop_requested: StopPredicate | None = None,
        resume_user_message_id: int | None = None,
    ) -> TurnResult:
        # Allocate a short-lived correlation id so every nested log line
        # carries `turn=…`. Cleared in the finally below regardless of how
        # the inner body exits (success, return, exception).
        turn_id = secrets.token_hex(4)
        token = set_turn_id(turn_id)
        # P1 (perf backlog): start the per-turn embed counters on this
        # thread. ``_run_inner`` reads them via ``end_turn`` right
        # before the headline INFO log so the counters land both on
        # the log line *and* on ``result.telemetry``. The ``finally``
        # below calls ``end_turn`` again as a defensive cleanup -- it
        # returns ``(0, 0.0)`` on an already-ended turn, so the double
        # call never inflates the numbers, but it does guarantee that
        # an exception mid-flow doesn't leak counter state into the
        # next turn on the same thread.
        if self._embedder is not None:
            try:
                self._embedder.begin_turn()
            except Exception:
                log.debug("embedder.begin_turn failed", exc_info=True)
        try:
            return self._run_inner(
                session_key=session_key,
                user_text=user_text,
                on_token=on_token,
                on_tts_chunk=on_tts_chunk,
                on_earcon=on_earcon,
                on_overlay=on_overlay,
                on_outfit=on_outfit,
                on_motion=on_motion,
                on_touch=on_touch,
                stop_requested=stop_requested,
                resume_user_message_id=resume_user_message_id,
            )
        finally:
            if self._embedder is not None:
                try:
                    self._embedder.end_turn()
                except Exception:
                    log.debug(
                        "embedder.end_turn (cleanup) failed", exc_info=True,
                    )
            reset_turn_id(token)

    def _run_inner(
        self,
        *,
        session_key: str,
        user_text: str,
        on_token: TokenCallback | None,
        on_tts_chunk: TtsChunkCallback | None,
        on_earcon: EarconCallback | None,
        on_overlay: OverlayCallback | None,
        on_outfit: OutfitCallback | None,
        on_motion: MotionCallback | None,
        on_touch: TouchCallback | None,
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

        # ── Pass 1: tool calling (optional, P14-gated) ───────────────────
        # If a tool registry is attached we let the model decide whether it
        # wants to call any tools before producing the spoken reply. Tool
        # results are appended as ``role="tool"`` messages and the prompt is
        # then sent through ``chat_stream`` for the user-facing reply.
        #
        # P14: the heuristic gate skips the decision pass entirely on turns
        # with no tool-shaped signal (the pass is a full non-streaming LLM
        # round-trip — the largest avoidable TTFT contributor). Continuity
        # signals (finished-task block, active tasks, previous turn used a
        # tool, MCP force flag) always run the pass; the kill-switch
        # ``agent.tool_pass_gate_enabled=false`` restores the old
        # always-run behaviour.
        tool_usage = OllamaUsage()
        if self._tool_registry is not None and len(self._tool_registry) > 0:
            gate_decision = self._gate_tool_pass(cleaned_user, messages)
            telemetry.tool_gate_event = gate_decision.as_event()
            if gate_decision.run:
                tool_pass_t0 = time.monotonic()
                # Brain-lane skill router: narrow the tool schema list to
                # the matched families + always-on core when enabled.
                # ``None`` (router off / widen-case) sends the full set.
                allow = select_active_tool_names(
                    gate_decision,
                    self._tool_registry.names(),
                    core_families=self._brain_core_families,
                    router_enabled=self._skill_router_enabled,
                )
                try:
                    tool_usage = self._maybe_run_tool_pass(
                        messages, stop_requested=stop_requested, allow=allow,
                    )
                except Exception:
                    log.exception("tool pre-pass failed; falling back to plain stream")
                telemetry.tool_pass_ms = round(
                    (time.monotonic() - tool_pass_t0) * 1000.0, 2,
                )
                self._tool_pass_ms_total += telemetry.tool_pass_ms
                self._tool_pass_count += 1
            else:
                # Pass skipped: this turn dispatches nothing, so the
                # continuity flag for the NEXT turn clears here
                # (``_maybe_run_tool_pass`` owns it on the run path).
                self._last_turn_dispatched_tool = False
                self._tool_gate_skips += 1
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
                surface="turn_stream",
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
                # then operate on the body text afterwards. Phase 3
                # stacked reactions ``[[reaction:A+B]]`` route the
                # primary (A) into ``mood`` as before; companions (B,
                # C, ...) fire as long-duration overlay pulses on top
                # so the stacked emotional texture lands at the
                # renderer.
                if mood is None:
                    parsed_mood, companions, body_after_react = (
                        parse_reaction_stack_at_start(full)
                    )
                    if parsed_mood is not None:
                        mood = parsed_mood
                        if on_overlay is not None:
                            for companion in companions:
                                try:
                                    on_overlay(companion)
                                except Exception:
                                    log.debug(
                                        "on_overlay (reaction companion) raised",
                                        exc_info=True,
                                    )
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
                            self._dispatch_chunk_with_earcons(
                                chunk,
                                mood=mood or "neutral",
                                on_tts_chunk=on_tts_chunk,
                                on_earcon=on_earcon,
                                on_overlay=on_overlay,
                                on_outfit=on_outfit,
                                on_motion=on_motion,
                                on_touch=on_touch,
                            )

        except Exception as exc:
            self._filler.disarm()
            log.warning("stream failed: %s", exc)
            raise
        finally:
            # Belt-and-braces: ensure the watchdog is never left armed.
            self._filler.disarm()

        full_raw = "".join(accumulator)
        if mood is None:
            # Phase 3 stack form: route the primary into ``mood`` and
            # fire companions through ``on_overlay`` so the renderer
            # still gets the stacked texture even when the streaming
            # parser missed it (e.g. the tag arrived in a single
            # final delta).
            parsed_mood, companions, full_raw = parse_reaction_stack_at_start(
                full_raw,
            )
            if parsed_mood is not None:
                mood = parsed_mood
                if on_overlay is not None:
                    for companion in companions:
                        try:
                            on_overlay(companion)
                        except Exception:
                            log.debug(
                                "on_overlay (reaction companion) raised",
                                exc_info=True,
                            )
        # Visibility: log the raw output once per turn so we can finally
        # see whether the LLM is emitting structural tags or only prose.
        # ``%r`` keeps multi-line responses on a single log line. The
        # tag-presence summary is greppable ("llm tags:") so the common
        # "is reaction missing again?" question takes seconds to answer.
        log.info("llm raw response: %r", full_raw)
        log.info(
            "llm tags: reaction=%s outfit=%s motion=%s overlay=%s remember=%s",
            "Y" if "[[reaction:" in full_raw else "n",
            "Y" if "[[outfit:" in full_raw else "n",
            "Y" if "[[motion:" in full_raw else "n",
            "Y" if "[[overlay:" in full_raw else "n",
            "Y" if "[[remember" in full_raw else "n",
        )
        # Mood fallback: when the LLM forgets ``[[reaction:X]]`` the
        # streaming branch's ``mood is not None`` gate suppressed every
        # TTS chunk for the whole turn. Default to ``neutral`` here so
        # the final-flush path below picks up the full body in one go.
        mood_fallback = False
        if mood is None:
            mood = "neutral"
            mood_fallback = True
            log.info(
                "mood fallback: defaulting to neutral (LLM omitted "
                "[[reaction:X]] tag)",
            )
        # Side-channel dispatch from the un-stripped raw text. The
        # per-chunk path inside ``_dispatch_chunk_with_earcons`` runs
        # against ``tts_buffer`` which is filled from
        # ``safe_visible_prefix(body)`` -- and that helper already
        # strips overlay/outfit/motion tags via ``strip_all_meta_tags``
        # before the dispatcher ever sees them. End result: per-chunk
        # ``on_outfit`` / ``on_motion`` / ``on_overlay`` were silently
        # never firing, even on the happy ``[[reaction:X]]`` path. Run
        # the extraction once here against ``full_raw`` (the only copy
        # of the text that still carries the tags) so the callbacks
        # fire exactly once per tag, in stream order, regardless of
        # whether the reaction tag was emitted.
        from app.core.services.response_text_service import (
            _MOTION_TAG_PATTERN,
            _OUTFIT_TAG_PATTERN,
            _OVERLAY_TAG_PATTERN,
            _TOUCH_TAG_PATTERN,
        )

        if not aborted:
            if on_overlay is not None:
                for match in _OVERLAY_TAG_PATTERN.finditer(full_raw):
                    try:
                        on_overlay(match.group(1).strip().lower())
                    except Exception:
                        log.debug("on_overlay raised", exc_info=True)
            if on_outfit is not None:
                for match in _OUTFIT_TAG_PATTERN.finditer(full_raw):
                    try:
                        on_outfit(match.group(1).strip().lower())
                    except Exception:
                        log.debug("on_outfit raised", exc_info=True)
            if on_motion is not None:
                for match in _MOTION_TAG_PATTERN.finditer(full_raw):
                    try:
                        on_motion(match.group(1).strip().lower())
                    except Exception:
                        log.debug("on_motion raised", exc_info=True)
            if on_touch is not None:
                for match in _TOUCH_TAG_PATTERN.finditer(full_raw):
                    try:
                        on_touch(
                            match.group(1).strip().lower(),
                            (match.group(2) or "").strip(),
                            (match.group(3) or "").strip(),
                        )
                    except Exception:
                        log.debug("on_touch raised", exc_info=True)
        # Reconcile against the SAME normalization the streaming path used.
        # The streamed `visible` (and therefore `ui_sent_chars` /
        # `tts_appended_chars`) is derived from the reaction-parsed body,
        # and `parse_reaction_stack_at_start` does `.lstrip("\n")` on the
        # tail — so the newline after `[[reaction:X]]\n` is gone. Running
        # `strip_all_meta_tags(full_raw)` here instead would keep that
        # leading newline (the helper intentionally does not `.strip()`),
        # making `body_text` one char longer than what was streamed. The
        # catch-up flush below would then re-emit the message's final
        # character to both the UI (a doubled trailing char, e.g.
        # "later..") and the TTS buffer (a lone "." synthesized as a
        # garbled micro-utterance after a pause). Pre-stripping the
        # reaction stack keeps the offsets aligned so the flush only fires
        # for genuinely-held-back tails.
        _, _, body_after_reaction = parse_reaction_stack_at_start(full_raw)
        body_text = strip_all_meta_tags(body_after_reaction)
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
                self._dispatch_chunk_with_earcons(
                    chunk,
                    mood=mood or "neutral",
                    on_tts_chunk=on_tts_chunk,
                    on_earcon=on_earcon,
                    on_overlay=on_overlay,
                    on_outfit=on_outfit,
                    on_motion=on_motion,
                    on_touch=on_touch,
                )

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
        # P1: read the embedder's per-turn counters and stamp them on
        # telemetry so the headline log line below can include them.
        # Public ``run()`` also calls ``end_turn`` in its finally as a
        # defensive cleanup -- safe because ``end_turn`` returns
        # ``(0, 0.0)`` on a thread that doesn't currently have an
        # active turn, so a double-end never inflates the numbers.
        if self._embedder is not None:
            try:
                embed_calls, embed_ms = self._embedder.end_turn()
            except Exception:
                log.debug("embedder.end_turn failed", exc_info=True)
                embed_calls, embed_ms = (0, 0.0)
            telemetry.embed_calls = int(embed_calls)
            telemetry.embed_ms = round(float(embed_ms), 2)
        # Prompt-cache observability: ``cached`` is the absolute count
        # of prompt tokens that hit the provider's prefix cache (only
        # populated by OpenAI today; Ollama / Gemini / Groq / OpenRouter
        # leave it at 0). ``cached_pct`` is the hit-rate against
        # ``prompt_tokens``. Healthy OpenAI sessions settle around 80-95
        # after the second turn; a number stuck near 0 on OpenAI points
        # at a misplaced prompt block — see ``docs/prompt-caching.md``.
        log.info(
            "turn done: chars=%d mood=%s prompt=%d completion=%d "
            "cached=%d cached_pct=%.1f ctx_pct=%.1f "
            "first_token_ms=%s total_ms=%.0f eval_ms=%.0f tools=%d "
            "compactions=%d filler=%s aborted=%s mood_fallback=%s "
            "rag_prefetch=%s prebuild=%s listen_extensions=%d "
            "embed_calls=%d embed_ms=%.0f assemble_ms=%.0f rag_lookup_ms=%.0f "
            "tool_gate=%s tool_pass_ms=%.0f",
            len(cleaned),
            mood or "neutral",
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.cached_tokens,
            usage.cached_tokens_pct,
            ctx_pct,
            f"{first_token_ms:.0f}" if first_token_ms is not None else "-",
            duration_ms,
            usage.eval_duration_ms,
            tool_calls,
            compactions_run,
            "1" if self._filler.fired else "0",
            "1" if aborted else "0",
            "1" if mood_fallback else "0",
            telemetry.rag_prefetch_event,
            telemetry.slice_cache_event,
            listen_extensions,
            telemetry.embed_calls,
            telemetry.embed_ms,
            telemetry.assemble_ms,
            telemetry.rag_lookup_ms,
            telemetry.tool_gate_event,
            telemetry.tool_pass_ms,
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
            mood_fallback=mood_fallback,
            assistant_message_id=assistant_message_id,
        )

    # ── helpers ───────────────────────────────────────────────────────

    def _gate_tool_pass(
        self,
        user_text: str,
        messages: list[dict[str, Any]],
    ) -> GateDecision:
        """P14: decide whether the tool-decision pass runs this turn.

        Consumes the one-shot ``_tool_gate_force_next`` flag, stamps the
        decision + counters for the MCP ``get_tool_gate_state`` surface,
        and never raises (any failure degrades to "run" — the status
        quo).
        """
        self._tool_gate_turns += 1
        if not self._tool_pass_gate_enabled:
            decision = GateDecision(run=True, reason="disabled")
            self._tool_gate_last = decision
            return decision
        force = self._tool_gate_force_next
        self._tool_gate_force_next = False
        tasks_active = False
        if self._tasks_active_provider is not None:
            try:
                tasks_active = bool(self._tasks_active_provider())
            except Exception:
                log.debug("tasks_active_provider raised", exc_info=True)
        try:
            registry = self._tool_registry
            tool_names = (
                list(registry.names()) if registry is not None else []
            )
            decision = should_run_tool_pass(
                user_text,
                tool_names,
                context=GateContext(
                    finished_task_block=_messages_have_finished_task_block(
                        messages,
                    ),
                    last_turn_dispatched_tool=self._last_turn_dispatched_tool,
                    tasks_active=tasks_active,
                    force=force,
                ),
            )
        except Exception:
            log.exception("tool-pass gate raised; defaulting to run")
            decision = GateDecision(run=True, reason="gate_error")
        self._tool_gate_last = decision
        return decision

    def get_tool_gate_state(self) -> dict[str, Any]:
        """Snapshot for the MCP ``get_tool_gate_state`` debug tool."""
        avg_pass_ms = (
            self._tool_pass_ms_total / self._tool_pass_count
            if self._tool_pass_count
            else 0.0
        )
        last = self._tool_gate_last
        return {
            "enabled": bool(self._tool_pass_gate_enabled),
            "force_next": bool(self._tool_gate_force_next),
            "last_decision": (
                {
                    "run": last.run,
                    "reason": last.reason,
                    "matched": list(last.matched),
                }
                if last is not None
                else None
            ),
            "turns_gated": int(self._tool_gate_turns),
            "passes_skipped": int(self._tool_gate_skips),
            "passes_run": int(self._tool_pass_count),
            "avg_pass_ms": round(avg_pass_ms, 2),
            "est_ms_saved": round(avg_pass_ms * self._tool_gate_skips, 2),
            "last_turn_dispatched_tool": bool(
                self._last_turn_dispatched_tool,
            ),
            "router_enabled": bool(self._skill_router_enabled),
            "core_skills": sorted(self._brain_core_families),
            "last_active_tools": (
                list(self._last_active_tools)
                if self._last_active_tools is not None
                else None
            ),
        }

    def _maybe_run_tool_pass(
        self,
        messages: list[dict[str, Any]],
        *,
        stop_requested: StopPredicate | None,
        max_rounds: int = 2,
        allow: "set[str] | None" = None,
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
        # P14: the run path owns the continuity flag — it flips True only
        # when a real tool is dispatched below, so an escape-only pick or
        # an early bail correctly reads as "no tool used this turn".
        self._last_turn_dispatched_tool = False
        registry = self._tool_registry
        if registry is None:
            return total_usage
        tool_schemas = registry.to_ollama_tools(allow=allow)
        # Safety fallback: narrowing must never strip every tool. If the
        # filtered subset is empty but the registry has tools, send all.
        if not tool_schemas and allow is not None:
            tool_schemas = registry.to_ollama_tools()
        if not tool_schemas:
            self._last_active_tools = []
            return total_usage
        self._last_active_tools = [
            s.get("function", {}).get("name", "") for s in tool_schemas
        ]
        if allow is not None:
            log.debug(
                "skill-router: active=%d/%d names=%s",
                len(tool_schemas),
                len(registry),
                ",".join(self._last_active_tools),
            )
        # Force-pick a tool every round, with the synthetic
        # ``respond_directly`` escape hatch as the "no tool needed"
        # option (see ``_RESPOND_DIRECTLY_SCHEMA``). This is the lever
        # that actually fixes chatty-model under-calling; reasoning
        # effort does not move the decision.
        tool_schemas = [*tool_schemas, _RESPOND_DIRECTLY_SCHEMA]
        # ...except when a finished-task result is already in the prompt:
        # forcing a pick then just tempts the model to re-run the task it
        # already completed. Relax to "auto" so it narrates the result.
        tool_choice = (
            "auto"
            if _messages_have_finished_task_block(messages)
            else "required"
        )

        for round_idx in range(max_rounds):
            if self._is_stop_requested(stop_requested) or self._stop.is_set():
                return total_usage
            try:
                response = self._ollama.chat_with_tools(
                    messages,
                    options={
                        "temperature": self._temperature,
                        "num_ctx": self._context_window,
                        # Tool selection emits a tiny visible payload (a
                        # function name + small JSON args). 512 is plenty
                        # of headroom; the reasoning_effort on this pass is
                        # kept at "minimal" (raising it did not change the
                        # tool-vs-text decision, see openai_compatible_client).
                        "num_predict": min(self._max_tokens, 512),
                    },
                    tools=tool_schemas,
                    tool_choice=tool_choice,
                    model=self._model,
                    surface="tool_pass",
                )
            except Exception:
                log.exception("chat_with_tools round %d failed", round_idx)
                return total_usage
            # OllamaClient stamps last_usage on every chat_with_tools call.
            tool_call_usage = getattr(self._ollama, "last_usage", None)
            if isinstance(tool_call_usage, OllamaUsage):
                total_usage = total_usage.merge(tool_call_usage)
            # Drop the synthetic escape tool: when it's the only pick (or
            # nothing was picked), the model is signalling "just answer" --
            # return WITHOUT appending the tool_calls message, otherwise the
            # streaming pass would carry a dangling tool_call with no
            # matching result and 400 on strict providers.
            real_calls = [
                call for call in (response.tool_calls or [])
                if call.name != _RESPOND_DIRECTLY_TOOL
            ]
            if not real_calls:
                return total_usage

            # Tool-call messages are emitted in a "neutral" shape that
            # both Ollama and OpenAI-compatible clients can consume.
            # Each call carries an ``id`` + ``type=function``; the tool
            # result message carries a ``tool_call_id`` linking back.
            # Ollama tolerates the extra fields (ignored); OpenAI
            # *requires* them or 400s with ``missing_required_parameter``.
            # ``arguments`` stays as a dict here — the OpenAI client
            # JSON-encodes it just before posting, the Ollama client
            # forwards it as-is. ``call_id`` may be empty when the
            # upstream model didn't supply one (older Ollama models);
            # synthesise a stable per-turn id so the round-trip
            # references stay valid.
            tool_call_ids = [
                (call.call_id or f"call_{round_idx}_{idx}")
                for idx, call in enumerate(real_calls)
            ]
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": response.content or "",
                "tool_calls": [
                    {
                        "id": tool_call_ids[idx],
                        "type": "function",
                        "function": {
                            "name": call.name,
                            "arguments": call.arguments,
                        },
                    }
                    for idx, call in enumerate(real_calls)
                ],
            }
            messages.append(assistant_msg)

            for idx, call in enumerate(real_calls):
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
                    "tool_call_id": tool_call_ids[idx],
                    "name": result.name,
                    "content": result.content,
                }
                messages.append(tool_msg)
                log.info(
                    "tool dispatch: name=%s ok=%s len=%d",
                    result.name, result.ok, len(result.content),
                )
                # P14: a real tool ran — the next turn's gate lets
                # follow-ups through without a tool-shaped token.
                self._last_turn_dispatched_tool = True
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
            # remains a "self_tagged" user fact (Aiko's explicit annotation).
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
                    # Schema v8: ``[[remember:...]]`` and
                    # ``[[remember:self:...]]`` tags are Aiko's own
                    # explicit anchors. Long_term so they never decay
                    # through the scratchpad's fast lane.
                    tier="long_term",
                    # Schema v10: persona instructs Aiko to use these
                    # tags for *durable* facts and self-notes (one
                    # short sentence in third / first person). The
                    # batch :class:`MemoryExtractor` is the path that
                    # parses temporal language out of the transcript;
                    # inline tags get the safe default so ``yesterday``
                    # in a tag content stays as content (it's already
                    # rare, and the LLM extractor catches the same
                    # turn anyway).
                    temporal_type="durable",
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
