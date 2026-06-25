"""Session lifecycle mixin.

Extracted from :mod:`app.core.session.session_controller`. Owns the
identity / display-name surface, session switch/clear/new, the model +
context-window getters, remember-history + session-type flags, the
scheduler / RAG accessors, the voice-merge helpers, and the
assistant-preference / idle-gate / shutdown lifecycle. State ownership
stays on ``SessionController.__init__``.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for any moved method must patch
``app.core.session.lifecycle_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
from typing import Any
from app.core.infra.settings import AppSettings
from collections.abc import Callable
from app.core.session.session_state import SessionState
from app.core.voice.speaking_window_scheduler import SpeakingWindowScheduler
from app.core.infra.crash_logging import log_event
from app.core.infra.settings import persist_user_overrides
from app.core.infra.settings import read_user_overrides
import threading
import time
import uuid


log = logging.getLogger("app.session")


class LifecycleMixin:
    """Identity, session switch/clear, model getters, accessors, shutdown."""

    @property
    def state(self) -> SessionState:
        return self._state

    def update_sources(self, *, mic: bool) -> None:
        self._state.mic_enabled = bool(mic)

    @property
    def session_key(self) -> str:
        return f"{self._user_id}:{self._session_id}" if self._user_id else self._session_id

    @property
    def user_display_name(self) -> str:
        """Configured user display name (or ``"friend"`` fallback).

        Single read site for every renderer, transcript formatter, and
        worker LLM prompt. Refreshes implicitly on next read after the
        identity is updated via ``update_user_display_name``.
        """
        from app.core.infra.settings import resolve_user_display_name
        return resolve_user_display_name(self._settings)

    @property
    def needs_onboarding(self) -> bool:
        """True when no display name has been configured yet."""
        from app.core.infra.settings import is_onboarding_needed
        return is_onboarding_needed(self._settings)

    def update_user_display_name(self, name: str) -> str:
        """Persist the user display name to ``config/user.json``.

        Validated to 1-32 chars after strip. Empty input is rejected
        (the caller -- REST handler -- returns 400). Returns the
        normalized stored value. Broadcasts ``identity_changed`` so the
        UI and any registered listeners see the new name without a
        reload.
        """
        cleaned = (name or "").strip()[:32]
        if not cleaned:
            raise ValueError("user_display_name must be non-empty after trim")
        self._settings.assistant.user_display_name = cleaned
        try:
            persist_user_overrides({"assistant": {"user_display_name": cleaned}})
        except Exception:
            log.warning(
                "failed to persist user_display_name to user.json",
                exc_info=True,
            )
        for listener in list(getattr(self, "_identity_listeners", []) or []):
            try:
                listener(cleaned)
            except Exception:
                log.debug("identity listener raised", exc_info=True)
        return cleaned

    def _seed_onboarding_goal_if_first_time(
        self, *, force: bool = False,
    ):
        """K1 follow-up: seed the curated "get to know {user_name}" goal.

        Idempotent via the ``goals.onboarding_goal_seeded`` row in
        ``kv_meta`` — the second call (and every call after) is a
        no-op unless ``force=True``. Gated additionally on
        ``not needs_onboarding`` so a user who hasn't typed their
        name yet doesn't get a goal that says "Get to know friend";
        the identity-listener path will fire it the moment they do.

        Called from two places:

        - ``SessionController.__init__`` (backfill for existing
          users coming back after the feature ships).
        - The identity listener registered against
          ``update_user_display_name`` — fires automatically on
          first name set.

        Defensive: returns ``None`` on any failure, never raises.
        Logged via :mod:`app.onboarding_goal` so the call is
        traceable end-to-end without a fresh logger here.
        """
        if not force and self.needs_onboarding:
            log.debug(
                "onboarding-goal: needs_onboarding=True; deferring seed",
            )
            return None
        if self._goal_store is None or self._memory_store is None:
            log.debug(
                "onboarding-goal: stores not initialised; deferring seed",
            )
            return None
        try:
            from app.core.goals.onboarding_goal import seed_onboarding_goal

            return seed_onboarding_goal(
                goal_store=self._goal_store,
                memory_store=self._memory_store,
                chat_db=self._chat_db,
                user_display_name=self.user_display_name,
                force=force,
            )
        except Exception:
            log.warning("onboarding-goal seed raised", exc_info=True)
            return None

    def add_identity_listener(self, callback: Callable[[str], None]) -> None:
        """Register a callback fired after ``update_user_display_name``.

        Workers / renderers that cache the name in pre-built prompt
        strings subscribe here to invalidate or rebuild on rename.
        """
        listeners = getattr(self, "_identity_listeners", None)
        if listeners is None:
            listeners = []
            self._identity_listeners = listeners
        if callback and callback not in listeners:
            listeners.append(callback)

    def switch_session(self, session_id: str) -> None:
        # Drop any pending voice merge buffer; the new session starts
        # without an in-flight phrase A waiting for a continuation.
        self._clear_merge_buffer()
        with self._vocal_tone_lock:
            self._last_vocal_tone = None
        normalized = (session_id or "").strip()
        if not normalized:
            return
        self._session_id = normalized
        # K29 — reset the per-session opinion-injection count so the
        # cap applies to the new conversation, not the previous one.
        # Cooldown survives so a fresh switch doesn't accidentally
        # re-fire on the same beat that the prior session ended on.
        self._opinion_injection_session_count = 0
        # P21 — drop any deferred borderline verdict / pending cue so the
        # new conversation doesn't inherit a contradiction beat from the
        # prior one.
        self._opinion_injection_pending_borderline = None
        self._opinion_injection_pending_cue = None
        # K28 — wipe any stashed turning-over slot so the new session
        # doesn't inherit a "this is a comeback" cue from the prior
        # one. The force-next bypass and last-fire diagnostic also
        # clear so MCP debug state matches the visible session.
        self._pending_turning_over_seconds = None
        self._turning_over_force_next = False
        self._last_turning_over = None
        # K36 — wipe the away-activities slot on session switch too.
        self._pending_away_activities_seconds = None
        self._away_activities_force_next = False
        # K34 — wipe the forward-curiosity slot on session switch too.
        self._pending_forward_curiosity_seconds = None
        self._forward_curiosity_force_next = False
        # Follow-up cue: clear the MCP force-next flag on session switch
        # (the cue ring + watermark live in kv_meta, not per-session).
        self._follow_up_force_next = False
        # K38 — wipe the self-correction slot + cooldown on switch.
        self._pending_self_correction = None
        self._self_correction_cooldown_remaining = 0
        # K53 — fresh initiative counter per session (warmup applies
        # again so a new session never opens with a floor-grab).
        self._initiative_director = None
        # K55 — an opened thread doesn't survive a session switch.
        self._owned_thread = None
        self._pending_thread_open = None
        # K54 — the once-per-conversation appetite slip re-arms.
        self._topic_appetite_fired = False
        # K57 — staged (unapplied) triggers don't cross sessions;
        # live episodes intentionally DO (they're kv-backed feelings
        # with wall-clock decay, not per-session state).
        self._pending_emotion_triggers = []
        # K60 — the one-shot slip bypass doesn't cross sessions.
        self._mask_force_slip_next = False
        # Best-effort: a write failure (read-only volume, locked file)
        # must not break the in-memory switch — the user just lands
        # back on whatever was previously persisted on next launch.
        try:
            persist_user_overrides({"session": {"last_active_id": normalized}})
        except Exception:
            log.debug("failed to persist last_active_id", exc_info=True)

    def new_session(self) -> str:
        new_id = str(uuid.uuid4())[:8]
        self.switch_session(new_id)
        return new_id

    def _resolve_initial_session_id(self, *, default: str = "main") -> str:
        """Pick the session id to land on at startup.

        Priority (first match wins):

        1. ``user.json``'s ``session.last_active_id`` if it's a non-empty
           string. Honoured even when the underlying session has no
           messages yet — this lets a "New session" → tab-close →
           reopen sequence keep the user on their fresh empty session.
        2. The most recently active session in the chat DB. Saves users
           who never had a persisted preference (first-run, downgrade
           from a build without persistence) from the cold "main"
           default if they've already chatted before.
        3. ``default`` (``"main"``).

        Pure read — no writes — so failures here just fall through.
        """
        try:
            saved = (
                read_user_overrides()
                .get("session", {})
                .get("last_active_id", "")
            )
            if isinstance(saved, str) and saved.strip():
                return saved.strip()
        except Exception:
            log.debug("read_user_overrides failed during startup", exc_info=True)
        try:
            rows = self._chat_db.list_sessions()
            if rows:
                most_recent = rows[0].get("session_id", "")
                # ``list_sessions`` returns the full ``user_id:session_id``
                # composite key; strip the user prefix so the value is
                # consistent with what ``_session_id`` stores everywhere
                # else (the session_key property re-prepends it).
                if isinstance(most_recent, str) and ":" in most_recent:
                    most_recent = most_recent.split(":", 1)[1]
                if most_recent.strip():
                    return most_recent.strip()
        except Exception:
            log.debug("list_sessions failed during startup", exc_info=True)
        return default

    def clear_conversation_memory(self) -> None:
        self._clear_merge_buffer()
        self._chat_db.clear_messages(self.session_key, full_reset=True)
        # K29 — wiping the conversation also resets per-session
        # counters; the cap is about *this conversation*, not the
        # process lifetime.
        self._opinion_injection_session_count = 0
        self._opinion_injection_cooldown = 0
        self._opinion_injection_force_next = False
        self._last_opinion_injection = None
        self._opinion_injection_pending_borderline = None
        self._opinion_injection_pending_cue = None
        # K28 — same logic: a full clear should leave no stashed
        # turning-over slot or force-next bypass.
        self._pending_turning_over_seconds = None
        self._turning_over_force_next = False
        self._last_turning_over = None
        # K36 — clear the away-activities slot on a full history wipe.
        self._pending_away_activities_seconds = None
        self._away_activities_force_next = False
        # K34 — clear the forward-curiosity slot on a full history wipe.
        self._pending_forward_curiosity_seconds = None
        self._forward_curiosity_force_next = False
        # Follow-up cue: clear the MCP force-next flag on a full wipe.
        self._follow_up_force_next = False
        # K38 — clear the self-correction slot + cooldown on a wipe.
        self._pending_self_correction = None
        self._self_correction_cooldown_remaining = 0
        # K53 — a full wipe restarts the initiative cadence + warmup.
        self._initiative_director = None
        # K55 — drop any opened thread with the history it lived in.
        self._owned_thread = None
        self._pending_thread_open = None
        # K54 — a wiped history re-arms the appetite slip.
        self._topic_appetite_fired = False
        # K57 — staged triggers die with the history (live episodes
        # persist in kv_meta by design).
        self._pending_emotion_triggers = []
        # K60 — the one-shot slip bypass dies with the history too.
        self._mask_force_slip_next = False

    def _clear_merge_buffer(self, session_key: str | None = None) -> None:
        """Drop the voice merge buffer (one specific session, or all).

        Called on session change, on full clear, on shutdown, and
        whenever the merge window naturally closes (TTS-start, merge
        branch consumed it, barge-in flow took over).
        """
        with self._merge_lock:
            if session_key is None:
                self._merge_buffer.clear()
            else:
                self._merge_buffer.pop(session_key, None)

    def _wrap_tts_chunk_for_merge(
        self,
        inner: Callable[[str, str], None] | None,
        merge_key: str,
    ) -> Callable[[str, str], None]:
        """Return a TTS-chunk callback that closes the merge window on
        the first invocation and then forwards every chunk to ``inner``.

        Once the first audio chunk is enqueued the user has crossed the
        "Aiko is now speaking" boundary; any subsequent partial speech
        falls back to the existing barge-in flow rather than the merge
        flow. Setting ``tts_started=True`` makes ``feed_stt_partial`` skip
        the early-abort path even if the buffer is still in the dict.
        """
        first_chunk_seen = False

        def _wrapped(prepared_text: str, reaction: str) -> None:
            nonlocal first_chunk_seen
            if not first_chunk_seen:
                first_chunk_seen = True
                with self._merge_lock:
                    buf = self._merge_buffer.get(merge_key)
                    if buf is not None:
                        buf.tts_started = True
                # Once TTS has started the merge window is closed; drop
                # the buffer so we don't keep a reference to a runner
                # whose stream is past the abort-friendly point.
                self._clear_merge_buffer(merge_key)
            if inner is not None:
                inner(prepared_text, reaction)

        return _wrapped

    @property
    def chat_model(self) -> str:
        return self._settings.ollama.chat_model

    @property
    def effective_chat_model(self) -> str:
        return self._effective_chat_model

    @property
    def context_window_size(self) -> int:
        return self._context_window

    @property
    def context_window_source(self) -> str:
        """Where ``context_window`` came from: ``config|client|fallback``.

        ``config`` means an explicit ``chat_llm.context_window`` (or
        legacy ``ollama.context_window``) override won. ``client``
        means the active ``ChatClient`` answered ``get_context_length``
        with a positive value — either Ollama's ``/api/show`` for
        local models or the static OpenAI-compat lookup table for
        known cloud models. ``fallback`` is the hardcoded 8192
        last-resort when neither path produced an answer.
        """
        return getattr(self, "_context_source", "fallback")

    @property
    def context_tokens_used(self) -> int:
        try:
            metrics = self._last_metrics
            return int(metrics.get("prompt_tokens", 0) or 0)
        except Exception:
            return 0

    @property
    def remember_history(self) -> bool:
        return self._remember_history

    def set_remember_history(self, value: bool) -> None:
        self._remember_history = bool(value)

    @property
    def active_session_type(self) -> str:
        return "chat"

    @property
    def scheduler(self) -> SpeakingWindowScheduler:
        return self._scheduler

    def notify_user_speech_started(self) -> None:
        """Called by LiveSession when fresh user audio lands mid-window.

        Background workers cooperatively cancel so the LLM channel is free
        for the actual reply.
        """
        try:
            self._scheduler.on_user_speech()
        except Exception:
            log.debug("scheduler.on_user_speech failed", exc_info=True)

    @property
    def rag_store(self):
        return getattr(self, "_rag_store", None)

    @property
    def document_ingestor(self):
        return getattr(self, "_document_ingestor", None)

    def get_conversation_memory(self, max_entries: int = 200) -> list[dict[str, str]]:
        rows = self._chat_db.get_messages(self.session_key, limit=max_entries)
        return [
            {"role": r.role, "content": r.content, "created_at": r.created_at}
            for r in rows
        ]

    def _apply_assistant_preferences(self) -> None:
        length_scale = getattr(self._settings.assistant, "tts_length_scale", 1.0) or 1.0
        set_length = getattr(self._tts_engine, "set_length_scale", None)
        if callable(set_length):
            try:
                set_length(length_scale)
            except Exception:
                log.debug("tts engine rejected length scale", exc_info=True)
        # Layer 1c gate: opt-in per-reaction temperature deltas.
        # Default OFF -- Pocket-TTS is sensitive enough to temperature
        # excursions that even small per-reaction deltas can introduce
        # pitch / timbre artefacts on the active voice. The user
        # opts in via ``agent.tts_runtime_temp_enabled`` once a
        # voice has been validated.
        runtime_temp_enabled = bool(
            getattr(self._settings.agent, "tts_runtime_temp_enabled", False),
        )
        set_runtime_temp = getattr(
            self._tts_engine, "set_runtime_temp_enabled", None,
        )
        if callable(set_runtime_temp):
            try:
                set_runtime_temp(runtime_temp_enabled)
            except Exception:
                log.debug(
                    "tts engine rejected runtime temp toggle",
                    exc_info=True,
                )
        # Layer 5 gate: opt-in per-reaction speed jitter.
        # Default OFF -- Pocket-TTS scales playback ``sample_rate`` to
        # change speed, which couples speed and pitch. With per-
        # reaction sub-caps active, that pitch couples to the affect
        # channel and the user perceives "her voice keeps changing"
        # between sentences. The user opts in via
        # ``agent.tts_runtime_speed_enabled`` once a voice has been
        # validated.
        runtime_speed_enabled = bool(
            getattr(
                self._settings.agent, "tts_runtime_speed_enabled", False,
            ),
        )
        set_runtime_speed = getattr(
            self._tts_engine, "set_runtime_speed_enabled", None,
        )
        if callable(set_runtime_speed):
            try:
                set_runtime_speed(runtime_speed_enabled)
            except Exception:
                log.debug(
                    "tts engine rejected runtime speed toggle",
                    exc_info=True,
                )

    def _trace(self, stage: str, message: str) -> None:
        from datetime import datetime, timezone
        self._decision_trace.append({
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "stage": stage,
            "message": message,
        })
        if "error" in stage.lower():
            try:
                log_event(stage, message)
            except Exception:
                pass

    @staticmethod
    def _build_tts_service(settings: AppSettings) -> Any:
        # Lean v1 ships only pocket-tts (matches the active user.json config).
        # Playback now flows through ``set_pcm_listener`` -> WS hub
        # -> connected clients; the engine no longer holds a device handle.
        from app.tts.pocket_tts_service import PocketTtsService
        return PocketTtsService(settings.tts)

    def _touch_user_activity(self) -> None:
        """Mark "the user just did something". Resets the idle gate.

        Called from the turn lifecycle and from incoming WS / REST
        traffic. The :class:`IdleWorkerScheduler` consults
        :meth:`_is_user_idle` before running a worker; a recent touch
        defers background work so it doesn't compete with the active
        conversation.
        """
        self._last_user_activity_at = time.monotonic()

    def _is_user_idle(self) -> bool:
        """Return True when it's safe to run a background worker.

        Three rules:
          * Live mode (voice) is **always** considered busy. The
            speaking window already runs the speaking-window scheduler;
            stacking idle workers on top would compete for CPU.
          * A turn currently in progress -> not idle.
          * Less than ``idle_worker_quiet_threshold_seconds`` since the
            last user activity -> not idle.
        """
        try:
            if getattr(self, "_live_mode_enabled", False):
                return False
            if getattr(self, "_turn_in_progress", False):
                return False
        except Exception:
            return True
        threshold = float(
            self._memory_settings.idle_worker_quiet_threshold_seconds
        )
        elapsed = time.monotonic() - float(
            getattr(self, "_last_user_activity_at", 0.0) or 0.0
        )
        return elapsed >= threshold

    def shutdown(self) -> None:
        # Clear the voice merge buffer first so a tail-end partial that
        # races shutdown can't try to call ``request_stop()`` on a
        # half-torn-down ``TurnRunner``.
        try:
            self._clear_merge_buffer()
        except Exception:
            log.debug("merge buffer clear on shutdown failed", exc_info=True)
        try:
            self._disarm_typed_silence_timer()
        except Exception:
            log.debug("typed silence timer cancel on shutdown failed", exc_info=True)
        # Brain orchestration first: stop the loop + escalation timers
        # before downstream components disappear. The mixin is
        # exception-safe internally; the outer guard is just for the
        # case where ``_init_task_orchestration`` raised partway
        # through and left the mixin in a half-built state.
        try:
            self._shutdown_task_orchestration()
        except Exception:
            log.debug(
                "task-orchestration shutdown failed", exc_info=True
            )
        if self._mcp_server_runner is not None:
            try:
                self._mcp_server_runner.stop()
            except Exception:
                log.debug("mcp stop failed", exc_info=True)
        try:
            self._scheduler.stop()
        except Exception:
            log.debug("scheduler stop failed", exc_info=True)
        if getattr(self, "_rag_prefetcher", None) is not None:
            try:
                self._rag_prefetcher.shutdown()
            except Exception:
                log.debug("rag prefetcher shutdown failed", exc_info=True)
        if getattr(self, "_rag_retriever", None) is not None:
            try:
                self._rag_retriever.close()
            except Exception:
                log.debug("rag retriever close failed", exc_info=True)
        if getattr(self, "_listening_window_executor", None) is not None:
            try:
                self._listening_window_executor.shutdown(
                    wait=False, cancel_futures=True,
                )
            except Exception:
                log.debug("listening window executor shutdown failed", exc_info=True)
        try:
            self._tts.stop()
        except Exception:
            pass
        if getattr(self, "_client_cache", None) is not None:
            try:
                self._client_cache.shutdown()
            except Exception:
                log.debug("client cache shutdown failed", exc_info=True)
        if getattr(self, "_idle_scheduler", None) is not None:
            try:
                self._idle_scheduler.stop(timeout=1.5)
            except Exception:
                log.debug("idle worker scheduler stop failed", exc_info=True)
        if getattr(self, "_message_indexer", None) is not None:
            try:
                self._message_indexer.stop()
            except Exception:
                log.debug("message indexer stop failed", exc_info=True)
        try:
            self._summary_worker.stop()
        except Exception:
            pass
        if self._memory_store is not None:
            try:
                self._memory_store.close()
            except Exception:
                log.debug("memory store close failed", exc_info=True)
        if self._embedder is not None:
            try:
                self._embedder.close()
            except Exception:
                log.debug("embedder close failed", exc_info=True)
        try:
            t = threading.Thread(target=self._realtime_stt.stop_context, daemon=True)
            t.start()
            t.join(timeout=2.0)
        except Exception:
            pass
