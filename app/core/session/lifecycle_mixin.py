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
from datetime import datetime
from typing import Any
from app.core.infra import timephrase
from app.core.infra.settings import AppSettings
from collections.abc import Callable
from app.core.session.debug_overrides import DebugOverridesHostMixin
from app.core.session.session_state import SessionState
from app.core.voice.speaking_window_scheduler import SpeakingWindowScheduler
from app.core.infra.crash_logging import log_event
from app.core.infra.settings import persist_user_overrides
from app.core.infra.settings import read_user_overrides
import threading
import time
import uuid


log = logging.getLogger("app.session")

# Wall-clock mirror of ``_last_user_activity_at``. Persisted because the
# monotonic clock restarts with the process, and idle *depth* (P36) has
# to survive that to be worth anything.
_KV_LAST_USER_ACTIVITY = "idle.last_user_activity_at"


class LifecycleMixin(DebugOverridesHostMixin):
    """Identity, session switch/clear, model getters, accessors, shutdown."""

    #: Last value written to ``session.last_active_id`` in ``user.json``.
    #: Declared on the class so ``_touch_last_active_session`` is safe on a
    #: partially-constructed controller; ``__init__`` re-sets it per
    #: instance.
    _persisted_last_active_id: str = ""

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
        self._opinion_injection_cue_emitted = False
        # K46 — drop any warm-stance window so the new conversation
        # doesn't inherit a "hold your take" beat from the prior one.
        self._stance_recent_window = 0
        self._stance_recent_text = ""
        # K63 — reset the per-session callback cap. The wall-clock cooldown
        # + don't-repeat ring stay in kv_meta on purpose (a long-arc
        # callback should remain rare across a session switch, not reset).
        self._long_arc_callback_session_count = 0
        # Every armed MCP debug override, so one that never fired can't go
        # off in the session the user just switched to. This used to be a
        # hand-written list that covered 11 of the 43 flags.
        self._debug_overrides.clear()
        # K28 — wipe any stashed turning-over slot so the new session
        # doesn't inherit a "this is a comeback" cue from the prior one.
        self._pending_turning_over_seconds = None
        self._last_turning_over = None
        # K36 — wipe the away-activities slot on session switch too.
        self._pending_away_activities_seconds = None
        # K34 — wipe the forward-curiosity slot on session switch too.
        self._pending_forward_curiosity_seconds = None
        # L30b — same, for the hypothesis-ask gap slot.
        self._pending_concept_hypothesis_seconds = None
        # K75 user-expertise: reset the provider cooldown.
        self._user_expertise_cooldown = 0
        self._user_expertise_last = None
        # K38 — reset the self-correction cooldown on switch. The cue
        # itself lives in cue_pool and is not session-scoped: an owed
        # correction is owed whichever session she is in, and its own
        # half-hour TTL retires it.
        self._self_correction_cooldown_remaining = 0
        # F13 — drop any un-drained user-correction candidates on switch.
        # The corrected note belongs to a memory the previous session
        # surfaced; carrying the candidate across would confirm it against
        # the wrong context.
        if hasattr(self, "_pending_correction_candidates"):
            self._pending_correction_candidates.clear()
        # K53 — fresh initiative counter per session (warmup applies
        # again so a new session never opens with a floor-grab).
        self._initiative_director = None
        # K55 — an opened thread doesn't survive a session switch.
        self._owned_thread = None
        self._pending_thread_open = None
        # K52 — an unresolved imperative charge doesn't cross sessions.
        self._pending_want_imperative = None
        # K92 — the brevity brake measures a run of long replies, and a
        # session switch breaks the run. The arc age resets too: a new
        # session's opening beat has earned the protected-arc veto again.
        self._recent_reply_words = ()
        self._arc_age_turns = 0
        self._last_stance_decision = None
        # K54 — the once-per-conversation appetite slip re-arms.
        self._topic_appetite_fired = False
        # K81 — the once-per-conversation taste-lean slip re-arms.
        self._taste_lean_fired = False
        self._conduct_notice_fired = False
        # L17e — the belief-revision slip re-arms per conversation (the
        # long global cooldown in kv_meta is what actually keeps it rare).
        self._learning_reflection_fired = False
        # K57 — staged (unapplied) triggers don't cross sessions;
        # live episodes intentionally DO (they're kv-backed feelings
        # with wall-clock decay, not per-session state).
        self._pending_emotion_triggers = []
        # Best-effort: a write failure (read-only volume, locked file)
        # must not break the in-memory switch — the user just lands
        # back on whatever was previously persisted on next launch.
        try:
            persist_user_overrides({"session": {"last_active_id": normalized}})
            self._persisted_last_active_id = normalized
        except Exception:
            log.debug("failed to persist last_active_id", exc_info=True)

    def _touch_last_active_session(self) -> None:
        """Record the session the user is *actually* talking in.

        :meth:`switch_session` persists the pointer when the user picks a
        conversation, which records **intent**, not activity. Anything
        that lands on a session without a click — the startup fallback
        chain, a write that failed and logged at debug, a session created
        by another surface — leaves the pointer naming a conversation the
        user has since moved on from, and
        :meth:`_resolve_initial_session_id` honours that pointer over the
        database's own record of where the last message actually went. It
        was found a full day stale in the wild: the pointer named a
        session last used on the 10th while 75 messages had since landed
        in another one, so every restart re-opened the older thread.

        Called once per user turn and guarded by an in-memory copy, so it
        costs one small write per session rather than one per message.
        The in-memory copy starts empty on purpose: the first turn after a
        cold start always writes, which is what repairs a pointer that
        drifted while a previous build was running.
        """
        session_id = (self._session_id or "").strip()
        if not session_id or session_id == self._persisted_last_active_id:
            return
        try:
            persist_user_overrides({"session": {"last_active_id": session_id}})
            self._persisted_last_active_id = session_id
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
        # Every armed MCP debug override. The hand-written list this replaces
        # covered 14 of the 43 flags, and disagreed with the switch_session
        # list about three more.
        self._debug_overrides.clear()
        self._opinion_injection_session_count = 0
        self._opinion_injection_cooldown = 0
        self._last_opinion_injection = None
        self._opinion_injection_pending_borderline = None
        self._opinion_injection_pending_cue = None
        self._opinion_injection_cue_emitted = False
        # K46 — clear the warm-stance window + diagnostics on a full wipe.
        self._stance_recent_window = 0
        self._stance_recent_text = ""
        self._last_stance_persistence = None
        # K63 — a full memory wipe resets everything, including the
        # don't-repeat ring (the memories it references are gone).
        # Best-effort kv clear so a stale id can't suppress a fresh
        # callback after the user nukes their history.
        self._long_arc_callback_session_count = 0
        self._last_long_arc_callback = None
        try:
            from app.core.conversation import long_arc_callback as _lac

            self._chat_db.kv_set(_lac.KV_RECENT_IDS, "[]")
        except Exception:
            pass
        # K28 — same logic: a full clear should leave no stashed
        # turning-over slot.
        self._pending_turning_over_seconds = None
        self._last_turning_over = None
        # K36 — clear the away-activities slot on a full history wipe.
        self._pending_away_activities_seconds = None
        # K34 — clear the forward-curiosity slot on a full history wipe.
        self._pending_forward_curiosity_seconds = None
        # L30b — same, for the hypothesis-ask gap slot.
        self._pending_concept_hypothesis_seconds = None
        # K75 user-expertise: reset the provider cooldown.
        self._user_expertise_cooldown = 0
        self._user_expertise_last = None
        # K38 — clear the self-correction cooldown on a wipe.
        self._self_correction_cooldown_remaining = 0
        # F13 — drop any un-drained user-correction candidates on a wipe.
        if hasattr(self, "_pending_correction_candidates"):
            self._pending_correction_candidates.clear()
        # K53 — a full wipe restarts the initiative cadence + warmup.
        self._initiative_director = None
        # K55 — drop any opened thread with the history it lived in.
        self._owned_thread = None
        self._pending_thread_open = None
        # K52 — an unresolved imperative charge goes with the history.
        self._pending_want_imperative = None
        # K92 — the reply-length run and the arc age went with the
        # history they were measured over.
        self._recent_reply_words = ()
        self._arc_age_turns = 0
        self._last_stance_decision = None
        # K54 — a wiped history re-arms the appetite slip.
        self._topic_appetite_fired = False
        # K81 — a wiped history re-arms the taste-lean slip.
        self._taste_lean_fired = False
        self._conduct_notice_fired = False
        self._learning_reflection_fired = False
        # K57 — staged triggers die with the history (live episodes
        # persist in kv_meta by design).
        self._pending_emotion_triggers = []

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
        """Model configured for the ``main_chat`` route."""
        return self._effective_chat_model

    @property
    def effective_chat_model(self) -> str:
        return self._effective_chat_model

    @property
    def context_window_size(self) -> int:
        return self._context_window

    @property
    def max_tokens(self) -> int:
        """Per-reply generation cap from the ``main_chat`` route."""
        return self._max_tokens

    @property
    def context_window_source(self) -> str:
        """Where ``context_window`` came from: ``config|client|fallback``.

        ``config`` means the ``main_chat`` route's explicit
        ``context_window`` won. ``client``
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

    def _capture_embedding_swap_notice(self, rag_store: Any) -> None:
        """Turn a destructive LanceDB rebuild into a queued startup notice.

        ``RagStore`` drops and rebuilds its tables when the embedding model
        or dimension changes (mixing dims silently breaks search). That
        used to be a WARNING log only; here we capture it as a one-shot
        ``warning`` notice delivered in the WS ``hello`` so the user gets a
        visible toast that their document / message vectors were wiped (I7).
        """
        swap = getattr(rag_store, "embedding_swap", None)
        if not swap:
            return
        from_model = swap.get("from_model") or "unknown"
        to_model = swap.get("to_model") or "unknown"
        text = (
            "Embedding model changed ("
            f"{from_model} -> {to_model}). The vector index was rebuilt from "
            "scratch, so semantic search over older messages and uploaded "
            "documents is empty until they are re-indexed. Long-term memories "
            "are preserved (they live in SQLite)."
        )
        self._queue_startup_notice(
            kind="warning", text=text, code="embedding_rebuild", detail=swap,
        )

    def _queue_startup_notice(
        self,
        *,
        kind: str,
        text: str,
        code: str | None = None,
        detail: Any | None = None,
    ) -> None:
        notices = getattr(self, "_startup_notices", None)
        if notices is None:
            notices = []
            self._startup_notices = notices
        notice: dict[str, Any] = {"kind": kind, "text": text}
        if code is not None:
            notice["code"] = code
        if detail is not None:
            notice["detail"] = detail
        notices.append(notice)

    def consume_startup_notices(self) -> list[dict[str, Any]]:
        """Return and clear queued one-shot boot notices.

        Called when assembling the WS ``hello`` payload so the first client
        to connect after boot sees them once; later reconnects don't repeat
        a stale destructive-rebuild toast.
        """
        notices = getattr(self, "_startup_notices", None) or []
        self._startup_notices = []
        return list(notices)

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
        # Playback flows through ``set_pcm_listener`` -> WS hub ->
        # connected clients, so the engine never holds a device handle and
        # swapping one for another is invisible downstream.
        #
        # P28: honour ``tts.enabled``. Constructing a real engine pulls
        # its whole runtime -- PyTorch, ~0.6-1 GB resident -- and starts a
        # load thread, so a TTS-off install used to pay for an engine it
        # would never call. ``set_tts_enabled`` upgrades the null engine
        # to the real one if TTS is switched on at runtime.
        if not bool(getattr(settings.tts, "enabled", True)):
            from app.tts.null_tts_service import NullTtsService
            log.info("TTS disabled in settings: engine not loaded")
            return NullTtsService(settings.tts)

        # The provider setting was previously stored and switchable but
        # never read here, so every engine choice resolved to pocket-tts.
        # The registry decides what is installed and imports only the one
        # asked for; it also degrades rather than raising, because a
        # missing experimental venv should not stop the app from booting.
        from app.tts import registry

        provider = (
            getattr(settings.tts, "provider", "") or registry.DEFAULT_PROVIDER
        )
        return registry.build_with_fallback(provider, settings.tts)

    def _touch_user_activity(self) -> None:
        """Mark "the user just did something". Resets the idle gate.

        Called from the turn lifecycle and from incoming WS / REST
        traffic. The :class:`IdleWorkerScheduler` consults
        :meth:`_is_user_idle` before running a worker; a recent touch
        defers background work so it doesn't compete with the active
        conversation.

        The monotonic stamp drives the quiet gate. The wall-clock mirror
        in ``kv_meta`` drives idle *depth* (P36): monotonic resets to
        zero on restart, which would make an eight-hour absence look
        like a fresh one and keep the budget pinned at ``just_left``
        exactly when there is most catching up to do.
        """
        self._last_user_activity_at = time.monotonic()
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is None:
            return
        try:
            chat_db.kv_set(
                _KV_LAST_USER_ACTIVITY, timephrase.utcnow().isoformat(),
            )
        except Exception:
            log.debug("kv_set last_user_activity failed", exc_info=True)

    def _idle_depth_seconds(self) -> float:
        """Seconds since the user was last around, surviving a restart.

        ``max`` of the in-process monotonic elapsed and the wall-clock
        gap since the persisted stamp. The monotonic value is the
        trustworthy one while the process lives; the persisted one is
        what rescues depth after a reboot mid-absence. Taking the max
        means a clock jump can only ever make Aiko *more* willing to
        work, never less careful about the chat path -- and the quiet
        gate, not this number, is what protects an active conversation.
        """
        monotonic_elapsed = time.monotonic() - float(
            getattr(self, "_last_user_activity_at", 0.0) or 0.0
        )
        wall_elapsed = 0.0
        chat_db = getattr(self, "_chat_db", None)
        if chat_db is not None:
            try:
                raw = chat_db.kv_get(_KV_LAST_USER_ACTIVITY)
                if raw:
                    last = datetime.fromisoformat(str(raw))
                    wall_elapsed = (
                        timephrase.utcnow() - last
                    ).total_seconds()
            except Exception:
                log.debug("idle depth wall-clock read failed", exc_info=True)
        return max(0.0, monotonic_elapsed, wall_elapsed)

    def _llm_contention_grade(self) -> str:
        """How badly background LLM work fights the chat path for a GPU.

        Recomputed per tick rather than cached because the route table
        is editable at runtime from the settings drawer: pointing
        ``worker_default`` at a second backend should widen the LLM lane
        on the next tick, not on the next restart. The comparison is a
        couple of dict lookups.
        """
        from app.core.proactive.llm_contention import (
            CONTENTION_QUEUEING,
            classify_contention,
        )

        try:
            return classify_contention(
                self._settings.llm,
                override=getattr(
                    self._memory_settings,
                    "idle_worker_contention_override",
                    "auto",
                ),
            )
        except Exception:
            log.debug("contention classification failed", exc_info=True)
            return CONTENTION_QUEUEING

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
        # Fully shut down the RealtimeSTT recorder (not just its capture
        # context). This sets the subprocesses' shared shutdown_event so
        # their poll loops exit; skipping it orphans the children, which
        # then spin on a BrokenPipeError flooding the log. Run in a daemon
        # thread with a join timeout so a slow subprocess join can't wedge
        # app exit — the event is set synchronously at the top of
        # shutdown(), so even a timed-out join still stops the spin.
        try:
            t = threading.Thread(target=self._realtime_stt.shutdown, daemon=True)
            t.start()
            t.join(timeout=6.0)
        except Exception:
            pass
