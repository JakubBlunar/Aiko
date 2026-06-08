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

from app.core.infra.chat_database import ChatDatabase
from app.core.session.session_text_utils import (
    prepare_tts_text,
    resolve_user_name,
    sanitize_assistant_text,
)
from app.core.services.response_text_service import (
    parse_reaction_at_start,
    strip_all_meta_tags,
)
from app.core.session.prompt_assembler import PromptAssembler
from app.llm.ollama_client import OllamaClient

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.core.conversation.conversation_arc import ArcStore


# H1 + K4: cooldown multiplier applied when the current arc is light and
# inviting. ``silly`` / ``playful`` arcs are when proactive nudges land
# best, so we loosen the cooldown gate by 30%; ``support`` arcs are
# already gated by the affect rule, but we tighten further by 50%
# defensively against piling on a vent. The other arcs land on 1.0.
_ARC_COOLDOWN_MULTIPLIER: dict[str, float] = {
    "silly": 0.7,
    "playful": 0.7,
    "support": 1.5,
}
_VENT_DIALOGUE_ACT = "vent"


log = logging.getLogger("app.proactive")


SpeakCallback = Callable[[str, str], None]
"""Signature: ``(prepared_text, reaction)``."""

NotifyMessageCallback = Callable[..., None]
"""Signature: ``(speaker, text)`` -- routes the proactive line into the chat
transcript so the React UI / desktop log show what Aiko said unprompted."""

BoolPredicate = Callable[[], bool]


def _build_proactive_hint(user_display_name: str = "the user") -> str:
    """Voice-mode proactive hint, templated on the user's display name."""
    name = user_display_name or "the user"
    return (
        f"[Aiko speaks first, briefly, because {name} has been quiet for a "
        "moment. Pick up a thread from the recent conversation, or ask a "
        "casual short question to keep the chat going. ONE OR TWO SENTENCES "
        "MAXIMUM. Don't greet, don't restart the conversation. Continue "
        "naturally.]"
    )


def _build_proactive_hint_typed(user_display_name: str = "the user") -> str:
    """Typed-mode proactive hint, templated on the user's display name.

    Frames Aiko's continuation as her own agency rather than commenting on
    the user being quiet — at typed thresholds the latter reads as
    abandonment-anxiety.
    """
    name = user_display_name or "the user"
    return (
        "[Aiko speaks first to continue the thread. Pick ONE of: "
        f"a callback to something {name} said recently, a small thought "
        "you've been turning over, or a brief in-character moment from "
        "your room (something you noticed, made, or fiddled with). ONE "
        "OR TWO SENTENCES. Don't greet, don't restart the chat, don't "
        f"comment on {name} being quiet — just continue naturally as if "
        "you'd been there the whole time.]"
    )


