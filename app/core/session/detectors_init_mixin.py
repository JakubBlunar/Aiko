"""Detectors + runtime bootstrap mixin.

Extracted from ``SessionController.__init__``. Holds the construction of
the per-turn detectors + one-shot inner-life state slots
(``_init_detectors_and_state``) and the proactive director + runtime
flags + WS listeners + MCP server + post-hook wiring
(``_init_runtime_and_hooks``). These run in the same order they used to
inline; state ownership is unchanged (every ``self.*`` assignment stays
verbatim).

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for a symbol used here must patch
``app.core.session.detectors_init_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
from typing import Any
from collections.abc import Callable
from app.core.proactive.proactive_director import ProactiveDirector
from app.core.session.session_state import SessionState
from collections import deque
import threading


log = logging.getLogger("app.session")


class DetectorsInitMixin:
    """__init__ bootstrap: detectors/state + proactive/runtime/hooks."""

    def _init_detectors_and_state(self, settings: AppSettings) -> None:
        self._pending_clarification: Any = None
        # K8 — one-shot affect-rupture slot. Same shape as above:
        # post-turn detector fills, next-turn provider clears.
        self._pending_rupture: Any = None
        # J6 — conflict-repair watch. Armed when a rupture fires; a later
        # post-turn valence recovery records a durable ``repair`` shared
        # moment and clears it. In-memory (a watch lost on restart is an
        # acceptable miss). Holds a ``conflict_repair.RepairWatch``.
        self._repair_watch: Any = None
        # K45 — mood inertia. ``_mood_inertia_reactions`` is a short
        # oldest-first ring of recent reaction tags (whiplash
        # detection); ``_pending_mood_inertia`` is the one-shot cue
        # slot (post-turn detector fills, next-turn provider clears);
        # the cooldown counter keeps one big mood swing from nagging
        # on consecutive turns. ``_mood_inertia_last`` is a debug
        # snapshot for the MCP state dump. ``_mood_inertia_force``
        # mirrors the K23/K29 one-shot bypass for MCP repro.
        self._mood_inertia_reactions: deque[str] = deque(maxlen=3)
        self._pending_mood_inertia: Any = None
        self._mood_inertia_cooldown_remaining: int = 0
        self._mood_inertia_last: dict[str, Any] | None = None
        self._mood_inertia_force: bool = False
        # K38 — one-shot self-correction slot + per-fire cooldown.
        # Post-turn detector fills the slot when Aiko's reply
        # contradicted one of her own high-confidence fact/preference
        # memories; the next-turn provider clears it. The cooldown
        # counter keeps a single slip from nagging every turn.
        self._pending_self_correction: Any = None
        self._self_correction_cooldown_remaining: int = 0
        # K23 — misattunement detector state. Unlike K8/K17 the
        # detector runs provider-time (same-turn reaction), so we
        # only need a cooldown counter -- no pending-result slot.
        # Decremented by ``_render_misattunement_block`` each call;
        # armed to ``misattunement_cooldown_turns`` when ``detect()``
        # returns a hit. ``_last_misattunement_*`` fields are
        # diagnostic-only (read by the MCP debug tool); no behaviour
        # depends on them.
        self._misattunement_cooldown: int = 0
        self._misattunement_force_next: bool = False
        self._last_misattunement_trigger: str | None = None
        self._last_misattunement_fire_turn: int | None = None
        # K29 — opinion-injection detector state. Same provider-time
        # shape as K23 (same-turn reaction), with two extra guards
        # against contrarianism: a per-session cap and an LLM
        # rate-limiter for the borderline-heuristic path. The
        # rate-limiter is constructed lazily below once the chat_db
        # is known; the per-session count resets on session boundary
        # via ``switch_session`` / ``clear_conversation_memory``.
        # ``_last_opinion_injection`` carries the most recent
        # :class:`OpinionInjectionResult` for the MCP debug tool;
        # behaviour does not depend on it.
        self._opinion_injection_cooldown: int = 0
        self._opinion_injection_session_count: int = 0
        self._opinion_injection_force_next: bool = False
        self._last_opinion_injection: Any = None
        self._opinion_injection_rate_limiter = None
        # P21 — deferred borderline verdict. The hot-path provider stashes a
        # PENDING borderline candidate in ``_pending_borderline``; the
        # post-turn resolver runs the rate-limited LLM verdict and, on YES,
        # arms ``_pending_cue`` (the rendered block) for the NEXT turn.
        self._opinion_injection_pending_borderline: dict[str, Any] | None = None
        self._opinion_injection_pending_cue: str | None = None
        # K28 — "What I've been turning over" between-session cue.
        # ``_pending_turning_over_seconds`` is armed by the post-turn
        # engagement tracker when a typed turn lands after a gap of
        # at least ``memory.turning_over_min_gap_minutes``. The next
        # prompt assembly's provider reads + clears the slot and
        # runs the picker. ``_turning_over_force_next`` is the MCP
        # debug bypass (set by ``force_turning_over``); cleared
        # whether the picker fires or not so the bypass is strictly
        # one-turn. ``_last_turning_over`` carries the most recent
        # :class:`TurningOverResult` for the MCP debug tool.
        self._pending_turning_over_seconds: float | None = None
        self._turning_over_force_next: bool = False
        self._last_turning_over: Any = None
        # K36 — "things I did while you were away". ``_pending_away_
        # activities_seconds`` is armed by the post-turn tracker on a
        # typed gap >= ``memory.away_activities_min_gap_hours`` (default
        # 4h). The provider reads + clears the slot, reads the
        # IdleAwayActivityWorker journal, and defers to turning_over via
        # the shared ``_gap_cue_surfaced`` flag. ``_away_activities_
        # force_next`` is the MCP debug bypass.
        self._pending_away_activities_seconds: float | None = None
        self._away_activities_force_next: bool = False
        self._gap_cue_surfaced: bool = False
        self._away_activity_worker: Any = None
        # K34 — "forward curiosity". ``_pending_forward_curiosity_
        # seconds`` is armed by the post-turn tracker on a typed gap >=
        # ``memory.forward_curiosity_min_gap_hours`` (default 4h). The
        # provider reads + clears the slot, reads the ForwardCuriosity
        # question ring, and defers to turning_over / away_activities via
        # the shared ``_gap_cue_surfaced`` flag. ``_forward_curiosity_
        # force_next`` is the MCP debug bypass.
        self._pending_forward_curiosity_seconds: float | None = None
        self._forward_curiosity_force_next: bool = False
        self._forward_curiosity_worker: Any = None
        # K30 — self-noticing cues (agreement-streak / flat-affect /
        # repeated-thought). Three sub-detectors fan into one
        # ``self_noticing`` inner-life block. Agreement-streak is
        # stateless (the provider queries ``chat_db.get_messages`` per
        # turn, K23-style); the other two each own a small ring on
        # the controller because ``AffectState`` has no per-turn
        # ring buffer and there's no shared "recent assistant
        # vectors" accessor on ``RagStore``. ``_repeated_thought_*``
        # is the one-shot carry-forward flag set in ``post_turn``
        # and consumed by the next provider call. Force flags
        # mirror the K23 / K29 one-shot bypass shape so the MCP
        # debug tools can drop a cue into the next prompt without
        # waiting for the streak to genuinely fire.
        self_noticing_window = max(
            1, int(getattr(settings.agent, "self_noticing_window", 6))
        )
        self._self_noticing_affect_samples: deque[
            tuple[float, float, str | None]
        ] = deque(maxlen=max(12, self_noticing_window * 2))
        self._self_noticing_aiko_vecs: deque[Any] = deque(maxlen=3)
        self._self_noticing_force_agreement: bool = False
        self._self_noticing_force_flat_affect: bool = False
        self._self_noticing_force_repeated_thought: bool = False

        # K47 — question/share balance. Rolling ring of "did this reply
        # contain a question" flags + a suppress countdown. The post-turn
        # hook appends + arms; the provider-time guards read
        # ``_question_balance_suppress_remaining`` to mute the
        # question-pushing cues and surface a share-first cue instead.
        question_balance_window = max(
            2, int(getattr(settings.agent, "question_balance_window", 10))
        )
        self._question_turn_flags: deque[bool] = deque(
            maxlen=question_balance_window
        )
        self._question_balance_suppress_remaining: int = 0

        # K48 — tease rhythm (banter budget). Rolling ring of "was this
        # reply a tease" flags + the id of the most recent tease so the
        # post-turn hook can read its K32 reactions next turn. The
        # verdict lands in ``_pending_tease_cue`` (one-shot, consumed by
        # the provider); ``_tease_cue_cooldown`` rate-limits firing.
        tease_rhythm_window = max(
            2, int(getattr(settings.agent, "tease_rhythm_window", 6))
        )
        self._tease_flags: deque[bool] = deque(maxlen=tease_rhythm_window)
        self._last_tease_message_id: int | None = None
        self._pending_tease_cue: str | None = None
        self._tease_cue_cooldown: int = 0
        self._tease_rhythm_force: str | None = None
        self._self_noticing_agreement_cooldown: int = 0
        self._self_noticing_flat_affect_cooldown: int = 0
        self._repeated_thought_fired_last_turn: bool = False
        self._repeated_thought_last_cosine: float = 0.0
        self._repeated_thought_last_matched_index: int = -1
        # Diagnostic-only — most-recent verdicts from the three
        # sub-detectors. Read by ``get_self_noticing_state`` over MCP;
        # no behaviour depends on them.
        self._last_self_noticing_agreement: Any = None
        self._last_self_noticing_flat_affect: Any = None
        # K27 — daily personality colour MCP debug flags. The canonical
        # roll is performed by :class:`DayColorWorker` (registered on
        # the idle scheduler above) and by the lazy fallback in
        # :meth:`_render_day_color_block` for the first-turn-after-
        # midnight case. These flags only exist to let MCP debug tools
        # override the next provider call without waiting for natural
        # cadence:
        #
        # * ``_day_color_force_next``: name of a palette colour to
        #   render on the next call regardless of kv_meta state. Does
        #   NOT touch ``kv_meta`` (so the persisted roll survives).
        #   Consumed one-shot.
        # * ``_day_color_force_reroll``: when True, the next provider
        #   call rolls a fresh colour and writes it to ``kv_meta``
        #   (useful for repro without shifting the OS clock).
        #   Consumed one-shot.
        self._day_color_force_next: str | None = None
        self._day_color_force_reroll: bool = False
        # K60 — one-shot MCP bypass (``force_dere_slip``) of the
        # dere-slip intensity + cooldown gates. Consumed by the next
        # emotion-episode provider call that hits a masked episode.
        self._mask_force_slip_next: bool = False
        # K15 -- vulnerability budget MCP debug flags. The persisted
        # bucket lives in ``kv_meta`` (``aiko.vulnerability_budget``)
        # and is read+decayed lazily on every provider call; these
        # flags only exist so MCP debug tools can override the next
        # render or wipe the persisted state without crafting real
        # self-tags:
        #
        # * ``_vulnerability_budget_force_spent``: when set, the
        #   next provider call renders the cue as if ``state.spent``
        #   equalled this value. Does NOT touch ``kv_meta`` (so the
        #   real persisted bucket survives the test). Consumed
        #   one-shot.
        # * ``_vulnerability_budget_force_reset``: when True, the
        #   next provider call writes a fresh
        #   ``BudgetState(spent=0)`` to ``kv_meta``. Consumed
        #   one-shot.
        self._vulnerability_budget_force_spent: float | None = None
        self._vulnerability_budget_force_reset: bool = False
        if (
            self._chat_db is not None
            and bool(getattr(settings.agent, "belief_tracking_enabled", True))
        ):
            try:
                from app.core.relationship.belief_store import BeliefStore

                self._belief_store = BeliefStore(self._chat_db)
            except Exception:
                log.warning("BeliefStore init failed", exc_info=True)
                self._belief_store = None
        if self._belief_store is not None:
            try:
                from app.core.relationship.belief_gap_detector import BeliefGapDetector

                self._belief_gap_detector = BeliefGapDetector(
                    belief_store=self._belief_store,
                    belief_settings=self._memory_settings,
                )
            except Exception:
                log.warning("BeliefGapDetector init failed", exc_info=True)
                self._belief_gap_detector = None
        if (
            self._idle_scheduler is not None
            and self._belief_store is not None
            and self._fact_check_cancel is not None
            and self._embedder is not None
            and bool(getattr(settings.agent, "belief_worker_enabled", True))
        ):
            try:
                from app.core.relationship.belief_worker import BeliefInferenceWorker
                from app.core.memory.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )

                self._belief_rate_limiter = FactCheckRateLimiter(
                    self._chat_db,
                    per_hour_cap=int(
                        getattr(
                            settings.agent,
                            "belief_worker_per_hour_cap",
                            4,
                        )
                    ),
                    per_day_cap=int(
                        getattr(
                            settings.agent,
                            "belief_worker_per_day_cap",
                            20,
                        )
                    ),
                    state_key="belief_worker.rate_state",
                )
                self._belief_worker = BeliefInferenceWorker(
                    belief_store=self._belief_store,
                    chat_db=self._chat_db,
                    embedder=self._embedder,
                    # Idle-scheduler worker → maintenance tier.
                    ollama=self._maintenance_client,
                    chat_model=self._effective_worker_model,
                    rate_limiter=self._belief_rate_limiter,
                    cancel_event=self._fact_check_cancel,
                    agent_settings=settings.agent,
                    belief_settings=self._memory_settings,
                    session_id_provider=lambda: self._session_id,
                    user_id_provider=lambda: self._user_id,
                    user_names_provider=lambda: [self.user_display_name]
                    if self.user_display_name
                    else [],
                    assistant_name_provider=lambda: "Aiko",
                    notify_belief_added=self._notify_belief_added,
                    notify_belief_updated=self._notify_belief_updated,
                )
                self._idle_scheduler.register(self._belief_worker)
            except Exception:
                log.warning("BeliefInferenceWorker init failed", exc_info=True)
                self._belief_worker = None
                self._belief_rate_limiter = None

        # Phase 3c (reworked): context-aware promise extraction worker.
        # Sole writer of ``kind="promise"`` memories now -- reads the last
        # few turns for context, extracts self-contained promises via the
        # worker LLM, quality-gates + dedupes, and writes long_term rows.
        self._promise_rate_limiter = None
        if (
            self._idle_scheduler is not None
            and self._memory_store is not None
            and self._fact_check_cancel is not None
            and self._embedder is not None
            and bool(getattr(settings.agent, "promise_worker_enabled", True))
        ):
            try:
                from app.core.memory.promise_worker import (
                    PromiseExtractionWorker,
                )
                from app.core.memory.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )

                self._promise_rate_limiter = FactCheckRateLimiter(
                    self._chat_db,
                    per_hour_cap=int(
                        getattr(
                            settings.agent,
                            "promise_worker_per_hour_cap",
                            10,
                        )
                    ),
                    per_day_cap=int(
                        getattr(
                            settings.agent,
                            "promise_worker_per_day_cap",
                            60,
                        )
                    ),
                    state_key="promise_worker.rate_state",
                )
                self._promise_worker = PromiseExtractionWorker(
                    memory_store=self._memory_store,
                    chat_db=self._chat_db,
                    embedder=self._embedder,
                    # Idle-scheduler worker → maintenance tier.
                    ollama=self._maintenance_client,
                    chat_model=self._effective_worker_model,
                    rate_limiter=self._promise_rate_limiter,
                    cancel_event=self._fact_check_cancel,
                    agent_settings=settings.agent,
                    memory_settings=self._memory_settings,
                    session_id_provider=lambda: self._session_id,
                    user_display_name_provider=lambda: self.user_display_name,
                    user_names_provider=lambda: [self.user_display_name]
                    if self.user_display_name
                    else [],
                    assistant_name_provider=lambda: "Aiko",
                )
                self._idle_scheduler.register(self._promise_worker)
            except Exception:
                log.warning(
                    "PromiseExtractionWorker init failed", exc_info=True
                )
                self._promise_worker = None
                self._promise_rate_limiter = None

        # K6 — surprise / novelty detector. Pure in-process helper:
        # one embed + a tiny in-memory ring per turn, no DB writes,
        # no background worker. Registered as a per-turn inner-life
        # provider below (taking ``user_text``), same shape as the
        # F2 knowledge-gap block. Requires an Embedder; if RAG is
        # disabled the detector still works (it just can't warm
        # from past sessions and starts every install cold).
        self._novelty_detector = None
        if (
            self._embedder is not None
            and bool(getattr(settings.agent, "novelty_detection_enabled", True))
        ):
            try:
                from app.core.conversation.novelty_detector import NoveltyDetector

                # F10k: hand the detector a late-bound accessor for the
                # topic graph so it can name topic transitions, but only
                # when the master switch is on (off → tracking disabled,
                # K6/K18 behave exactly as before). Restart to toggle.
                topic_graph_provider = None
                if bool(
                    getattr(settings.agent, "topic_tracking_enabled", True)
                ):
                    topic_graph_provider = lambda: getattr(
                        self, "_topic_graph", None
                    )
                self._novelty_detector = NoveltyDetector(
                    embedder=self._embedder,
                    rag_store=self._rag_store,
                    user_id=self._user_id,
                    memory_settings=self._memory_settings,
                    topic_graph_provider=topic_graph_provider,
                )
            except Exception:
                log.warning("NoveltyDetector init failed", exc_info=True)
                self._novelty_detector = None

        # K18 (topic stagnation) — sibling of K6 that consumes the
        # per-turn distance the novelty detector exposes via
        # ``last_distance``/``last_band``. No embedder, no rag_store,
        # no rate-cap; the per-turn cost is a deque append + a mean.
        # Disabling K6 doesn't disable K18 explicitly here, but the
        # provider returns "" silently when ``last_distance`` is
        # always None (which it will be without the K6 detector
        # populating it), so the cue stays quiet.
        self._topic_stagnation_detector = None
        if bool(
            getattr(settings.agent, "topic_stagnation_enabled", True)
        ):
            try:
                from app.core.conversation.topic_stagnation import (
                    TopicStagnationDetector,
                )

                self._topic_stagnation_detector = TopicStagnationDetector(
                    memory_settings=self._memory_settings,
                )
            except Exception:
                log.warning(
                    "TopicStagnationDetector init failed", exc_info=True,
                )
                self._topic_stagnation_detector = None

        # Anti-rut layer: AikoStylePatternTracker watches Aiko's *own*
        # recent assistant turns for opener / question / length ruts
        # and surfaces a soft "Heads-up" inner-life cue when one of
        # the bands trips. Sibling architecture to K6/K18; cheap pure
        # rolling-window detector (no embedder, no LLM). Per-band
        # cooldowns plus the in-prompt cue let the rut self-correct
        # over a few turns instead of recurring forever.
        self._aiko_style_tracker = None
        if bool(
            getattr(settings.agent, "style_tracker_enabled", True)
        ):
            try:
                from app.core.persona.aiko_style_tracker import (
                    AikoStylePatternTracker,
                )

                self._aiko_style_tracker = AikoStylePatternTracker(
                    agent_settings=settings.agent,
                )
            except Exception:
                log.warning(
                    "AikoStylePatternTracker init failed", exc_info=True,
                )
                self._aiko_style_tracker = None

        # K13 stylometric mirror: tracks Jacob's writing style across
        # recent user turns. Persisted via a tiny JSON-blob table so
        # the rolling window survives restart; warmed lazily on first
        # invocation if the persisted blob is missing or empty (one
        # cheap scan over the latest user messages from chat_db).
        self._style_signal_analyzer = None
        self._style_signal_store = None
        self._style_signal_warmed = False
        if bool(
            getattr(settings.agent, "style_signal_enabled", True)
        ):
            try:
                from app.core.persona.style_signal import (
                    StyleSignalAnalyzer,
                    StyleSignalStore,
                )

                self._style_signal_analyzer = StyleSignalAnalyzer(
                    agent_settings=settings.agent,
                )
                self._style_signal_store = StyleSignalStore(self._chat_db)
                # Restore from persistence eagerly (cheap one-row read);
                # cross-session warm from chat history happens lazily on
                # the first post-turn record so a brand-new install
                # warms naturally instead of doing a full DB scan at
                # boot.
                try:
                    blob = self._style_signal_store.load(self._user_id)
                    if blob:
                        self._style_signal_analyzer.from_dict(blob)
                except Exception:
                    log.debug(
                        "style_signal initial load failed", exc_info=True,
                    )
            except Exception:
                log.warning(
                    "StyleSignalAnalyzer init failed", exc_info=True,
                )
                self._style_signal_analyzer = None
                self._style_signal_store = None

        # K20: metacognitive calibration store. Holds per-user
        # CalibrationState (global score + bounded topic slots) so
        # decay survives restart. Constructed unconditionally
        # (read-side bonus stays available even when the detector's
        # write side is disabled) -- production code reads the state
        # baseline from the configured ``calibration_baseline``.
        self._calibration_store = None
        # Cache slots for the K20 softening detector + topic centroid.
        # ``_last_assistant_vec`` is set by K22's wire-in when it
        # embeds the just-emitted reply; ``_prior_assistant_vec`` is
        # carried forward by K20's wire-in to the next turn so the
        # softening detector can compare the next user message
        # against the claim that triggered the pushback.
        self._last_assistant_vec = None
        self._prior_assistant_vec = None
        try:
            from app.core.affect.calibration_store import CalibrationStore

            self._calibration_store = CalibrationStore(
                self._chat_db,
                baseline=float(
                    getattr(
                        settings.memory, "calibration_baseline", 0.80,
                    )
                ),
            )
        except Exception:
            log.warning(
                "CalibrationStore init failed", exc_info=True,
            )
            self._calibration_store = None

        # K24: sensory anchoring cadence. Per-controller state
        # holder for the "small physical beat available" cue. No
        # persistence -- the in-memory cooldown counter resets on
        # restart, worst case = one extra beat in the first quiet
        # window post-boot. Gated by ``agent.sensory_anchor_enabled``;
        # provider short-circuits to ``""`` when the cadence is None.
        self._sensory_anchor_cadence = None
        if bool(
            getattr(settings.agent, "sensory_anchor_enabled", True)
        ):
            try:
                from app.core.conversation.sensory_anchor import SensoryAnchorCadence

                self._sensory_anchor_cadence = SensoryAnchorCadence(
                    max_recent=int(
                        getattr(
                            settings.memory,
                            "sensory_anchor_max_recent_items",
                            4,
                        )
                    ),
                )
            except Exception:
                log.warning(
                    "SensoryAnchorCadence init failed", exc_info=True,
                )
                self._sensory_anchor_cadence = None

        # K14: implicit engagement tracker. Reuses the K13 rolling word-
        # count window via ``recent_word_counts()`` so we don't pay a
        # second buffer. ``None`` when the master toggle is off; the
        # post-turn pipeline gates on the attribute being non-None.
        self._engagement_tracker = None
        if bool(
            getattr(settings.agent, "engagement_tracker_enabled", True)
        ):
            try:
                from app.core.affect.engagement_tracker import EngagementTracker

                word_count_provider = None
                analyzer = self._style_signal_analyzer
                if analyzer is not None:
                    word_count_provider = analyzer.recent_word_counts
                self._engagement_tracker = EngagementTracker(
                    agent_settings=settings.agent,
                    word_count_window_provider=word_count_provider,
                )
            except Exception:
                log.warning(
                    "EngagementTracker init failed", exc_info=True,
                )
                self._engagement_tracker = None
        # K14 per-turn state: read by ``_post_turn_inner_life`` to
        # compute reply latency, the typed-proactive eligibility
        # predicate to skip nudging an abandoned conversation, and the
        # absence-curiosity inner-life provider. Bookended by the
        # ``chat_once_streaming`` entry (stashes ``_last_turn_mode``)
        # and the post-turn pipeline (stashes label + absence).
        self._last_turn_mode: str = "typed"
        self._last_engagement_label: str = "neutral"
        self._pending_absence_seconds: float | None = None

    def _init_runtime_and_hooks(self, settings: AppSettings) -> None:
        self._proactive = ProactiveDirector(
            self._chat_client,
            self._chat_db,
            self._prompt_assembler,
            model=self._effective_chat_model,
            speak=self._tts.enqueue,
            is_busy=lambda: self._turn_in_progress,
            is_live_mode=lambda: self._live_voice_session_active,
            cooldown_seconds=float(
                getattr(settings.agent, "proactive_cooldown_seconds", 120.0),
            ),
            cooldown_seconds_typed=float(
                getattr(settings.agent, "proactive_cooldown_seconds_typed", 600.0),
            ),
            is_typed_eligible=self._is_typed_proactive_eligible,
            typed_tts_enabled=lambda: bool(
                getattr(
                    getattr(self._settings, "agent", None),
                    "proactive_typed_tts_enabled",
                    False,
                )
            ),
            context_window=self._context_window,
            notify_message=self._notify_message,
            prepared_nudge_store=self._prepared_nudge_store,
            user_id=self._user_id,
            user_display_name_provider=lambda: self.user_display_name,
            arc_store=self._arc_store,
        )

        # ── Runtime state ────────────────────────────────────────────────
        self._vad_level_threshold = settings.audio.vad_level_threshold
        self._vad_silence_seconds = settings.audio.vad_silence_seconds
        # Push-to-talk / input mode bookkeeping moved to the client.
        # The server only ever sees the resulting PCM stream.
        self._live_no_speech_streak = 0
        self._live_voice_session_active = False
        self._turn_in_progress = False

        # ── Typed-mode proactive timer + presence gate ──────────────
        # The typed-mode ``ProactiveDirector`` path fires opportunistic
        # "pick up the thread" nudges after a long quiet period (4 min
        # default). It's gated on user presence so we never poke
        # someone who alt-tabbed away. Two complementary signals fold
        # client-side into one boolean:
        #   * Browser: ``document.visibilityState === "visible"``.
        #   * Tauri:  ``tauri://focus`` / ``tauri://blur`` events.
        # Default ``True`` so a freshly-loaded UI that hasn't sent a
        # presence frame yet still works.
        self._typed_silence_timer: threading.Timer | None = None
        self._typed_silence_lock = threading.Lock()
        self._user_present: bool = True
        # Wall-clock (monotonic) when the timer was last armed AND the
        # silence budget at that moment. Used to re-arm with a smaller
        # remainder when presence flips ``False -> True`` mid-budget.
        self._typed_silence_armed_at: float | None = None
        self._typed_silence_armed_budget: float | None = None
        # Activity awareness (Phase 4): the foreground app the user is
        # currently in. ``None`` covers "couldn't determine", "user is
        # in our own window", and "feature disabled". Browser users
        # never set this. The setter is gated server-side on
        # ``activity_awareness_enabled`` so a buggy client emitting
        # events while the toggle is off can't leak the data.
        self._user_active_app: str | None = None

        self._remember_history = settings.assistant.remember_history
        self._state = SessionState(
            mic_enabled=settings.audio.enable_microphone,
            session_type="chat",
        )
        self._decision_trace: deque[dict[str, str]] = deque(maxlen=500)

        # ── Metrics ──────────────────────────────────────────────────────
        self._last_metrics: dict[str, Any] = self._zero_metrics()
        self._metrics_history: deque[dict[str, Any]] = deque(maxlen=10)
        self._compactions_total = 0
        # TTS timing: the moment chat_once_streaming finishes the LLM stream
        # is the natural "TTS may begin" mark; ``_tts_turn_start_at`` captures
        # that. We update ``_last_metrics["tts_ms"]`` when the TTS queue
        # signals "end" for a session that started after the LLM was done.
        self._tts_turn_start_at: float | None = None
        self._tts_turn_first_start_at: float | None = None

        # ── Listeners ────────────────────────────────────────────────────
        self._message_listeners: list[Callable[[str, str], None]] = []
        self._tts_state_listeners: list[Callable[..., None]] = []
        self._tts_amplitude_listeners: list[Callable[[float], None]] = []
        self._metrics_listeners: list[Callable[[dict[str, Any]], None]] = []
        self._tts.set_amplitude_listener(self._on_tts_amplitude)
        self._models_cache: list[str] | None = None
        self._models_cache_time = 0.0
        self._cache_ttl = 60.0

        # ── MCP debug server ─────────────────────────────────────────────
        self._mcp_server_runner = None
        if settings.mcp_server.enabled:
            try:
                from app.mcp.runner import McpServerRunner
                from app.mcp.server import create_mcp_server
                mcp_srv = create_mcp_server(self, port=settings.mcp_server.port)
                self._mcp_server_runner = McpServerRunner(
                    mcp_srv, port=settings.mcp_server.port,
                )
                self._mcp_server_runner.start()
            except Exception:
                log.warning("Failed to start embedded MCP server", exc_info=True)

        # ── Phase 2a + 2b: resume opener + dream worker ─────────────────
        # Both ride the listening-window executor so init never blocks
        # on an LLM round-trip. The dream pass writes a salience-boosted
        # ``reflection`` memory; the resume pass then has a fresher
        # candidate to weave when it primes the welcome-back line.
        try:
            self._maybe_schedule_dream_pass()
        except Exception:
            log.debug("dream pass schedule failed", exc_info=True)
        try:
            self._maybe_schedule_resume_opener()
        except Exception:
            log.debug("resume opener schedule failed", exc_info=True)

        # Phase B2 — register the internal listener that turns
        # backchannel hints into low-priority motion broadcasts. Done
        # after every dependency is wired so the callback can use
        # ``self._avatar`` / ``self._avatar_motion_listeners``
        # (registered above).
        self.add_backchannel_listener(self._emit_backchannel_motion)

        # K1 follow-up — first-run onboarding goal seed. Two entry
        # paths converge on ``_seed_onboarding_goal_if_first_time``:
        #
        # 1. **Backfill** (this call here): if the user already has a
        #    display name set (returning user / migrated profile) and
        #    the ``goals.onboarding_goal_seeded`` kv_meta row is
        #    absent, drop the curated goal in now. Idempotent — on
        #    every subsequent boot the kv_meta gate skips.
        # 2. **Identity listener** (registered below): the first
        #    time ``update_user_display_name`` lands a real name,
        #    fire the seed automatically. The ``needs_onboarding``
        #    gate inside the method means the listener is a no-op
        #    until the name is actually set.
        try:
            self._seed_onboarding_goal_if_first_time()
        except Exception:
            log.debug(
                "onboarding-goal backfill failed", exc_info=True,
            )
        self.add_identity_listener(
            lambda _new_name: self._seed_onboarding_goal_if_first_time(),
        )

        # ── Brain orchestration (chunk 5 of phase 1) ─────────────────
        # Wire the task subsystem last so ``_init_task_orchestration``
        # can read every dependency it needs (``_chat_db``, ``_tts``,
        # ``_last_user_activity_at``, ``_settings.agent``) plus the
        # ``self._prompt_assembler`` we'll hook the cue provider into
        # below. The mixin is a clean no-op when
        # ``agent.tasks_enabled`` is False — the subsystem stays
        # dormant and ``self._brain_loop`` stays ``None``.
        try:
            self._init_task_orchestration()
        except Exception:
            log.exception("task-orchestration init failed")
        # The initial ``rebuild_tool_registry()`` above ran before the
        # orchestrator existed, so the filesystem task tools
        # (``list_file_roots`` / ``start_file_search`` / …) were gated
        # out (their gate is ``_task_orchestrator is not None``). Now
        # that orchestration is wired, rebuild once more so those tools
        # actually land in the registry the LLM sees. Cheap + idempotent.
        if getattr(self, "_task_orchestrator", None) is not None:
            try:
                self.rebuild_tool_registry()
            except Exception:
                log.warning(
                    "tool registry rebuild after orchestration init failed",
                    exc_info=True,
                )
        # Install the T6 task-cues provider on the prompt assembler.
        # Best-effort: a broken provider call lands as an empty
        # block (the assembler swallows provider exceptions), but a
        # missing assembler (very early shutdown / partial init)
        # would crash here so we guard the install too.
        if getattr(self, "_prompt_assembler", None) is not None:
            try:
                self._prompt_assembler.set_inner_life_providers(
                    task_cues=lambda: self.drain_task_cues_for_render(
                        turn_id=None,
                    ),
                    running_tasks=self._render_running_tasks_block,
                )
            except Exception:
                log.debug(
                    "task-orchestration provider install on prompt assembler failed",
                    exc_info=True,
                )