# Back-compat constants for any existing import sites; new code should
# call ``_build_proactive_hint(name)``.
_PROACTIVE_HINT = _build_proactive_hint()
_PROACTIVE_HINT_TYPED = _build_proactive_hint_typed()


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
        # Typed-mode (non-voice) parameters. ``is_typed_eligible`` returns
        # True when the SessionController is willing to accept a typed
        # proactive nudge right now — it folds enabled / presence / not-
        # voice / not-busy into one predicate so this class doesn't have
        # to know about settings or live-mode internals. Cooldown is
        # tracked independently from voice so the two modes can have
        # very different cadences (10 min typed vs 2 min voice default).
        cooldown_seconds_typed: float = 600.0,
        is_typed_eligible: BoolPredicate | None = None,
        user_display_name_provider: Callable[[], str] | None = None,
        arc_store: "ArcStore | None" = None,
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
        self._user_display_name_provider = user_display_name_provider
        # H1 + K4: optional arc store for the eligibility bias. Reads
        # the live arc per ``notify_silence`` call; ``None`` falls back
        # to the legacy unbiased path.
        self._arc_store = arc_store

        self._lock = threading.Lock()
        self._last_run_monotonic = 0.0
        self._inflight = False
        self._prepared_consumed = 0
        self._llm_path_used = 0

        # Typed-mode state. Lives behind the same lock as the voice
        # state so we can never end up running both code paths in
        # parallel against the same DB row.
        self._cooldown_typed = float(cooldown_seconds_typed)
        self._is_typed_eligible = is_typed_eligible
        self._last_typed_run_monotonic = 0.0
        self._typed_inflight = False
        self._typed_prepared_consumed = 0
        self._typed_llm_path_used = 0

    # ── public ────────────────────────────────────────────────────────

    def _last_user_dialogue_act(self, session_key: str) -> str | None:
        """Read the most recent user message's dialogue_act from SQLite.

        Returns ``None`` when the lookup fails or no recent user row
        carries a tag (legacy / pre-K4 sessions). Best-effort: an
        exception here must never abort the proactive path.
        """
        try:
            rows = self._db.get_messages(session_key, limit=4)
        except Exception:
            return None
        for row in reversed(rows):
            if (row.role or "").lower() == "user":
                return row.dialogue_act
        return None

    def _emit_notify(
        self, speaker: str, text: str, message_id: int | None,
    ) -> None:
        """Fan a proactive line out to the injected message listener.

        Passes the persisted ``message_id`` so the client can enable
        reactions on the new bubble (K32). Tolerant of legacy two-arg
        callbacks: an arity ``TypeError`` (raised before the callback
        body runs) falls back to the id-less signature.
        """
        cb = self._notify_message
        if cb is None:
            return
        try:
            try:
                cb(speaker, text, message_id)
            except TypeError:
                cb(speaker, text)
        except Exception:
            log.debug("notify_message raised", exc_info=True)

    def _arc_cooldown_multiplier(self) -> float:
        """Return the cooldown scale based on the current arc.

        ``silly`` / ``playful`` get ``0.7`` (loosened), ``support``
        gets ``1.5`` (tighter), everything else is ``1.0``. Falls back
        to ``1.0`` when no arc store is wired or the lookup fails.
        """
        if self._arc_store is None:
            return 1.0
        try:
            state = self._arc_store.get(self._user_id)
        except Exception:
            return 1.0
        if state is None:
            return 1.0
        return _ARC_COOLDOWN_MULTIPLIER.get(state.arc, 1.0)

    def notify_silence(self, session_key: str) -> None:
        """Possibly speak a proactive line. No-op if guards reject."""
        if not session_key:
            return
        if not self._is_live():
            return
        if self._is_busy():
            log.debug("proactive skip: chat in progress")
            return
        # K4: never pile on a vent. The user is processing something;
        # a proactive nudge here reads as fix-it energy at exactly the
        # wrong beat. The smoother / next user turn will let us back in.
        if self._last_user_dialogue_act(session_key) == _VENT_DIALOGUE_ACT:
            log.debug("proactive skip: last user dialogue_act=vent")
            return
        scale = self._arc_cooldown_multiplier()
        effective_cooldown = self._cooldown * scale
        with self._lock:
            since = time.monotonic() - self._last_run_monotonic
            if since < effective_cooldown:
                log.debug(
                    "proactive skip: cooldown %.1fs/%.1fs (arc_scale=%.2f)",
                    since,
                    effective_cooldown,
                    scale,
                )
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

    def notify_typed_silence(self, session_key: str) -> None:
        """Possibly speak a proactive line in TYPED mode.

        Mirrors :meth:`notify_silence` but is gated by
        ``is_typed_eligible`` (which the owner builds from
        enabled-flag + presence + not-voice + not-busy + ...) instead
        of ``is_live_mode``, and uses an independent cooldown clock so
        a recent voice-mode ping doesn't muzzle the typed one and vice
        versa. Voice mode dominance is the responsibility of the
        eligibility predicate — this method does not consult
        ``is_live_mode`` directly.
        """
        if not session_key:
            return
        eligible = self._is_typed_eligible
        if eligible is None or not eligible():
            return
        if self._is_busy():
            log.debug("proactive(typed) skip: chat in progress")
            return
        if self._last_user_dialogue_act(session_key) == _VENT_DIALOGUE_ACT:
            log.debug("proactive(typed) skip: last user dialogue_act=vent")
            return
        scale = self._arc_cooldown_multiplier()
        effective_cooldown = self._cooldown_typed * scale
        with self._lock:
            since = time.monotonic() - self._last_typed_run_monotonic
            if since < effective_cooldown:
                log.debug(
                    "proactive(typed) skip: cooldown %.1fs/%.1fs (arc_scale=%.2f)",
                    since,
                    effective_cooldown,
                    scale,
                )
                return
            if self._typed_inflight:
                log.debug("proactive(typed) skip: already running")
                return
            self._typed_inflight = True
        threading.Thread(
            target=self._run_typed_safe,
            args=(session_key,),
            daemon=True,
            name="proactive-director-typed",
        ).start()

    def notify_task_escalation(self, session_key: str) -> None:
        """Brain-orchestration chunk 6: speak a parked task cue.

        Fires when :class:`TaskEscalationManager`'s timer elapses and
        the loop's gate cleared — i.e. the user has gone quiet long
        enough that the parked ``task_result`` or
        ``task_input_needed`` cue should surface as a proactive turn
        rather than wait for a natural reply.

        Differences from :meth:`notify_silence` / :meth:`notify_typed_silence`:

        * **No cooldown gate.** The escalation manager already
          enforces a per-cue silence window (45 s / 20 s) plus a
          retry-with-backoff loop. The proactive cooldown is for
          *boredom* nudges; a finished task is event-driven, not
          time-driven, and would silently drop on a recent voice
          ping otherwise.
        * **No vent-detection skip.** The user explicitly asked for
          the task earlier; surfacing the result is a follow-up to
          their request, not a fix-it impulse on a fresh vent.
        * **Picks voice vs typed by live-mode automatically.** The
          escalation manager doesn't know whether the user is in
          live voice or typed mode at fire time — that ownership
          stays here.

        Inflight gates from both modes are still consulted so we
        never stack a task-escalation turn on top of a regular
        proactive one (or vice versa). When both are inflight, the
        call returns silently — the escalation manager will retry.

        The parked cue itself lands in the proactive turn's prompt
        via the existing :class:`TaskCueStore` T6 provider on
        :class:`PromptAssembler` (drained on assembly, which also
        cancels the matching escalation timer). This method only
        needs to *trigger* the turn; it doesn't need to thread the
        cue text through.
        """
        if not session_key:
            return
        if self._is_busy():
            log.debug("proactive(task_escalation) skip: chat in progress")
            return
        live = False
        try:
            live = bool(self._is_live())
        except Exception:
            log.debug(
                "proactive(task_escalation) is_live probe failed",
                exc_info=True,
            )
        with self._lock:
            if self._inflight or self._typed_inflight:
                log.debug(
                    "proactive(task_escalation) skip: already running "
                    "(voice=%s typed=%s)",
                    self._inflight,
                    self._typed_inflight,
                )
                return
            if live:
                self._inflight = True
            else:
                self._typed_inflight = True
        target = self._run_safe if live else self._run_typed_safe
        thread_name = (
            "proactive-director-task"
            if live
            else "proactive-director-task-typed"
        )
        log.info(
            "proactive(task_escalation) dispatched: mode=%s session=%s",
            "voice" if live else "typed",
            session_key,
        )
        threading.Thread(
            target=target,
            args=(session_key,),
            daemon=True,
            name=thread_name,
        ).start()

    def update_runtime(
        self,
        *,
        client: Any | None = None,
        model: str | None = None,
        cooldown_seconds: float | None = None,
        cooldown_seconds_typed: float | None = None,
        context_window: int | None = None,
    ) -> None:
        # ``client`` lets ``SessionController.reconfigure_chat_llm``
        # rebind the proactive director's chat client without
        # rebuilding the whole instance. New nudge tasks see the new
        # client; in-flight ones keep their original reference.
        if client is not None:
            self._ollama = client
        if model is not None:
            self._model = model
        if cooldown_seconds is not None:
            self._cooldown = float(cooldown_seconds)
        if cooldown_seconds_typed is not None:
            self._cooldown_typed = float(cooldown_seconds_typed)
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
            _build_proactive_hint(
                resolve_user_name(self._user_display_name_provider),
            ),
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
                surface="proactive_silence",
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
        message_id = self._db.add_message(
            session_id=session_key,
            role="assistant",
            content=cleaned,
            token_count=usage.completion_tokens,
        )
        # Surface the line in the chat transcript using a distinguishable
        # speaker so the React UI can render it differently if it wants.
        # Carry the persisted id so the client can enable reactions (K32).
        self._emit_notify("Assistant (proactive)", cleaned, message_id)
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
        message_id: int | None = None
        try:
            message_id = self._db.add_message(
                session_id=session_key,
                role="assistant",
                content=cleaned,
                token_count=0,
            )
        except Exception:
            log.debug("prepared nudge persist failed", exc_info=True)
        self._emit_notify("Assistant (proactive)", cleaned, message_id)
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

    # ── typed-mode runners ───────────────────────────────────────────────

    def _run_typed_safe(self, session_key: str) -> None:
        try:
            self._run_typed(session_key)
        except Exception as exc:
            log.warning("proactive(typed) run failed: %s", exc)
        finally:
            with self._lock:
                self._typed_inflight = False
                self._last_typed_run_monotonic = time.monotonic()

    def _run_typed(self, session_key: str) -> None:
        if self._db.get_message_count(session_key) <= 0:
            log.debug("proactive(typed) skip: no history yet")
            return

        # Same prepared-nudge fast path as voice mode: prefer a fresh
        # callback / open-question / promise / agenda woven by the
        # NarrativeWeaver. Falls back to the LLM hint below when nothing
        # is queued.
        prepared = self._consume_prepared_nudge()
        if prepared is not None and self._speak_prepared_typed(session_key, prepared):
            return

        messages = self._prompt.build(
            session_key,
            _build_proactive_hint_typed(
                resolve_user_name(self._user_display_name_provider),
            ),
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
                surface="proactive_typed",
            )
        except Exception as exc:
            log.info("proactive(typed) call failed: %s", exc)
            return

        # Re-check eligibility before speaking — the user may have
        # started typing, alt-tabbed away, or flipped to voice mode
        # while the LLM call was in flight.
        eligible = self._is_typed_eligible
        if self._is_busy() or eligible is None or not eligible():
            log.debug("proactive(typed): discarding (state changed mid-call)")
            return

        _mood, body = parse_reaction_at_start(content or "")
        body = strip_all_meta_tags(body)
        cleaned = sanitize_assistant_text(body)
        if not cleaned:
            log.debug("proactive(typed): empty output")
            return

        message_id = self._db.add_message(
            session_id=session_key,
            role="assistant",
            content=cleaned,
            token_count=usage.completion_tokens,
        )
        self._emit_notify("Assistant (proactive)", cleaned, message_id)
        # Typed mode is text-only by design: the assumption is the
        # user is reading, not listening, so auto-speaking a 4-min-
        # later "pick up the thread" line just to fill the room would
        # surprise them. TTS toggle for typed proactive is on the
        # backlog as a follow-up.
        self._typed_llm_path_used += 1
        log.info(
            "proactive(typed) wrote %d chars (%d/%d tokens, %.0f ms)",
            len(cleaned),
            usage.prompt_tokens,
            usage.completion_tokens,
            (time.monotonic() - t0) * 1000.0,
        )

    def _speak_prepared_typed(self, session_key: str, nudge: object) -> bool:
        """Persist a prepared nudge as a typed-mode proactive turn.

        Mirrors :meth:`_speak_prepared` but skips the TTS enqueue —
        typed proactive is text-only by design.
        """
        text = getattr(nudge, "text", "")
        if not text:
            return False
        eligible = self._is_typed_eligible
        if self._is_busy() or eligible is None or not eligible():
            log.debug("proactive(typed) prepared: discarding (state changed)")
            return False
        cleaned = sanitize_assistant_text(text)
        if not cleaned:
            return False
        message_id: int | None = None
        try:
            message_id = self._db.add_message(
                session_id=session_key,
                role="assistant",
                content=cleaned,
                token_count=0,
            )
        except Exception:
            log.debug("prepared nudge persist failed", exc_info=True)
        self._emit_notify("Assistant (proactive)", cleaned, message_id)
        self._typed_prepared_consumed += 1
        log.info(
            "proactive(typed) wrote prepared nudge (kind=%s, %d chars)",
            getattr(nudge, "source_kind", "?"),
            len(cleaned),
        )
        return True

    def stats(self) -> dict[str, int]:
        return {
            "prepared_consumed": int(self._prepared_consumed),
            "llm_path_used": int(self._llm_path_used),
            "typed_prepared_consumed": int(self._typed_prepared_consumed),
            "typed_llm_path_used": int(self._typed_llm_path_used),
        }
