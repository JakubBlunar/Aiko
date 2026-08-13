"""Speaking-window worker bootstrap mixin.

Extracted from ``SessionController.__init__``. Holds the construction of
the foreground/background workers built before the scheduler
(``_init_speaking_workers``: reflection, dream, catchphrase, curiosity,
relationship, agenda, promise, dialogue-act, user-profile,
consolidator, shared-moments, knowledge-gap, goals, fact-check,
relationship-axes, touch) and the speaking-window scheduler + its
registered jobs (``_init_speaking_window``). Runs in the same order it
used to inline; state ownership is unchanged.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for a symbol used here must patch
``app.core.session.speaking_workers_init_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from collections.abc import Callable
from app.core.memory.memory_extractor import MemoryExtractor
from app.core.infra import timephrase
from pathlib import Path
from app.core.voice.speaking_window_scheduler import SpeakingWindowScheduler
from app.core.proactive.summary_worker import SummaryWorker
from app.core.session.turn_runner import TurnRunner
import threading
import time

if TYPE_CHECKING:
    from app.core.infra.settings import AppSettings


log = logging.getLogger("app.session")


class SpeakingWorkersInitMixin:
    """__init__ bootstrap: speaking-window workers + scheduler/jobs."""

    def _init_speaking_workers(self, settings: AppSettings) -> None:
        self._reflection_worker = None
        try:
            from app.core.proactive.reflection_worker import ReflectionWorker

            self._reflection_worker = ReflectionWorker(
                ollama=self._ollama,
                memory_store=self._memory_store,
                embedder=self._embedder,
                model=self._effective_worker_model,
                min_seconds_between=settings.agent.reflection_min_seconds_between,
                emotional_delta_threshold=settings.agent.reflection_emotional_delta_threshold,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("ReflectionWorker init failed", exc_info=True)
            self._reflection_worker = None

        # Phase 2b: DreamWorker — bootstrap-time reflection that runs
        # once per app start when the gap since the last assistant turn
        # exceeds a threshold. Writes a kind=reflection memory tagged
        # ``[dream]`` so the resume opener / NarrativeWeaver can prefer
        # it as a candidate when seeding the welcome-back line.
        self._dream_worker = None
        if bool(getattr(settings.agent, "dream_worker_enabled", True)):
            try:
                from app.core.proactive.dream_worker import DreamWorker

                self._dream_worker = DreamWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    model=self._effective_worker_model,
                    chat_db=self._chat_db,
                    min_hours_since_last=float(
                        getattr(
                            settings.agent,
                            "dream_worker_min_hours_since_last", 6.0,
                        ),
                    ),
                    user_display_name_provider=lambda: self.user_display_name,
                )
            except Exception:
                log.warning("DreamWorker init failed", exc_info=True)
                self._dream_worker = None

        # Phase 2c: CatchphraseMiner — speaking-window job that mines
        # recurring 3-7-word phrases across recent user + assistant
        # turns. Surfaced via the catchphrase inner-life block.
        self._catchphrase_miner = None
        if bool(getattr(settings.agent, "catchphrase_miner_enabled", True)):
            try:
                from app.core.memory.catchphrase_miner import CatchphraseMiner

                self._catchphrase_miner = CatchphraseMiner(
                    chat_db=self._chat_db,
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    min_seconds_between=float(
                        getattr(
                            settings.agent,
                            "catchphrase_miner_min_seconds_between", 600.0,
                        ),
                    ),
                    min_new_user_turns=int(
                        getattr(
                            settings.agent,
                            "catchphrase_miner_min_new_user_turns", 6,
                        ),
                    ),
                    min_total_count=int(
                        getattr(
                            settings.agent,
                            "catchphrase_miner_min_total_count", 3,
                        ),
                    ),
                )
            except Exception:
                log.warning("CatchphraseMiner init failed", exc_info=True)
                self._catchphrase_miner = None

        # Phase 4b: ambient-noise tracker. EMAs the mic floor during
        # silence-only chunks so the prompt + Pocket-TTS know whether
        # the room is quiet, hums, or is loudly noisy. Optional: the
        # capture path is a no-op if the tracker is None.
        self._ambient_noise = None
        try:
            from app.core.affect.ambient_noise import AmbientNoiseTracker

            self._ambient_noise = AmbientNoiseTracker()
        except Exception:
            log.warning("AmbientNoiseTracker init failed", exc_info=True)
            self._ambient_noise = None

        # Phase 4c: CuriosityWorker — emits a small "next-turn"
        # follow-up question into the open_question store when the
        # current arc is shallow and the user hasn't been asking much.
        self._curiosity_worker = None
        if bool(getattr(settings.agent, "curiosity_worker_enabled", True)):
            try:
                from app.core.proactive.curiosity_worker import CuriosityWorker

                self._curiosity_worker = CuriosityWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    model=self._effective_worker_model,
                    min_turns_between=int(
                        getattr(settings.agent, "curiosity_worker_min_turns_between", 3),
                    ),
                    min_seconds_between=float(
                        getattr(settings.agent, "curiosity_worker_min_seconds_between", 60.0),
                    ),
                    max_user_word_count=int(
                        getattr(settings.agent, "curiosity_worker_max_user_word_count", 8),
                    ),
                    user_display_name_provider=lambda: self.user_display_name,
                    # K65c: anchor the follow-up on a known-but-quiet K9
                    # interest. Late-bound lambda — the topic graph is built
                    # later in this same mixin, so it resolves at fire time.
                    # Returns [] in the non-persistent / cold state, which
                    # keeps the worker on its legacy literal-words anchoring.
                    interest_provider=lambda: (
                        self._topic_graph.cluster_activity(top_n=8, min_size=3)
                        if getattr(self, "_topic_graph", None) is not None
                        else []
                    ),
                    cluster_anchor_enabled=bool(
                        getattr(
                            settings.agent,
                            "curiosity_worker_cluster_anchor_enabled",
                            True,
                        )
                    ),
                    quiet_min_days=float(
                        getattr(settings.agent, "curiosity_worker_quiet_days", 7.0),
                    ),
                    subject_quota=float(
                        getattr(
                            settings.agent, "curiosity_subject_quota", 0.4,
                        ),
                    ),
                )
            except Exception:
                log.warning("CuriosityWorker init failed", exc_info=True)
                self._curiosity_worker = None

        # Phase 3b: relationship tracker (turn / session counters + phase
        # + milestones). Hot-path safe: a single SQLite row per user.
        self._relationship_store = None
        self._relationship_tracker = None
        try:
            from app.core.relationship.relationship import (
                RelationshipStore, RelationshipTracker,
            )
            self._relationship_store = RelationshipStore(self._chat_db)
            self._relationship_tracker = RelationshipTracker(
                self._relationship_store,
            )
            # Bump session counter on init.
            try:
                self._relationship_tracker.register_session_start(self._user_id)
            except Exception:
                log.debug("relationship session start failed", exc_info=True)
        except Exception:
            log.warning("RelationshipTracker init failed", exc_info=True)
            self._relationship_store = None
            self._relationship_tracker = None

        # Phase 4a: agenda store + LLM grooming worker.
        self._agenda_store = None
        self._agenda_worker = None
        try:
            from app.core.goals.agenda import AgendaStore, AgendaWorker

            self._agenda_store = AgendaStore(
                self._chat_db, on_change=self._notify_agenda,
            )
            self._agenda_worker = AgendaWorker(
                ollama=self._ollama,
                store=self._agenda_store,
                model=self._effective_worker_model,
                every_n_turns=settings.agent.agenda_groom_every_n_turns,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("AgendaStore/AgendaWorker init failed", exc_info=True)
            self._agenda_store = None
            self._agenda_worker = None

        # Phase 3c (reworked): promise extraction now runs as the
        # context-aware ``PromiseExtractionWorker`` idle worker, registered
        # alongside the other LLM idle workers below. The old regex
        # post-turn + speaking-window tracks were retired because they
        # wrote context-free fragments ("Jacob promised: never know").
        self._promise_worker = None

        # K4: per-turn dialogue-act tagger. Regex hot path runs inline
        # in ``_post_turn_inner_life``; the LLM cold path (~3 user-turn
        # cadence) upgrades any low-confidence regex result on the
        # speaking-window scheduler.
        self._dialogue_act_tagger = None
        try:
            from app.core.conversation.dialogue_act_tagger import DialogueActTagger

            self._dialogue_act_tagger = DialogueActTagger(
                ollama=self._ollama,
                chat_db=self._chat_db,
                model=self._effective_worker_model,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("DialogueActTagger init failed", exc_info=True)
            self._dialogue_act_tagger = None

        # Phase 3a: structured user profile + per-turn user-state estimator.
        # The store is hot-path-safe (small SQL reads) and the estimator
        # runs after every turn (regex only). The worker is LLM-driven and
        # only fires every N user turns inside the speaking window.
        self._user_profile_store = None
        self._user_profile_worker = None
        self._user_state_store = None
        self._user_state_estimator = None
        try:
            from app.core.infra.user_profile import (
                UserProfileStore, UserProfileWorker,
            )
            from app.core.affect.user_state import UserStateEstimator, UserStateStore

            self._user_profile_store = UserProfileStore(self._chat_db)
            self._user_state_store = UserStateStore(self._chat_db)
            self._user_state_estimator = UserStateEstimator(self._user_state_store)
            self._user_profile_worker = UserProfileWorker(
                ollama=self._ollama,
                db=self._chat_db,
                store=self._user_profile_store,
                model=self._effective_worker_model,
                min_user_turns=settings.agent.user_profile_min_turns,
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("user-profile / user-state init failed", exc_info=True)
            self._user_profile_store = None
            self._user_profile_worker = None
            self._user_state_store = None
            self._user_state_estimator = None

        # Phase 4b: memory consolidator (cluster + merge near-cosine groups).
        self._consolidator = None
        if (
            settings.agent.consolidator_enabled
            and self._memory_store is not None
        ):
            try:
                from app.core.memory.memory_consolidator import MemoryConsolidator

                self._consolidator = MemoryConsolidator(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    chat_db=self._chat_db,
                    model=self._effective_worker_model,
                    chunk_size=settings.agent.consolidator_chunk_size,
                    similarity_threshold=settings.agent.consolidator_similarity_threshold,
                    min_cluster_size=settings.agent.consolidator_min_cluster_size,
                    min_hours_between=settings.agent.consolidator_min_hours_between,
                    use_llm_merge=settings.agent.consolidator_use_llm_merge,
                    user_display_name_provider=lambda: self.user_display_name,
                    repoint_memory_edges=self._repoint_concept_memory_edges,
                )
            except Exception:
                log.warning("MemoryConsolidator init failed", exc_info=True)
                self._consolidator = None

        # Phase 4b: weekly relationship pulse (LLM summary as self_tagged memory).
        self._relationship_pulse = None
        if (
            settings.agent.relationship_pulse_enabled
            and self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.relationship.relationship_pulse import RelationshipPulseWorker

                self._relationship_pulse = RelationshipPulseWorker(
                    ollama=self._ollama,
                    memory_store=self._memory_store,
                    relationship_store=getattr(self, "_relationship_store", None),
                    chat_db=self._chat_db,
                    embedder=self._embedder,
                    model=self._effective_worker_model,
                    min_hours=settings.agent.relationship_pulse_min_hours,
                    min_turns=settings.agent.relationship_pulse_min_turns,
                    max_tokens=settings.agent.relationship_pulse_max_tokens,
                    user_display_name_provider=lambda: self.user_display_name,
                )
            except Exception:
                log.warning("RelationshipPulseWorker init failed", exc_info=True)
                self._relationship_pulse = None

        # Schema v7: shared moments + relationship axes. Both are cheap;
        # the LLM detector is the only place we'd burn an extra call and
        # it's gated tightly (see ``_maybe_schedule_moment_llm_job``).
        self._shared_moments_store = None
        self._moment_detector = None
        if (
            settings.agent.shared_moments_enabled
            and self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.relationship.shared_moments import SharedMomentsStore

                self._shared_moments_store = SharedMomentsStore(
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                )
            except Exception:
                log.warning("SharedMomentsStore init failed", exc_info=True)
                self._shared_moments_store = None

        # F2 personality backlog: knowledge-gap journal. Cheap — pure
        # regex + a dedicated MemoryStore wrapper, no LLM. Wired
        # whenever long-term memory is available so the [[gap:...]]
        # extraction path always has somewhere to write.
        self._knowledge_gap_store = None
        if (
            self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.memory.knowledge_gap_extractor import KnowledgeGapStore

                self._knowledge_gap_store = KnowledgeGapStore(
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                )
            except Exception:
                log.warning("KnowledgeGapStore init failed", exc_info=True)
                self._knowledge_gap_store = None

        # K1 personality backlog: long-term goals journal. Cheap —
        # pure self-tag parsing + a dedicated MemoryStore wrapper,
        # the LLM-driven reflection runs out-of-band in
        # :class:`GoalWorker`. Wired whenever long-term memory is
        # available so the [[goal:...]] extraction path always has
        # somewhere to write, even when the worker is disabled.
        self._goal_store = None
        if (
            self._memory_store is not None
            and self._embedder is not None
        ):
            try:
                from app.core.goals.goal_store import GoalStore

                self._goal_store = GoalStore(
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    max_active=int(getattr(
                        settings.memory, "goal_max_active", 5,
                    )),
                    max_progress_per_goal=int(getattr(
                        settings.memory,
                        "goal_max_progress_per_goal",
                        12,
                    )),
                )
            except Exception:
                log.warning("GoalStore init failed", exc_info=True)
                self._goal_store = None
            # K1: tell the RAG retriever about the goal store so its
            # per-hit goal-alignment bonus has the active vectors to
            # check against. The retriever was constructed earlier in
            # the bootstrap; this hooks the dependency up after both
            # exist (the retriever's setter is None-safe).
            if (
                self._goal_store is not None
                and getattr(self, "_rag_retriever", None) is not None
                and hasattr(self._rag_retriever, "set_goal_store")
            ):
                try:
                    self._rag_retriever.set_goal_store(self._goal_store)
                except Exception:
                    log.debug(
                        "RagRetriever set_goal_store failed", exc_info=True,
                    )

        # F1 personality backlog: persistent claim queue + cancellation
        # event. The queue is enqueued from the ``_notify_memory_added``
        # path so every memory write site automatically feeds it. The
        # IdleFactChecker worker (registered below alongside decay /
        # promotion) drains it on the idle scheduler.
        self._fact_check_queue = None
        self._fact_check_cancel: threading.Event | None = None
        if (
            self._memory_store is not None
            and bool(getattr(settings.agent, "fact_checker_enabled", True))
        ):
            try:
                from app.core.memory.fact_check_queue import FactCheckQueue

                self._fact_check_queue = FactCheckQueue(self._chat_db)
            except Exception:
                log.warning("FactCheckQueue init failed", exc_info=True)
                self._fact_check_queue = None
            try:
                self._fact_check_cancel = threading.Event()
            except Exception:
                self._fact_check_cancel = None

        if (
            self._shared_moments_store is not None
            and settings.agent.shared_moments_llm_enabled
        ):
            try:
                from app.core.relationship.shared_moment_extractor import MomentDetector

                def _persist_moment_candidate(candidate: Any) -> None:
                    store = self._shared_moments_store
                    if store is None:
                        return
                    row = store.add_from_candidate(
                        candidate,
                        source_session=self.session_key,
                    )
                    if row is not None:
                        self._notify_shared_moment_added(row)

                self._moment_detector = MomentDetector(
                    ollama=self._ollama,
                    model=self._effective_worker_model,
                    persist_callback=_persist_moment_candidate,
                    min_turn_gap=settings.agent.shared_moments_min_turn_gap,
                    cooldown_seconds=settings.agent.shared_moments_cooldown_seconds,
                    user_display_name_provider=lambda: self.user_display_name,
                )
            except Exception:
                log.warning("MomentDetector init failed", exc_info=True)
                self._moment_detector = None

        self._relationship_axes_store = None
        self._relationship_axes_updater = None
        if settings.agent.relationship_axes_enabled:
            try:
                from app.core.relationship.relationship_axes import (
                    RelationshipAxesStore,
                    RelationshipAxesUpdater,
                )

                self._relationship_axes_store = RelationshipAxesStore(self._chat_db)
                self._relationship_axes_updater = RelationshipAxesUpdater(
                    self._relationship_axes_store,
                )
            except Exception:
                log.warning("RelationshipAxes init failed", exc_info=True)
                self._relationship_axes_store = None
                self._relationship_axes_updater = None

        # K31 soft physicality: TouchService state machine. Constructed
        # AFTER ``_relationship_axes_store`` so the dispatch path can
        # read live axes for the per-kind gate. Always built (even when
        # ``touch_enabled=False``) so the persisted cooldown state
        # survives a settings flap without resetting.
        self._touch_service = None
        try:
            from app.core.touch.touch_gestures import TouchService

            self._touch_service = TouchService(
                chat_db=self._chat_db,
                settings=settings.agent,
            )
        except Exception:
            log.warning("TouchService init failed", exc_info=True)
            self._touch_service = None

        # Listeners for the REST/WS layer. Shared moments fire on create
        # and on every edit/delete; axes fire only when an axis crosses a
        # 0.05 step (debounced server-side — see ``set_user_present`` /
        # the axes update path).
        self._shared_moment_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        self._relationship_axes_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        # K68 embodied vitality: listeners fire when the body-energy
        # scalar moves enough to matter (post-turn spend/boost or the
        # idle-worker recovery tick). Patches carry ``energy`` +
        # ``expressiveness_mult`` + ``band``; the WS hub broadcasts as
        # ``vitality_changed`` so the avatar visibly droops / perks.
        self._vitality_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        # Last broadcast energy, for the 0.03-step debounce so a noisy
        # chat doesn't flood the WS with micro-movements.
        self._vitality_last_broadcast: float | None = None
        # F2 personality backlog: knowledge-gap listeners fire on create,
        # on resolve, and on delete. Patches carry ``gap`` (full row dict)
        # or ``deleted_gap_id``. WS hub broadcasts as
        # ``knowledge_gap_updated``.
        self._knowledge_gap_listeners: list[
            Callable[[dict[str, Any]], None]
        ] = []
        self._axes_last_broadcast: dict[str, float] = {
            "closeness": 0.0,
            "humor": 0.0,
            "trust": 0.0,
            "comfort": 0.0,
        }

        # Per-turn cache: was a moment created on the most recent turn?
        # Used to feed the axes updater the moment-vibes list without
        # re-querying the store, and to decide whether to render the
        # anniversary block on the *next* turn (a moment created right
        # now isn't an anniversary today).
        self._last_turn_moment_vibes: list[str] = []
        self._last_turn_milestone: str | None = None
        # J8: one-shot milestone-celebration slot; armed post-turn when a
        # milestone crosses, consumed by _render_milestone_block next turn.
        self._pending_milestone_celebration: str | None = None
        # J4: cached bond stage for the hysteresis band (axes + tenure).
        self._last_relationship_stage: str | None = None
        # J5: created_at of the assistant message the reconnection cue last
        # greeted from, so the same return isn't re-greeted before Aiko's
        # reply collapses the gap.
        self._reconnection_anchored_at: str | None = None
        # K-time4: session-clock watermarks. ``_burst_key`` identifies the
        # current continuous sitting (re-arms the elapsed cue when it
        # changes); ``_fired_band`` is the strongest elapsed band already
        # surfaced this sitting; ``_gap_anchor`` is the latest-message ts
        # the mid-session pause cue last fired from.
        self._session_clock_burst_key: str | None = None
        self._session_clock_fired_band: str | None = None
        self._session_clock_gap_anchor: str | None = None
        self._last_turn_promise_kept: bool = False
        self._last_turn_gift_received: bool = False
        # Wire all hot-path providers (each cheap: SQL/mirror reads or
        # pure functions). Token accounting runs through PromptTelemetry.
        self._prompt_assembler.set_inner_life_providers(
            affect=self._render_affect_block,
            vitality=self._render_vitality_block,
            circadian=self._render_circadian_block,
            day_color=self._render_day_color_block,
            vulnerability_budget=self._render_vulnerability_budget_block,
            profile=self._render_user_profile_block,
            user_state=self._render_user_state_block,
            relationship=self._render_relationship_block,
            agenda=self._render_agenda_block,
            goals=self._render_goals_block,
            # interest_map + concept now flow through the T3 relevant_context
            # region (set_relevant_context_provider), turn-relevance scored
            # under the unified budget -- no longer wired as standalone T1
            # blocks. Only the L4 coactivation block stays here.
            coactivation=self._render_coactivation_block,
            coactivation_trace=(
                lambda: getattr(self, "_coactivation_block_trace", None)
            ),
            arc=self._render_arc_block,
            narrative=self._render_narrative_block,
            vocal_tone=self._render_vocal_tone_block,
            catchphrase=self._render_catchphrase_block,
            voice_adoption=self._render_voice_adoption_block,
            petname=self._render_petname_block,
            ambient_noise=self._render_ambient_noise_block,
            avatar_capabilities=self._avatar_capabilities,
            pajama=self._render_pajama_block,
            motion_names=self._avatar_motion_names,
            world=self._render_world_block,
            activity=self._render_activity_block,
            weather=self._render_weather_block,
            hobby=self._render_hobby_block,
            anniversary=self._render_anniversary_block,
            milestone=self._render_milestone_block,
            axes=self._render_axes_block,
            knowledge_gaps=self._render_knowledge_gaps_block,
            knowledge_gap_notice=self._render_knowledge_gap_notice_block,
            associative_wander=self._render_associative_wander_block,
            long_arc_callback=self._render_long_arc_callback_block,
            interest_drift=self._render_interest_drift_block,
            dormant_interest=self._render_dormant_interest_block,
            curiosity_gradient=self._render_curiosity_gradient_block,
            topic_temperature=self._render_topic_temperature_block,
            topic_confidence=self._render_topic_confidence_block,
            earned_familiarity=self._render_earned_familiarity_block,
            user_expertise=self._render_user_expertise_block,
            knowledge_grounding=self._render_knowledge_grounding_block,
            belief_gaps=self._render_belief_gaps_block,
            clarification=self._render_clarification_block,
            inside_joke=self._render_inside_joke_block,
            calibration=self._render_calibration_block,
            sensory_anchor=self._render_sensory_anchor_block,
            rupture=self._render_rupture_block,
            mood_inertia=self._render_mood_inertia_block,
            mood_drift=self._render_mood_drift_block,
            self_correction=self._render_self_correction_block,
            user_correction=self._render_user_correction_block,
            fact_reversal=self._render_fact_reversal_block,
            promise_followthrough=self._render_promise_followthrough_block,
            misattunement=self._render_misattunement_block,
            implicit_need=self._render_implicit_need_block,
            opinion_injection=self._render_opinion_injection_block,
            boundary_clash=self._render_boundary_clash_block,
            stance_persistence=self._render_stance_persistence_block,
            absence_curiosity=self._render_absence_curiosity_block,
            reconnection=self._render_reconnection_block,
            session_clock=self._render_session_clock_block,
            appreciation=self._render_appreciation_block,
            reciprocal_vulnerability=self._render_reciprocal_vulnerability_block,
            turning_over=self._render_turning_over_block,
            sleep_return=self._render_sleep_return_block,
            away_activities=self._render_away_activities_block,
            caught_mid_activity=self._render_caught_mid_activity_block,
            forward_curiosity=self._render_forward_curiosity_block,
            concept_hypothesis=self._render_concept_hypothesis_block,
            follow_up=self._render_follow_up_block,
            growth_witness=self._render_growth_witness_block,
            self_callback=self._render_self_callback_block,
            aspiration_momentum=self._render_aspiration_momentum_block,
            tension=self._render_tension_block,
            wellbeing_concern=self._render_wellbeing_concern_block,
            shared_ritual=self._render_shared_ritual_block,
            upcoming_horizon=self._render_upcoming_horizon_block,
            mood_shell=self._render_mood_shell_block,
            intimacy_pacing=self._render_intimacy_pacing_block,
            novelty=self._render_novelty_block,
            stagnation=self._render_stagnation_block,
            style_pattern=self._render_style_pattern_block,
            question_balance=self._render_question_balance_block,
            tease_rhythm=self._render_tease_rhythm_block,
            style_signal=self._render_style_signal_block,
            self_noticing=self._render_self_noticing_block,
            curiosity_seeds=self._render_curiosity_seeds_block,
            idle_seeds=self._render_idle_seed_block,
            wants=self._render_wants_block,
            initiative=self._render_initiative_block,
            thread_ownership=self._render_thread_ownership_block,
            topic_appetite=self._render_topic_appetite_block,
            taste_lean=self._render_taste_lean_block,
            pursuit_lean=self._render_pursuit_lean_block,
            conduct_notice=self._render_conduct_notice_block,
            concept_learning=self._render_concept_learning_block,
            emotion_episode=self._render_emotion_episode_block,
            tease_ledger=self._render_tease_collection_block,
            grounding_line=self._render_grounding_line,
            user_reactions=self._render_user_reactions_block,
            # B7: the touch budget cue (``touch_state``) was retired —
            # gating is gone, so there's no physical budget to surface.
            attachments=self._render_attachments_block,
            seen_image=self._render_seen_image_block,
        )
        # Unified context budget: wire the T3 relevant_context region builder
        # + its sizing knobs. The builder gathers turn-relevance-scored
        # memories / clusters / concepts and fills the reserved surfacing
        # budget; the assembler reserves that budget before packing history.
        try:
            self._prompt_assembler.set_relevant_context_provider(
                self.build_relevant_context,
            )
            _ms = getattr(self, "_memory_settings", None)
            if _ms is not None:
                self._prompt_assembler.set_context_budget_sizing(
                    enabled=bool(getattr(_ms, "context_budget_enabled", True)),
                    fraction=float(getattr(_ms, "context_budget_fraction", 0.15)),
                    max_tokens=int(getattr(_ms, "context_budget_max_tokens", 4096)),
                    min_tokens=int(getattr(_ms, "context_budget_min_tokens", 256)),
                    history_floor_tokens=int(
                        getattr(_ms, "context_budget_history_floor_tokens", 1024)
                    ),
                )
        except Exception:
            log.debug("relevant_context wiring failed", exc_info=True)
        # K16: register the grounding-line mode so the assembler knows
        # which granular blocks to suppress on each turn. Idempotent;
        # safe to re-call on settings reload.
        try:
            self._prompt_assembler.set_grounding_line_mode(
                getattr(self._settings.agent, "grounding_line_mode", "off"),
            )
        except Exception:
            log.debug("grounding_line_mode setter failed", exc_info=True)

        # Phase 5b: feed the prosody dispatcher live affect/circadian.
        prosody = getattr(self, "_prosody", None)
        if prosody is not None:
            try:
                prosody.set_context_provider(self._cadence_context)
            except Exception:
                log.debug("prosody context provider wire failed", exc_info=True)

        if (
            self._memory_settings.enabled
            and self._memory_settings.extractor_enabled
            and self._embedder is not None
            and self._memory_store is not None
        ):
            try:
                self._memory_extractor = MemoryExtractor(
                    self._chat_db,
                    self._memory_store,
                    self._embedder,
                    self._ollama,
                    model=self._effective_worker_model,
                    max_tokens=self._memory_settings.memory_extractor_max_tokens,
                    think=self._memory_settings.memory_extractor_think,
                    min_window_messages=(
                        self._memory_settings.memory_extractor_min_new
                    ),
                    max_window_messages=(
                        self._memory_settings.memory_extractor_max_window
                    ),
                    context_messages=(
                        self._memory_settings.memory_extractor_context_messages
                    ),
                    user_display_name_provider=lambda: self.user_display_name,
                )
                self._memory_extractor.add_listener(self._notify_memory_added)
            except Exception:
                log.warning("memory extractor failed to initialise", exc_info=True)
                self._memory_extractor = None

    def _build_concept_contradiction_detector(self, settings):
        """L9: build the read-only ``ConceptContradictionDetector`` for the
        L3 lifecycle worker, or ``None`` when disabled / missing deps.

        Uses its own :class:`FactCheckRateLimiter`
        (``state_key='concept_contradiction.rate_state'``) so its LLM
        budget never shares with F5, and the maintenance-tier LLM client
        like every other idle worker. Non-fatal: any failure returns
        ``None`` and the worker degrades to pure lifecycle math.
        """
        self._concept_contradiction_rate_limiter = None
        if not bool(
            getattr(
                self._memory_settings, "concept_contradiction_enabled", True
            )
        ):
            return None
        if (
            getattr(self, "_memory_store", None) is None
            or getattr(self, "_maintenance_client", None) is None
            or getattr(self, "_chat_db", None) is None
        ):
            return None
        try:
            from app.core.concepts.concept_contradiction import (
                ConceptContradictionDetector,
            )
            from app.core.memory.fact_check_rate_limiter import (
                FactCheckRateLimiter,
            )

            self._concept_contradiction_rate_limiter = FactCheckRateLimiter(
                self._chat_db,
                per_hour_cap=int(
                    getattr(
                        settings.agent,
                        "concept_contradiction_per_hour_cap",
                        6,
                    )
                ),
                per_day_cap=int(
                    getattr(
                        settings.agent,
                        "concept_contradiction_per_day_cap",
                        30,
                    )
                ),
                state_key="concept_contradiction.rate_state",
            )
            return ConceptContradictionDetector(
                memory_store=self._memory_store,
                # Idle-scheduler worker → maintenance tier.
                ollama=self._maintenance_client,
                chat_model=self._effective_worker_model,
                rate_limiter=self._concept_contradiction_rate_limiter,
                cancel_event=self._fact_check_cancel,
                similarity_min=float(
                    getattr(
                        self._memory_settings,
                        "concept_contradiction_similarity_min",
                        0.6,
                    )
                ),
                similarity_max=float(
                    getattr(
                        self._memory_settings,
                        "concept_contradiction_similarity_max",
                        0.95,
                    )
                ),
                max_candidates=int(
                    getattr(
                        self._memory_settings,
                        "concept_contradiction_max_candidates",
                        6,
                    )
                ),
            )
        except Exception:
            log.warning(
                "ConceptContradictionDetector boot failed", exc_info=True
            )
            self._concept_contradiction_rate_limiter = None
            return None

    def _seed_pursuits(self, settings: AppSettings) -> None:
        """K85d: file the authored starter pursuits once, as candidates.

        Runs at boot rather than inside the synthesis pass because it is
        not synthesis -- nothing is being inferred, and the pass may not
        run for hours on a fresh install, which is precisely the window
        the seeds exist to cover. Watermarked in ``kv_meta``, so this is
        one kv read per boot after the first.
        """
        if not bool(
            getattr(settings.agent, "pursuit_seeds_enabled", True)
        ):
            return
        try:
            from app.core.concepts.pursuit_seeds import seed_pursuits

            seed_pursuits(
                self._concept_store,
                self._embedder,
                kv_get=self._chat_db.kv_get,
                kv_set=self._chat_db.kv_set,
            )
        except Exception:
            log.warning("pursuit seeding failed", exc_info=True)

    def _repoint_concept_memory_edges(self, old_id: int, new_id: int) -> int:
        """L25: repoint hook passed to the ``MemoryConsolidator``. Resolves
        the edge reconciler lazily (it's built later in init than the
        consolidator) and moves a hard-deleted victim's concept edges onto
        the surviving memory. A no-op (returns 0) when the concept layer
        isn't wired."""
        reconciler = getattr(self, "_concept_edge_reconciler", None)
        if reconciler is None:
            return 0
        try:
            return int(reconciler.repoint(int(old_id), int(new_id)))
        except Exception:
            log.debug("concept edge repoint hook failed", exc_info=True)
            return 0

    def _build_concept_belief_reviser(self, settings):
        """L15: build the ``ConceptBeliefReviser`` for the L3 lifecycle
        worker, or ``None`` when disabled / missing deps.

        Uses its own :class:`FactCheckRateLimiter`
        (``state_key='concept_belief_revision.rate_state'``) so its LLM
        budget never shares with L9 / F5, and the maintenance-tier LLM
        client like every other idle worker. Writes *memory* state only;
        non-fatal on any failure (returns ``None``, and the L3 worker
        degrades to the L9-only path).
        """
        self._concept_belief_revision_rate_limiter = None
        if not bool(
            getattr(
                self._memory_settings,
                "concept_belief_revision_enabled",
                True,
            )
        ):
            return None
        if (
            getattr(self, "_memory_store", None) is None
            or getattr(self, "_concept_store", None) is None
            or getattr(self, "_maintenance_client", None) is None
            or getattr(self, "_chat_db", None) is None
        ):
            return None
        try:
            from app.core.concepts.concept_belief_reviser import (
                ConceptBeliefReviser,
            )
            from app.core.memory.fact_check_rate_limiter import (
                FactCheckRateLimiter,
            )

            self._concept_belief_revision_rate_limiter = FactCheckRateLimiter(
                self._chat_db,
                per_hour_cap=int(
                    getattr(
                        settings.agent,
                        "concept_belief_revision_per_hour_cap",
                        6,
                    )
                ),
                per_day_cap=int(
                    getattr(
                        settings.agent,
                        "concept_belief_revision_per_day_cap",
                        30,
                    )
                ),
                state_key="concept_belief_revision.rate_state",
            )
            return ConceptBeliefReviser(
                concept_store=self._concept_store,
                memory_store=self._memory_store,
                # Idle-scheduler worker → maintenance tier.
                ollama=self._maintenance_client,
                chat_model=self._effective_worker_model,
                rate_limiter=self._concept_belief_revision_rate_limiter,
                cancel_event=self._fact_check_cancel,
                max_evidence=int(
                    getattr(
                        self._memory_settings,
                        "concept_belief_revision_max_evidence",
                        6,
                    )
                ),
                confidence_penalty=float(
                    getattr(
                        self._memory_settings,
                        "concept_belief_revision_confidence_penalty",
                        0.2,
                    )
                ),
                confidence_floor=float(
                    getattr(
                        self._memory_settings,
                        "concept_belief_revision_confidence_floor",
                        0.2,
                    )
                ),
                superseded_relevance_days=float(
                    getattr(
                        self._memory_settings,
                        "concept_belief_revision_superseded_relevance_days",
                        7.0,
                    )
                ),
                notify_memory_updated=self._notify_memory_updated,
            )
        except Exception:
            log.warning(
                "ConceptBeliefReviser boot failed", exc_info=True
            )
            self._concept_belief_revision_rate_limiter = None
            return None

    def _init_speaking_window(self, settings: AppSettings) -> None:
        self._scheduler = SpeakingWindowScheduler(
            speaking_window_grace_ms=settings.agent.scheduler_speaking_window_grace_ms,
            max_job_seconds=settings.agent.scheduler_max_job_seconds,
            idle_seconds=settings.agent.scheduler_idle_seconds,
            is_quiet=lambda: not self._turn_in_progress,
        )
        self._scheduler.start_idle_loop()

        self._summary_worker = SummaryWorker(
            self._chat_db,
            self._ollama,
            model=self._effective_worker_model,
            is_busy=lambda: self._turn_in_progress,
            idle_seconds=settings.agent.summary_idle_seconds,
            min_unsummarized_messages=settings.agent.summary_min_unsummarized_messages,
            target_tokens=settings.agent.summary_target_tokens,
            memory_extractor=self._memory_extractor,
        )
        self._summary_worker.start()
        # Schema v8 — background workers run through a single shared
        # :class:`IdleWorkerScheduler` instead of a dedicated decay
        # thread. The scheduler skips during Live mode + within the
        # configured quiet threshold of any user activity (see
        # :meth:`_is_user_idle`). New workers (memory promotion,
        # wall-clock decay, future F1/G2/G3) register here.
        self._last_user_activity_at: float = time.monotonic()
        # Shared engagement clock: a monotonic "active-conversation time"
        # counter (kv_meta-backed) that the memory decay worker + L3
        # concept lifecycle engine drive their decay off, so absence /
        # quiet stretches don't crater confidence. Built early so the
        # post-turn hook can credit turns; ``None`` when there's no db.
        self._engagement_clock = None
        if self._chat_db is not None:
            try:
                from app.core.infra.engagement_clock import EngagementClock

                self._engagement_clock = EngagementClock(
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    settings=self._memory_settings,
                )
            except Exception:
                log.warning("EngagementClock init failed", exc_info=True)
                self._engagement_clock = None
        self._idle_scheduler: "IdleWorkerScheduler | None" = None
        if self._memory_store is not None and self._memory_settings.tiers_enabled:
            try:
                from app.core.proactive.idle_worker_scheduler import IdleWorkerScheduler
                from app.core.memory.memory_decay_worker import MemoryDecayWorker
                from app.core.memory.memory_promotion_worker import (
                    MemoryPromotionWorker,
                )

                mem = self._memory_settings
                self._idle_scheduler = IdleWorkerScheduler(
                    wake_seconds=mem.idle_worker_wake_seconds,
                    is_quiet_callback=self._is_user_idle,
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    tick_budget_ms=mem.idle_worker_tick_budget_ms,
                    max_per_tick=mem.idle_worker_max_per_tick,
                    compute_budget_ms=mem.idle_worker_compute_budget_ms,
                    pressure_enabled=mem.idle_worker_pressure_enabled,
                    urgency_threshold=mem.idle_worker_urgency_threshold,
                    min_interval_ratio=mem.idle_worker_min_interval_ratio,
                    depth_max_multiplier=mem.idle_worker_depth_max_multiplier,
                    idle_depth_provider=self._idle_depth_seconds,
                    contention_provider=self._llm_contention_grade,
                )
                self._idle_scheduler.register(
                    MemoryPromotionWorker(self._memory_store, self._memory_settings)
                )
                self._idle_scheduler.register(
                    MemoryDecayWorker(
                        self._memory_store,
                        self._memory_settings,
                        knowledge_gap_store=getattr(
                            self, "_knowledge_gap_store", None
                        ),
                        engagement_clock=self._engagement_clock,
                        kv_get=self._chat_db.kv_get,
                        kv_set=self._chat_db.kv_set,
                    )
                )
                # K27 — daily personality colour roll. Cheap (hourly
                # kv_get + date compare; writes only on local-date
                # rollover). Registered immediately after the memory
                # workers so it shares their quiet-window gate. The
                # provider has a lazy fallback for the first-turn-
                # after-midnight case when this worker hasn't fired
                # yet -- see _render_day_color_block.
                if bool(
                    getattr(settings.agent, "day_color_enabled", True)
                ):
                    try:
                        from app.core.affect.day_color_worker import (
                            DayColorWorker,
                        )

                        self._idle_scheduler.register(
                            DayColorWorker(
                                chat_db=self._chat_db,
                                settings=settings.agent,
                            )
                        )
                    except Exception:
                        log.warning(
                            "day_color worker registration failed",
                            exc_info=True,
                        )
                # K68 — embodied-vitality idle recovery. Relaxes the
                # body-energy scalar toward the circadian baseline during
                # quiet windows and broadcasts so the avatar visibly
                # droops while she's left alone. The provider has a lazy
                # recovery fallback for the next-turn case, same hybrid
                # design as K27.
                if bool(getattr(settings.agent, "vitality_enabled", True)):
                    try:
                        from app.core.affect.vitality_worker import (
                            VitalityWorker,
                        )

                        self._idle_scheduler.register(
                            VitalityWorker(
                                chat_db=self._chat_db,
                                agent_settings=settings.agent,
                                memory_settings=self._memory_settings,
                                notify=self._notify_vitality,
                            )
                        )
                    except Exception:
                        log.warning(
                            "vitality worker registration failed",
                            exc_info=True,
                        )
                # H3 — mood-drift daily sampler. Records one (valence +
                # four axes) point per local day into the kv ring the
                # provider reads. Cheap (a date compare on the no-op tick).
                # The provider has a lazy-sample fallback for the starved-
                # scheduler case, same hybrid design as K27.
                if bool(
                    getattr(settings.agent, "mood_drift_enabled", True)
                ):
                    try:
                        from app.core.affect.mood_drift_worker import (
                            MoodDriftSampleWorker,
                        )

                        self._idle_scheduler.register(
                            MoodDriftSampleWorker(
                                chat_db=self._chat_db,
                                settings=settings.agent,
                                affect_store=self._affect_store,
                                axes_store=getattr(
                                    self, "_relationship_axes_store", None,
                                ),
                                user_id=self._user_id,
                            )
                        )
                    except Exception:
                        log.warning(
                            "mood_drift worker registration failed",
                            exc_info=True,
                        )
                # J11 — affection-style slow decay toward uniform. Cheap
                # (6h cadence; a no-op read when nothing's been learned
                # or no time has elapsed). The only path that moves the
                # weights back toward uniform — per-turn learning only
                # ever moves them away. Shares the quiet-window gate.
                if bool(
                    getattr(settings.agent, "affection_style_enabled", True)
                ):
                    try:
                        from app.core.relationship.affection_style_worker import (
                            AffectionStyleDecayWorker,
                        )

                        self._idle_scheduler.register(
                            AffectionStyleDecayWorker(
                                chat_db=self._chat_db,
                                settings=settings.agent,
                            )
                        )
                    except Exception:
                        log.warning(
                            "affection_style worker registration failed",
                            exc_info=True,
                        )
                # K74 — humor-style decay worker (sibling of J11's). The
                # only path that moves the humour-register weights back
                # toward uniform.
                if bool(
                    getattr(settings.agent, "humor_style_enabled", True)
                ):
                    try:
                        from app.core.relationship.humor_style_worker import (
                            HumorStyleDecayWorker,
                        )

                        self._idle_scheduler.register(
                            HumorStyleDecayWorker(
                                chat_db=self._chat_db,
                                settings=settings.agent,
                            )
                        )
                    except Exception:
                        log.warning(
                            "humor_style worker registration failed",
                            exc_info=True,
                        )
                # F1 — background fact-checker. Registered last because
                # it depends on the knowledge-gap store (created above)
                # and the (lazy) web-search helper. Failures here only
                # drop fact-checking; the rest of the scheduler stays.
                self._idle_fact_checker = None
                self._fact_check_rate_limiter = None
                if (
                    self._fact_check_queue is not None
                    and self._fact_check_cancel is not None
                    and bool(getattr(settings.agent, "fact_checker_enabled", True))
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.memory.idle_fact_checker import IdleFactChecker
                        from app.llm.tools.builtins import WebSearchTool

                        try:
                            web_search_tool = WebSearchTool(
                                provider=self._get_search_provider()
                            )
                            self._register_search_consumer(web_search_tool)
                        except Exception:
                            log.info(
                                "fact-checker disabled: web_search tool "
                                "unavailable (duckduckgo-search missing?)"
                            )
                            web_search_tool = None
                        if web_search_tool is not None:
                            self._fact_check_rate_limiter = FactCheckRateLimiter(
                                self._chat_db,
                                per_hour_cap=int(
                                    getattr(
                                        settings.agent,
                                        "fact_checker_per_hour_cap",
                                        10,
                                    )
                                ),
                                per_day_cap=int(
                                    getattr(
                                        settings.agent,
                                        "fact_checker_per_day_cap",
                                        50,
                                    )
                                ),
                            )
                            self._idle_fact_checker = IdleFactChecker(
                                queue=self._fact_check_queue,
                                memory_store=self._memory_store,
                                agent_settings=settings.agent,
                                memory_settings=self._memory_settings,
                                # Idle-scheduler worker → maintenance tier
                                # on the worker-LLM priority gate, so it
                                # yields to per-turn conversation workers.
                                ollama=self._maintenance_client,
                                chat_model=self._effective_worker_model,
                                web_search_tool=web_search_tool,
                                rate_limiter=self._fact_check_rate_limiter,
                                cancel_event=self._fact_check_cancel,
                                knowledge_gap_store=getattr(
                                    self, "_knowledge_gap_store", None
                                ),
                                embedder=self._embedder,
                                notify_memory_updated=self._notify_memory_updated,
                                # Privacy gate inputs — late-bound so a
                                # mid-session rename of the user (or
                                # the assistant) is picked up on the
                                # next tick.
                                user_names_provider=self._fact_check_user_names,
                                assistant_name_provider=self._fact_check_assistant_name,
                                query_reformulator=self._build_query_reformulator(),
                                # F14: arm a next-turn reversal cue when the
                                # worker's own research reverses a claim Aiko
                                # surfaced. Both late-bound so the L37 ledger
                                # need not exist yet at construction.
                                arm_reversal=self.queue_fact_reversal_cue,
                                was_surfaced=self._fact_reversal_was_surfaced,
                            )
                            self._idle_scheduler.register(self._idle_fact_checker)
                    except Exception:
                        log.warning(
                            "IdleFactChecker boot failed", exc_info=True
                        )
                        self._idle_fact_checker = None

                # G3 — idle curiosity worker. Picks Aiko's existing
                # ``open_question`` memories one at a time, web-searches
                # them, and writes the answer back as a
                # ``curiosity_finding`` memory. Reuses the F1 fact-
                # checker's ``WebSearchTool`` instance and cancel event
                # so a starting turn aborts both workers cleanly. The
                # rate limiter is a *separate* ``FactCheckRateLimiter``
                # instance keyed on ``"idle_curiosity.rate_state"`` so
                # the two web-search budgets don't share counters.
                self._idle_curiosity = None
                self._idle_curiosity_rate_limiter = None
                if (
                    self._fact_check_cancel is not None
                    and self._embedder is not None
                    and bool(
                        getattr(
                            settings.agent, "idle_curiosity_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.proactive.idle_curiosity_worker import (
                            IdleCuriosityWorker,
                        )
                        from app.llm.tools.builtins import WebSearchTool

                        # ``WebSearchTool`` is a thin DDGS wrapper with
                        # no state to share between workers, so a fresh
                        # instance is fine. Build one here so the
                        # curiosity worker survives the F1 path being
                        # disabled / failing.
                        try:
                            curiosity_search_tool = WebSearchTool(
                                provider=self._get_search_provider()
                            )
                            self._register_search_consumer(curiosity_search_tool)
                        except Exception:
                            log.info(
                                "idle_curiosity disabled: web_search "
                                "tool unavailable",
                            )
                            curiosity_search_tool = None
                        if curiosity_search_tool is not None:
                            self._idle_curiosity_rate_limiter = (
                                FactCheckRateLimiter(
                                    self._chat_db,
                                    per_hour_cap=int(
                                        getattr(
                                            settings.agent,
                                            "idle_curiosity_per_hour_cap",
                                            2,
                                        )
                                    ),
                                    per_day_cap=int(
                                        getattr(
                                            settings.agent,
                                            "idle_curiosity_per_day_cap",
                                            6,
                                        )
                                    ),
                                    state_key="idle_curiosity.rate_state",
                                )
                            )
                            self._idle_curiosity = IdleCuriosityWorker(
                                memory_store=self._memory_store,
                                embedder=self._embedder,
                                # Idle-scheduler worker → maintenance tier.
                                ollama=self._maintenance_client,
                                chat_model=self._effective_worker_model,
                                web_search_tool=curiosity_search_tool,
                                rate_limiter=(
                                    self._idle_curiosity_rate_limiter
                                ),
                                cancel_event=self._fact_check_cancel,
                                agent_settings=settings.agent,
                                memory_settings=self._memory_settings,
                                user_names_provider=(
                                    self._fact_check_user_names
                                ),
                                assistant_name_provider=(
                                    self._fact_check_assistant_name
                                ),
                                notify_memory_added=(
                                    self._notify_memory_added
                                ),
                                notify_memory_updated=(
                                    self._notify_memory_updated
                                ),
                                query_reformulator=(
                                    self._build_query_reformulator()
                                ),
                            )
                            self._idle_scheduler.register(
                                self._idle_curiosity,
                            )
                    except Exception:
                        log.warning(
                            "IdleCuriosityWorker boot failed",
                            exc_info=True,
                        )
                        self._idle_curiosity = None

                # F9: IdleKnowledgeWorker. Reads the K9 topic graph,
                # picks the densest under-researched interest cluster,
                # web-searches it, and distils impersonal ``knowledge``
                # facts into the memory pool. Strictly silent (no
                # proactive message) and off the brain path. Its own
                # ``FactCheckRateLimiter`` budget keyed on
                # ``"idle_knowledge.rate_state"`` so it never shares
                # counters with F1 / G3. The topic graph is read lazily
                # via a provider so registration order vs. the K9 graph
                # boot below doesn't matter.
                self._idle_knowledge = None
                self._idle_knowledge_rate_limiter = None
                if (
                    self._fact_check_cancel is not None
                    and self._embedder is not None
                    and bool(
                        getattr(
                            settings.agent,
                            "knowledge_enrichment_enabled",
                            True,
                        )
                    )
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.proactive.idle_knowledge_worker import (
                            IdleKnowledgeWorker,
                        )
                        from app.llm.tools.builtins import WebSearchTool

                        try:
                            knowledge_search_tool = WebSearchTool(
                                provider=self._get_search_provider()
                            )
                            self._register_search_consumer(knowledge_search_tool)
                        except Exception:
                            log.info(
                                "idle_knowledge disabled: web_search "
                                "tool unavailable",
                            )
                            knowledge_search_tool = None
                        if knowledge_search_tool is not None:
                            self._idle_knowledge_rate_limiter = (
                                FactCheckRateLimiter(
                                    self._chat_db,
                                    per_hour_cap=int(
                                        getattr(
                                            settings.agent,
                                            "knowledge_enrichment_per_hour_cap",
                                            1,
                                        )
                                    ),
                                    per_day_cap=int(
                                        getattr(
                                            settings.agent,
                                            "knowledge_enrichment_per_day_cap",
                                            4,
                                        )
                                    ),
                                    state_key="idle_knowledge.rate_state",
                                )
                            )
                            self._idle_knowledge = IdleKnowledgeWorker(
                                memory_store=self._memory_store,
                                embedder=self._embedder,
                                ollama=self._maintenance_client,
                                chat_model=self._effective_worker_model,
                                web_search_tool=knowledge_search_tool,
                                rate_limiter=(
                                    self._idle_knowledge_rate_limiter
                                ),
                                cancel_event=self._fact_check_cancel,
                                agent_settings=settings.agent,
                                memory_settings=self._memory_settings,
                                topic_graph_provider=(
                                    lambda: getattr(
                                        self, "_topic_graph", None,
                                    )
                                ),
                                kv_get=self._chat_db.kv_get,
                                kv_set=self._chat_db.kv_set,
                                user_names_provider=(
                                    self._fact_check_user_names
                                ),
                                assistant_name_provider=(
                                    self._fact_check_assistant_name
                                ),
                                notify_memory_added=(
                                    self._notify_memory_added
                                ),
                                query_reformulator=(
                                    self._build_query_reformulator()
                                ),
                            )
                            self._idle_scheduler.register(
                                self._idle_knowledge,
                            )
                    except Exception:
                        log.warning(
                            "IdleKnowledgeWorker boot failed",
                            exc_info=True,
                        )
                        self._idle_knowledge = None

                # F2.1: IdleGapResolver. Closes ``knowledge_gap`` rows
                # whose answer is already living in the memory store as
                # a ``preference`` / ``fact`` / etc. Without this, the
                # gap-injection block re-asks the same question every
                # time the topic recurs because nothing else marks
                # such gaps resolved (F1 only resolves via fresh web
                # search). Failure is non-fatal — the journal stays
                # readable, gaps just won't auto-close.
                self._idle_gap_resolver = None
                if (
                    self._memory_store is not None
                    and self._knowledge_gap_store is not None
                    and bool(
                        getattr(
                            settings.agent, "gap_resolver_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.conversation.idle_gap_resolver import (
                            IdleGapResolver,
                        )

                        self._idle_gap_resolver = IdleGapResolver(
                            memory_store=self._memory_store,
                            gap_store=self._knowledge_gap_store,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            cancel_event=self._fact_check_cancel,
                            notify_memory_updated=(
                                self._notify_memory_updated
                            ),
                        )
                        self._idle_scheduler.register(
                            self._idle_gap_resolver,
                        )
                    except Exception:
                        log.warning(
                            "IdleGapResolver boot failed",
                            exc_info=True,
                        )
                        self._idle_gap_resolver = None

                # L1: higher-order concept store. Substrate only -- with
                # no proposer (L2) or lifecycle engine (L3) yet, this just
                # constructs the ConceptStore and warm-starts its cosine
                # mirror from SQLite so later L-entries have somewhere to
                # write. Opt-in via ``agent.concepts_enabled`` (default
                # off); the tables exist regardless. Non-fatal on failure.
                self._concept_store = None
                self._concept_event_store = None
                self._concept_learning_store = None
                self._evolution_diary_store = None
                self._hypothesis_store = None
                if (
                    self._chat_db is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                ):
                    try:
                        from app.core.concepts.concept_store import ConceptStore

                        self._concept_store = ConceptStore(self._chat_db)
                        self._concept_store.load_all()
                    except Exception:
                        log.warning(
                            "ConceptStore init failed; concept layer disabled",
                            exc_info=True,
                        )
                        self._concept_store = None
                    try:
                        from app.core.concepts.concept_event_store import (
                            ConceptEventStore,
                        )

                        self._concept_event_store = ConceptEventStore(
                            self._chat_db
                        )
                    except Exception:
                        log.warning(
                            "ConceptEventStore init failed; concept "
                            "discovery timeline disabled",
                            exc_info=True,
                        )
                        self._concept_event_store = None

                    # L17c: the learning-event spine + the alias map that
                    # keeps a belief's history reachable after a merge.
                    # The sink must be attached before any consolidation
                    # tick can run, because ``merge_into`` deletes the
                    # absorbed row and that is the only moment its
                    # identity still exists.
                    self._concept_learning_store = None
                    try:
                        from app.core.concepts.concept_learning_event_store import (  # noqa: E501
                            ConceptAlias,
                            ConceptLearningEventStore,
                        )

                        self._concept_learning_store = (
                            ConceptLearningEventStore(self._chat_db)
                        )
                        if self._concept_store is not None:
                            learning_store = self._concept_learning_store
                            self._concept_store.set_alias_sink(
                                lambda payload: learning_store.record_alias(
                                    ConceptAlias(**payload)
                                )
                            )
                    except Exception:
                        log.warning(
                            "ConceptLearningEventStore init failed; concept "
                            "evolution history disabled",
                            exc_info=True,
                        )
                        self._concept_learning_store = None

                    # L17f: the evolution diary's own append-only log. A
                    # plain store here; the worker that fills it is
                    # registered with the other idle workers below.
                    self._evolution_diary_store = None
                    try:
                        from app.core.concepts.evolution_diary_store import (
                            EvolutionDiaryStore,
                        )

                        self._evolution_diary_store = EvolutionDiaryStore(
                            self._chat_db
                        )
                    except Exception:
                        log.warning(
                            "EvolutionDiaryStore init failed; the evolution "
                            "diary is disabled",
                            exc_info=True,
                        )
                        self._evolution_diary_store = None

                    # L30 Phase B: the invented layer's own table. Kept
                    # beside the concept store rather than inside it --
                    # a hypothesis is something Aiko guessed, and the
                    # separation is what makes "an invention cannot reach
                    # the belief graph" true by construction instead of
                    # by every reader remembering to filter. Gated on
                    # ``concepts_enabled`` because every exit a
                    # hypothesis has leads back into the concept layer.
                    try:
                        from app.core.concepts.hypothesis_store import (
                            HypothesisStore,
                        )

                        self._hypothesis_store = HypothesisStore(self._chat_db)
                        self._hypothesis_store.load_all()
                    except Exception:
                        log.warning(
                            "HypothesisStore init failed; the invented "
                            "hypothesis layer is disabled",
                            exc_info=True,
                        )
                        self._hypothesis_store = None

                    # L5: wire the concept store into the retriever so the
                    # ``recall_concept`` tool can resolve concepts + their
                    # evidence bundle. Second-pass setter (mirrors
                    # set_topic_graph); no-op when the retriever is absent.
                    if (
                        self._concept_store is not None
                        and getattr(self, "_rag_retriever", None) is not None
                        and hasattr(self._rag_retriever, "set_concept_store")
                    ):
                        try:
                            self._rag_retriever.set_concept_store(
                                self._concept_store,
                            )
                        except Exception:
                            log.debug(
                                "RagRetriever set_concept_store failed",
                                exc_info=True,
                            )

                # K9: TopicGraph + CuriositySeedWorker. The graph is a
                # zero-cost wrapper around the in-process memory mirror;
                # the worker registers as an idle tick that proposes
                # "topics we haven't touched yet" using the graph as
                # the "we already discussed that" filter. Both are
                # opt-out via ``agent.topic_graph_enabled`` /
                # ``agent.curiosity_seed_enabled``. Failures here are
                # non-fatal: the rest of the app keeps working without
                # the seed surface.
                self._topic_graph = None
                self._curiosity_seed_worker = None
                self._topic_digest_worker = None
                if (
                    self._memory_store is not None
                    and self._embedder is not None
                    and bool(
                        getattr(
                            settings.agent, "topic_graph_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.conversation.topic_graph import TopicGraph

                        # Persisted/incremental mode (schema v20): inject a
                        # TopicClusterStore so the graph warm-starts from
                        # SQLite, assigns new memories incrementally, and
                        # only batch-refits on the idle worker — no more
                        # O(n^2) rebuild on every read. The rag_store powers
                        # the ANN batch path + ANN best_match at scale.
                        topic_cluster_store = None
                        if (
                            self._chat_db is not None
                            and bool(
                                getattr(
                                    settings.agent,
                                    "topic_graph_persistent_enabled",
                                    True,
                                )
                            )
                        ):
                            try:
                                from app.core.conversation.topic_cluster_store import (
                                    TopicClusterStore,
                                )

                                topic_cluster_store = TopicClusterStore(
                                    self._chat_db,
                                )
                            except Exception:
                                log.warning(
                                    "TopicClusterStore init failed; "
                                    "topic graph falls back to in-memory mode",
                                    exc_info=True,
                                )
                                topic_cluster_store = None

                        self._topic_graph = TopicGraph(
                            self._memory_store,
                            similarity=0.55,
                            min_cluster_size=3,
                            filter_threshold=float(
                                getattr(
                                    settings.agent,
                                    "topic_graph_filter_threshold",
                                    0.65,
                                )
                            ),
                            cluster_store=topic_cluster_store,
                            rag_store=getattr(self, "_rag_store", None),
                        )
                        # F10b: wire the topic graph into the RagRetriever so
                        # its final top-k selection can cap hits per cluster
                        # (cluster-aware diversity). Second-pass setter,
                        # mirroring set_goal_store -- the retriever is built
                        # before the graph exists. No-op on the in-memory /
                        # non-persistent path (cluster_id_for returns None).
                        if (
                            getattr(self, "_rag_retriever", None) is not None
                            and hasattr(self._rag_retriever, "set_topic_graph")
                        ):
                            try:
                                self._rag_retriever.set_topic_graph(
                                    self._topic_graph,
                                )
                            except Exception:
                                log.debug(
                                    "RagRetriever set_topic_graph failed",
                                    exc_info=True,
                                )
                        # Incremental maintenance: a new memory is assigned
                        # to the nearest cluster, a deleted one is dropped,
                        # without re-clustering the whole corpus. No-ops in
                        # the in-memory fallback mode.
                        if self._topic_graph.persistent:
                            try:
                                self._memory_store.add_memory_listener(
                                    self._topic_graph.on_memory_added,
                                )
                                self._memory_store.add_delete_listener(
                                    self._topic_graph.on_memory_deleted,
                                )
                            except Exception:
                                log.debug(
                                    "topic graph listener wiring failed",
                                    exc_info=True,
                                )
                            # Batch refit during quiet windows (periodic +
                            # pending-pressure triggered).
                            if self._idle_scheduler is not None:
                                try:
                                    from app.core.conversation.topic_graph_rebuild_worker import (
                                        TopicGraphRebuildWorker,
                                    )

                                    self._idle_scheduler.register(
                                        TopicGraphRebuildWorker(
                                            self._topic_graph,
                                            interval_seconds=float(
                                                getattr(
                                                    settings.agent,
                                                    "topic_graph_rebuild_interval_seconds",
                                                    86_400.0,
                                                )
                                            ),
                                            pending_threshold=int(
                                                getattr(
                                                    settings.agent,
                                                    "topic_graph_refit_pending_threshold",
                                                    25,
                                                )
                                            ),
                                        )
                                    )
                                except Exception:
                                    log.debug(
                                        "TopicGraphRebuildWorker register failed",
                                        exc_info=True,
                                    )

                                # F10a: name each cluster with a worker-LLM
                                # pass during quiet windows (cached in
                                # kv_meta by representative). Needs the
                                # maintenance client + a kv-backed cache.
                                if (
                                    self._chat_db is not None
                                    and self._maintenance_client is not None
                                    and self._fact_check_cancel is not None
                                    and bool(
                                        getattr(
                                            settings.agent,
                                            "topic_label_enabled",
                                            True,
                                        )
                                    )
                                ):
                                    try:
                                        from app.core.conversation.topic_label_worker import (
                                            ClusterLabelWorker,
                                        )

                                        self._idle_scheduler.register(
                                            ClusterLabelWorker(
                                                topic_graph=self._topic_graph,
                                                memory_store=self._memory_store,
                                                ollama=self._maintenance_client,
                                                chat_model=self._effective_worker_model,
                                                cancel_event=self._fact_check_cancel,
                                                agent_settings=settings.agent,
                                                kv_get=self._chat_db.kv_get,
                                                kv_set=self._chat_db.kv_set,
                                            )
                                        )
                                    except Exception:
                                        log.debug(
                                            "ClusterLabelWorker register failed",
                                            exc_info=True,
                                        )

                                # F10g: per-cluster rolling digest memory.
                                # Writes one ``topic_digest`` pool memory per
                                # dense cluster; the RAG retriever surfaces it
                                # as the coarse "what I know about X" line.
                                # Needs the maintenance client + embedder.
                                if (
                                    self._chat_db is not None
                                    and self._maintenance_client is not None
                                    and self._embedder is not None
                                    and self._fact_check_cancel is not None
                                    and bool(
                                        getattr(
                                            settings.agent,
                                            "topic_digest_enabled",
                                            True,
                                        )
                                    )
                                ):
                                    try:
                                        from app.core.conversation.topic_digest_worker import (
                                            TopicDigestWorker,
                                        )

                                        self._topic_digest_worker = TopicDigestWorker(
                                            topic_graph=self._topic_graph,
                                            memory_store=self._memory_store,
                                            embedder=self._embedder,
                                            ollama=self._maintenance_client,
                                            chat_model=self._effective_worker_model,
                                            cancel_event=self._fact_check_cancel,
                                            agent_settings=settings.agent,
                                            kv_get=self._chat_db.kv_get,
                                            kv_set=self._chat_db.kv_set,
                                            notify_memory_added=self._notify_memory_added,
                                            notify_memory_updated=self._notify_memory_updated,
                                            user_display_name_provider=(
                                                lambda: self.user_display_name
                                            ),
                                            assistant_display_name_provider=(
                                                lambda: self._fact_check_assistant_name()
                                                or "Aiko"
                                            ),
                                        )
                                        self._idle_scheduler.register(
                                            self._topic_digest_worker
                                        )
                                        # Wire the F10g digest lookup into the
                                        # retriever (second pass — the worker
                                        # exists only now).
                                        retriever = getattr(
                                            self, "_rag_retriever", None
                                        )
                                        if retriever is not None and hasattr(
                                            retriever, "set_topic_digest_provider"
                                        ):
                                            retriever.set_topic_digest_provider(
                                                self._topic_digest_worker.digest_for_cluster
                                            )
                                    except Exception:
                                        log.debug(
                                            "TopicDigestWorker register failed",
                                            exc_info=True,
                                        )
                    except Exception:
                        log.warning(
                            "TopicGraph init failed", exc_info=True,
                        )
                        self._topic_graph = None

                if (
                    self._topic_graph is not None
                    and self._fact_check_cancel is not None
                    and bool(
                        getattr(
                            settings.agent, "curiosity_seed_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.proactive.curiosity_seed_worker import (
                            CuriositySeedWorker,
                        )

                        persona_path_seed = (
                            Path(__file__).resolve().parents[3]
                            / "data" / "persona" / "aiko_companion.txt"
                        )

                        def _persona_provider() -> str:
                            try:
                                return persona_path_seed.read_text(
                                    encoding="utf-8",
                                )
                            except OSError:
                                return ""

                        def _summary_provider() -> str:
                            try:
                                row = self._chat_db.get_latest_summary(
                                    self.session_key,
                                )
                                return (row.summary if row is not None else "") or ""
                            except Exception:
                                return ""

                        def _assistant_name_provider() -> str:
                            return (
                                self._fact_check_assistant_name() or "Aiko"
                            )

                        self._curiosity_seed_worker = CuriositySeedWorker(
                            cue_store_provider=(
                                lambda: getattr(self, "_cue_store", None)
                            ),
                            topic_graph=self._topic_graph,
                            embedder=self._embedder,
                            # Idle-scheduler worker → maintenance tier.
                            ollama=self._maintenance_client,
                            chat_model=self._effective_worker_model,
                            cancel_event=self._fact_check_cancel,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            persona_provider=_persona_provider,
                            rolling_summary_provider=_summary_provider,
                            user_display_name_provider=(
                                lambda: self.user_display_name
                            ),
                            assistant_display_name_provider=(
                                _assistant_name_provider
                            ),
                            notify_cue_added=self._notify_cue_pool_added,
                        )
                        self._idle_scheduler.register(
                            self._curiosity_seed_worker,
                        )
                    except Exception:
                        log.warning(
                            "CuriositySeedWorker boot failed",
                            exc_info=True,
                        )
                        self._curiosity_seed_worker = None

                # L2: ConceptSynthesisWorker. Regular, incremental idle
                # worker that mines topic clusters (user identity) + Aiko's
                # own self/reflection/diary memories (aiko identity) for
                # candidate concepts, a small bounded batch per run. Needs
                # the L1 ConceptStore (built earlier) + the topic graph
                # (built just above). Opt-in via ``concepts_enabled`` AND
                # ``concept_synthesis_enabled``. Non-fatal on failure.
                self._concept_synthesis_worker = None
                if (
                    self._idle_scheduler is not None
                    and getattr(self, "_concept_store", None) is not None
                    and self._topic_graph is not None
                    and self._memory_store is not None
                    and self._embedder is not None
                    and self._fact_check_cancel is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                    and bool(
                        getattr(
                            settings.agent, "concept_synthesis_enabled", True,
                        )
                    )
                ):
                    try:
                        from app.core.concepts.concept_synthesis_worker import (
                            ConceptSynthesisWorker,
                        )
                        from app.core.persona.style_signal import (
                            StyleSignalStore,
                        )

                        self._concept_synthesis_worker = ConceptSynthesisWorker(
                            concept_store=self._concept_store,
                            topic_graph=self._topic_graph,
                            memory_store=self._memory_store,
                            embedder=self._embedder,
                            # Idle-scheduler worker → maintenance tier.
                            ollama=self._maintenance_client,
                            chat_model=self._effective_worker_model,
                            cancel_event=self._fact_check_cancel,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            kv_get=self._chat_db.kv_get,
                            kv_set=self._chat_db.kv_set,
                            notify_concept_added=None,
                            # Personalise concept labels/rationales with the
                            # real names instead of "the user" / "Aiko".
                            user_display_name_provider=(
                                lambda: self.user_display_name
                            ),
                            assistant_display_name_provider=(
                                lambda: self._fact_check_assistant_name()
                                or "Aiko"
                            ),
                            concept_event_store=self._concept_event_store,
                            # L23: persisted style signals for the
                            # communication-style pass (digest guidance only).
                            # StyleSignalStore is built inline -- the live
                            # analyzer/store isn't wired until detectors init,
                            # which runs after this worker.
                            user_profile_store=self._user_profile_store,
                            style_signal_store=StyleSignalStore(self._chat_db),
                            user_id_provider=(
                                lambda: getattr(self, "_user_id", "")
                                or "default"
                            ),
                            # K81: the L37 ledger store is built later (in
                            # idle-workers init), so resolve it lazily.
                            surfacing_outcome_store_provider=(
                                lambda: getattr(
                                    self, "_surfacing_outcome_store", None
                                )
                            ),
                            # L17d: her own recorded corrections, the raw
                            # material for a rule about how she works. Built
                            # above, so a direct handle is safe.
                            learning_store=self._concept_learning_store,
                            # L42: reuse indexed user-message vectors; no
                            # historical re-embedding in the weekly pass.
                            user_vector_rows_provider=(
                                lambda window_days, limit: (
                                    self._rag_store.list_recent_user_vector_rows(
                                        user_id_prefix=(
                                            getattr(self, "_user_id", "")
                                            or "default"
                                        ),
                                        since_iso=(
                                            timephrase.utcnow()
                                            - timedelta(days=window_days)
                                        ).isoformat(),
                                        limit=limit,
                                    )
                                    if getattr(self, "_rag_store", None)
                                    is not None
                                    else []
                                )
                            ),
                        )
                        self._idle_scheduler.register(
                            self._concept_synthesis_worker,
                        )
                        self._seed_pursuits(settings)
                    except Exception:
                        log.warning(
                            "ConceptSynthesisWorker boot failed",
                            exc_info=True,
                        )
                        self._concept_synthesis_worker = None

                # L45: ConceptGateTunerWorker. Calibrates the concept
                # thresholds to this relationship's own distribution instead
                # of the constants one person's graph happened to suggest.
                # Cheap-ish (no LLM, one pass over the store plus a bounded
                # cosine sample) and daily; the read-side gates it may move
                # are all read per turn, so a run takes effect without a
                # restart. Non-fatal on failure -- an absent tuner just
                # leaves every threshold at its shipped default.
                self._concept_gate_tuner = None
                if (
                    self._idle_scheduler is not None
                    and getattr(self, "_concept_store", None) is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                    and bool(
                        getattr(
                            self._memory_settings,
                            "concept_gate_tuning_enabled",
                            True,
                        )
                    )
                ):
                    try:
                        from app.core.concepts.gate_tuner_worker import (
                            ConceptGateTunerWorker,
                        )

                        self._concept_gate_tuner = ConceptGateTunerWorker(
                            concept_store=self._concept_store,
                            memory_settings=self._memory_settings,
                            agent_settings=settings.agent,
                            kv_get=self._chat_db.kv_get,
                            kv_set=self._chat_db.kv_set,
                            event_store=self._concept_event_store,
                            # The L37 ledger is built in idle-workers init,
                            # which runs after this; resolve it lazily.
                            surfacing_outcome_store_provider=(
                                lambda: getattr(
                                    self, "_surfacing_outcome_store", None
                                )
                            ),
                            topic_graph=getattr(self, "_topic_graph", None),
                        )
                        self._idle_scheduler.register(self._concept_gate_tuner)
                    except Exception:
                        log.warning(
                            "ConceptGateTunerWorker boot failed",
                            exc_info=True,
                        )
                        self._concept_gate_tuner = None

                # L3: ConceptLifecycleWorker. The single writer of concept
                # confidence / plasticity / status. Cheap (no LLM), runs a
                # small rolling batch per tick so a growing concept set
                # never blocks the scheduler; decay is engagement-driven.
                # Opt-in via ``concepts_enabled`` AND
                # ``concept_lifecycle_enabled``. Non-fatal on failure.
                self._concept_lifecycle_worker = None
                if (
                    self._idle_scheduler is not None
                    and getattr(self, "_concept_store", None) is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                    and bool(
                        getattr(
                            self._memory_settings,
                            "concept_lifecycle_enabled",
                            True,
                        )
                    )
                ):
                    try:
                        from app.core.concepts.concept_lifecycle_worker import (
                            ConceptLifecycleWorker,
                        )
                        from app.core.concepts.concept_lifecycle import (
                            RelationshipSignal,
                        )

                        # L9: optional read-only contradiction detector.
                        # Built only when enabled and the deps it needs
                        # (memory mirror + maintenance LLM + chat db) are
                        # present; the worker stays the single writer and
                        # degrades to pure lifecycle math when it's None.
                        contradiction_detector = (
                            self._build_concept_contradiction_detector(
                                settings
                            )
                        )

                        # L15: optional belief reviser. Re-examines a
                        # just-``contradicted`` concept's supporting
                        # memories and applies per-memory resolutions
                        # (inaccurate / superseded / keep). Writes only
                        # memory state; None => the worker degrades to
                        # the L9-only path.
                        belief_reviser = (
                            self._build_concept_belief_reviser(settings)
                        )

                        def _graph_mature() -> bool:
                            # L21: promotion uses the stricter young-graph
                            # bar until the graph clears the cluster floor.
                            # No graph wired => treat as mature so lean
                            # deployments keep the normal bar.
                            graph = getattr(self, "_topic_graph", None)
                            if graph is None:
                                return True
                            min_clusters = int(
                                getattr(
                                    self._memory_settings,
                                    "concept_min_clusters",
                                    6,
                                )
                            )
                            try:
                                return bool(
                                    graph.mature(min_clusters=min_clusters)
                                )
                            except Exception:
                                return True

                        # L16: live relationship signal for plasticity
                        # modulation. Trust (axes store, clamped to positive)
                        # loosens a boundary; relationship duration (days since
                        # ``first_seen_at``, saturating) adds a slower lift.
                        # Returns ``None`` when the axes store is absent (lean
                        # deployments) so the worker no-ops modulation.
                        _axes_store = getattr(
                            self, "_relationship_axes_store", None
                        )
                        _rel_store = getattr(self, "_relationship_store", None)
                        _rel_user_id = self._user_id
                        _mem_settings = self._memory_settings

                        def _relationship_signal() -> "RelationshipSignal | None":
                            if _axes_store is None:
                                return None
                            import datetime as _dt

                            try:
                                trust01 = 0.0
                                axes = _axes_store.get(_rel_user_id)
                                if axes is not None:
                                    trust01 = min(
                                        1.0,
                                        max(0.0, float(getattr(axes, "trust", 0.0))),
                                    )
                                duration01 = 0.0
                                if _rel_store is not None:
                                    rel = _rel_store.get(_rel_user_id)
                                    seen = getattr(rel, "first_seen_at", None) if rel else None
                                    if seen:
                                        try:
                                            first = _dt.datetime.fromisoformat(
                                                str(seen).replace("Z", "+00:00")
                                            )
                                            if first.tzinfo is None:
                                                first = first.replace(
                                                    tzinfo=_dt.timezone.utc
                                                )
                                            days = max(
                                                0.0,
                                                (
                                                    _dt.datetime.now(_dt.timezone.utc)
                                                    - first
                                                ).total_seconds()
                                                / 86400.0,
                                            )
                                            full = (
                                                float(
                                                    getattr(
                                                        _mem_settings,
                                                        "concept_plasticity_duration_days_full",
                                                        180.0,
                                                    )
                                                )
                                                or 180.0
                                            )
                                            duration01 = min(1.0, days / full)
                                        except (TypeError, ValueError):
                                            duration01 = 0.0
                                return RelationshipSignal(
                                    trust01=trust01, duration01=duration01
                                )
                            except Exception:
                                log.debug(
                                    "relationship signal build failed",
                                    exc_info=True,
                                )
                                return None

                        self._concept_lifecycle_worker = ConceptLifecycleWorker(
                            concept_store=self._concept_store,
                            concept_event_store=self._concept_event_store,
                            engagement_clock=self._engagement_clock,
                            graph_mature_provider=_graph_mature,
                            contradiction_detector=contradiction_detector,
                            belief_reviser=belief_reviser,
                            relationship_signal_provider=_relationship_signal,
                            surfacing_outcome_store_provider=(
                                lambda: getattr(
                                    self, "_surfacing_outcome_store", None
                                )
                            ),
                            kv_get=self._chat_db.kv_get,
                            kv_set=self._chat_db.kv_set,
                            memory_settings=self._memory_settings,
                            agent_settings=settings.agent,
                        )
                        self._idle_scheduler.register(
                            self._concept_lifecycle_worker,
                        )
                    except Exception:
                        log.warning(
                            "ConceptLifecycleWorker boot failed",
                            exc_info=True,
                        )
                        self._concept_lifecycle_worker = None

                # L2: ConceptConsolidationWorker. Retroactive near-duplicate
                # fusion -- an LLM-adjudicated merge of paraphrase-twin
                # concepts that slipped under the creation-time dedup bar,
                # plus (L46) an unadjudicated merge of the ones that drifted
                # back *over* it after birth. Needs the concept store + a
                # maintenance LLM client; opt-in via ``concepts_enabled``
                # AND ``concept_consolidation_enabled``. Its own
                # FactCheckRateLimiter budget. Non-fatal on failure.
                self._concept_consolidation_worker = None
                self._concept_consolidation_rate_limiter = None
                if (
                    self._idle_scheduler is not None
                    and getattr(self, "_concept_store", None) is not None
                    and getattr(self, "_maintenance_client", None) is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                    and bool(
                        getattr(
                            self._memory_settings,
                            "concept_consolidation_enabled",
                            True,
                        )
                    )
                ):
                    try:
                        from app.core.concepts.concept_consolidation_worker import (  # noqa: E501
                            ConceptConsolidationWorker,
                        )
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )

                        def _consolidation_graph_mature() -> bool:
                            graph = getattr(self, "_topic_graph", None)
                            if graph is None:
                                return True
                            min_clusters = int(
                                getattr(
                                    self._memory_settings,
                                    "concept_min_clusters",
                                    6,
                                )
                            )
                            try:
                                return bool(
                                    graph.mature(min_clusters=min_clusters)
                                )
                            except Exception:
                                return True

                        self._concept_consolidation_rate_limiter = (
                            FactCheckRateLimiter(
                                self._chat_db,
                                per_hour_cap=int(
                                    getattr(
                                        settings.agent,
                                        "concept_consolidation_per_hour_cap",
                                        6,
                                    )
                                ),
                                per_day_cap=int(
                                    getattr(
                                        settings.agent,
                                        "concept_consolidation_per_day_cap",
                                        30,
                                    )
                                ),
                                state_key="concept_consolidation.rate_state",
                            )
                        )
                        self._concept_consolidation_worker = (
                            ConceptConsolidationWorker(
                                concept_store=self._concept_store,
                                memory_settings=self._memory_settings,
                                agent_settings=settings.agent,
                                # Idle-scheduler worker → maintenance tier.
                                ollama=self._maintenance_client,
                                chat_model=self._effective_worker_model,
                                rate_limiter=(
                                    self._concept_consolidation_rate_limiter
                                ),
                                cancel_event=self._fact_check_cancel,
                                concept_event_store=self._concept_event_store,
                                graph_mature_provider=(
                                    _consolidation_graph_mature
                                ),
                                user_display_name_provider=(
                                    lambda: self.user_display_name
                                ),
                                assistant_display_name_provider=(
                                    lambda: self._fact_check_assistant_name()
                                    or "Aiko"
                                ),
                                # L46: the "not the same belief" verdicts
                                # persist. They used to die with the
                                # process, so every restart re-litigated
                                # the same template collisions out of a
                                # thirty-a-day LLM budget.
                                kv_get=self._chat_db.kv_get,
                                kv_set=self._chat_db.kv_set,
                            )
                        )
                        self._idle_scheduler.register(
                            self._concept_consolidation_worker,
                        )
                    except Exception:
                        log.warning(
                            "ConceptConsolidationWorker boot failed",
                            exc_info=True,
                        )
                        self._concept_consolidation_worker = None
                        self._concept_consolidation_rate_limiter = None

                # L17: ConceptDriftWorker. Applies staged relabels (it is
                # the single writer of ``label`` / ``rationale``, the way
                # L3 is the single writer of confidence / status) and
                # records what actually changed as durable learning
                # events. Compute-lane except when a relabel proposal is
                # waiting to be adjudicated. Non-fatal on failure.
                self._concept_drift_worker = None
                self._concept_relabel_rate_limiter = None
                if (
                    self._idle_scheduler is not None
                    and getattr(self, "_concept_store", None) is not None
                    and getattr(self, "_concept_event_store", None) is not None
                    and getattr(self, "_concept_learning_store", None)
                    is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                    and bool(
                        getattr(
                            self._memory_settings,
                            "concept_drift_enabled",
                            True,
                        )
                    )
                ):
                    try:
                        from app.core.concepts.concept_drift_worker import (
                            ConceptDriftWorker,
                        )
                        from app.core.concepts.concept_snapshot import (
                            resolve_evidence_labels,
                        )
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )

                        self._concept_relabel_rate_limiter = (
                            FactCheckRateLimiter(
                                self._chat_db,
                                per_hour_cap=int(
                                    getattr(
                                        settings.agent,
                                        "concept_relabel_per_hour_cap",
                                        3,
                                    )
                                ),
                                per_day_cap=int(
                                    getattr(
                                        settings.agent,
                                        "concept_relabel_per_day_cap",
                                        12,
                                    )
                                ),
                                state_key="concept_relabel.rate_state",
                            )
                        )

                        def _drift_evidence_labels(cid: int) -> list[str]:
                            return resolve_evidence_labels(
                                self._concept_store,
                                self._memory_store,
                                getattr(self, "_topic_graph", None),
                                int(cid),
                                limit=6,
                            )

                        self._concept_drift_worker = ConceptDriftWorker(
                            concept_store=self._concept_store,
                            concept_event_store=self._concept_event_store,
                            learning_store=self._concept_learning_store,
                            memory_settings=self._memory_settings,
                            agent_settings=settings.agent,
                            embedder=getattr(self, "_embedder", None),
                            # Idle-scheduler worker -> maintenance tier.
                            ollama=getattr(self, "_maintenance_client", None),
                            chat_model=self._effective_worker_model,
                            rate_limiter=(
                                self._concept_relabel_rate_limiter
                            ),
                            cancel_event=self._fact_check_cancel,
                            kv_get=self._chat_db.kv_get,
                            kv_set=self._chat_db.kv_set,
                            evidence_labels_provider=_drift_evidence_labels,
                        )
                        self._idle_scheduler.register(
                            self._concept_drift_worker
                        )
                    except Exception:
                        log.warning(
                            "ConceptDriftWorker boot failed", exc_info=True
                        )
                        self._concept_drift_worker = None
                        self._concept_relabel_rate_limiter = None

                # L17f: EvolutionDiaryWorker. Composes one grounded
                # first-person paragraph per period from the learning
                # events the drift worker recorded, or none at all. Always
                # LLM-lane, but at a daily heartbeat behind a weekly
                # cooldown, so it is one of the cheapest workers here.
                self._evolution_diary_worker = None
                if (
                    self._idle_scheduler is not None
                    and getattr(self, "_concept_learning_store", None)
                    is not None
                    and getattr(self, "_evolution_diary_store", None)
                    is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                    and bool(
                        getattr(
                            settings.agent, "evolution_diary_enabled", True
                        )
                    )
                ):
                    try:
                        from app.core.concepts.evolution_diary_worker import (
                            EvolutionDiaryWorker,
                        )

                        self._evolution_diary_worker = EvolutionDiaryWorker(
                            learning_store=self._concept_learning_store,
                            diary_store=self._evolution_diary_store,
                            memory_settings=self._memory_settings,
                            agent_settings=settings.agent,
                            # Idle-scheduler worker -> maintenance tier.
                            ollama=getattr(self, "_maintenance_client", None),
                            chat_model=self._effective_worker_model,
                            cancel_event=self._fact_check_cancel,
                            kv_get=self._chat_db.kv_get,
                            kv_set=self._chat_db.kv_set,
                            user_name_provider=lambda: str(
                                getattr(self, "user_display_name", "") or ""
                            ),
                        )
                        self._idle_scheduler.register(
                            self._evolution_diary_worker
                        )
                    except Exception:
                        log.warning(
                            "EvolutionDiaryWorker boot failed", exc_info=True
                        )
                        self._evolution_diary_worker = None

                # L25: concept<->memory edge referential integrity. Wire the
                # reconciler as a MemoryStore delete listener (drop a deleted
                # memory's edges + recompute the affected concepts' evidence
                # counts) and register the idle integrity sweep that GCs
                # orphans left by ``prune()`` (which bypasses delete
                # listeners). Independent of the lifecycle worker so edges
                # stay honest even if L3 is disabled. Non-fatal on failure.
                self._concept_edge_reconciler = None
                self._concept_edge_integrity_worker = None
                if (
                    getattr(self, "_concept_store", None) is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                ):
                    try:
                        from app.core.concepts.concept_edge_reconciler import (
                            ConceptEdgeReconciler,
                        )

                        reconciler = ConceptEdgeReconciler(self._concept_store)
                        self._concept_edge_reconciler = reconciler
                        if self._memory_store is not None:
                            self._memory_store.add_delete_listener(
                                reconciler.on_memory_deleted
                            )
                        if self._idle_scheduler is not None:
                            from app.core.concepts.concept_edge_integrity_worker import (  # noqa: E501
                                ConceptEdgeIntegrityWorker,
                            )

                            self._concept_edge_integrity_worker = (
                                ConceptEdgeIntegrityWorker(
                                    reconciler=reconciler,
                                    memory_settings=self._memory_settings,
                                    agent_settings=settings.agent,
                                )
                            )
                            self._idle_scheduler.register(
                                self._concept_edge_integrity_worker
                            )
                    except Exception:
                        log.warning(
                            "ConceptEdgeReconciler boot failed", exc_info=True
                        )
                        self._concept_edge_reconciler = None
                        self._concept_edge_integrity_worker = None

                # L14: AspirationMomentumWorker. Occasional staleness-driven
                # producer that drafts a private "check in on where they're
                # heading" cue over an active ``aspiration`` concept (read via a
                # ConceptView, per L24). Consumer side is
                # ``_render_aspiration_momentum_block``. Needs the concept store;
                # opt-in via ``concepts_enabled`` AND
                # ``aspiration_momentum_enabled``. Non-fatal on failure.
                self._aspiration_momentum_worker = None
                if (
                    self._idle_scheduler is not None
                    and getattr(self, "_concept_store", None) is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                    and bool(
                        getattr(
                            settings.agent, "aspiration_momentum_enabled", True
                        )
                    )
                ):
                    try:
                        from app.core.concepts.concept_view import (
                            concept_view_from,
                        )
                        from app.core.proactive.aspiration_momentum_worker import (  # noqa: E501
                            AspirationMomentumWorker,
                        )

                        mem = self._memory_settings
                        self._aspiration_momentum_worker = (
                            AspirationMomentumWorker(
                                kv_get=self._chat_db.kv_get,
                                kv_set=self._chat_db.kv_set,
                                view_provider=lambda: concept_view_from(self),
                                user_display_name_provider=(
                                    lambda: self.user_display_name
                                ),
                                enabled_provider=lambda: bool(
                                    getattr(
                                        self._settings.agent,
                                        "aspiration_momentum_enabled",
                                        True,
                                    )
                                ),
                                interval_seconds=float(
                                    getattr(
                                        mem,
                                        "aspiration_momentum_interval_seconds",
                                        21600.0,
                                    )
                                ),
                                cooldown_days=float(
                                    getattr(
                                        mem,
                                        "aspiration_momentum_cooldown_days",
                                        10.0,
                                    )
                                ),
                                min_confidence=float(
                                    getattr(
                                        mem,
                                        "aspiration_momentum_min_confidence",
                                        0.6,
                                    )
                                ),
                                staleness_min_days=float(
                                    getattr(
                                        mem,
                                        "aspiration_momentum_staleness_min_days",
                                        7.0,
                                    )
                                ),
                                journal_max=int(
                                    getattr(
                                        mem,
                                        "aspiration_momentum_journal_max",
                                        4,
                                    )
                                ),
                            )
                        )
                        self._idle_scheduler.register(
                            self._aspiration_momentum_worker,
                        )
                    except Exception:
                        log.warning(
                            "AspirationMomentumWorker boot failed",
                            exc_info=True,
                        )
                        self._aspiration_momentum_worker = None

                # L12: TensionCueWorker. Rare, gentle producer that drafts a
                # private "a friction worth sitting with" cue over an active
                # ``tension`` (meta) concept (read via a ConceptView, per L24).
                # Consumer side is ``_render_tension_block``; tension concepts
                # are kept out of the relevant-context block, so this
                # strictly-cooldowned cue is their only surface. Opt-in via
                # ``concepts_enabled`` AND ``tension_cue_enabled``. Non-fatal.
                self._tension_cue_worker = None
                if (
                    self._idle_scheduler is not None
                    and getattr(self, "_concept_store", None) is not None
                    and bool(getattr(settings.agent, "concepts_enabled", False))
                    and bool(
                        getattr(settings.agent, "tension_cue_enabled", True)
                    )
                ):
                    try:
                        from app.core.concepts.concept_view import (
                            concept_view_from,
                        )
                        from app.core.proactive.tension_cue_worker import (
                            TensionCueWorker,
                        )

                        mem = self._memory_settings
                        self._tension_cue_worker = TensionCueWorker(
                            kv_get=self._chat_db.kv_get,
                            kv_set=self._chat_db.kv_set,
                            view_provider=lambda: concept_view_from(self),
                            user_display_name_provider=(
                                lambda: self.user_display_name
                            ),
                            enabled_provider=lambda: bool(
                                getattr(
                                    self._settings.agent,
                                    "tension_cue_enabled",
                                    True,
                                )
                            ),
                            interval_seconds=float(
                                getattr(
                                    mem,
                                    "tension_cue_interval_seconds",
                                    28800.0,
                                )
                            ),
                            cooldown_days=float(
                                getattr(
                                    self._settings.agent,
                                    "tension_cue_cooldown_days",
                                    6.0,
                                )
                            ),
                            min_confidence=float(
                                getattr(
                                    mem,
                                    "tension_cue_min_confidence",
                                    0.6,
                                )
                            ),
                            journal_max=int(
                                getattr(mem, "tension_cue_journal_max", 4)
                            ),
                        )
                        self._idle_scheduler.register(
                            self._tension_cue_worker,
                        )
                    except Exception:
                        log.warning(
                            "TensionCueWorker boot failed", exc_info=True
                        )
                        self._tension_cue_worker = None

                # K11: PreThoughtWorker. Drafts + caches Aiko's reply to
                # likely upcoming questions during idle windows so the
                # first real response lands smoother. Independent of the
                # topic graph (it grounds on the rolling summary +
                # persona, not clusters). LLM spend is bounded by its
                # own FactCheckRateLimiter; failures here only drop the
                # speculative cache — the live turn path is unaffected.
                self._pre_thought_worker = None
                self._pre_thought_rate_limiter = None
                if (
                    self._memory_store is not None
                    and self._embedder is not None
                    and self._fact_check_cancel is not None
                    and getattr(self, "_prompt_assembler", None) is not None
                    and bool(getattr(settings.agent, "pre_thought_enabled", True))
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.proactive.pre_thought_worker import (
                            PreThoughtWorker,
                        )

                        persona_path_pt = (
                            Path(__file__).resolve().parents[3]
                            / "data" / "persona" / "aiko_companion.txt"
                        )

                        def _pt_persona_provider() -> str:
                            try:
                                return persona_path_pt.read_text(encoding="utf-8")
                            except OSError:
                                return ""

                        def _pt_summary_provider() -> str:
                            try:
                                row = self._chat_db.get_latest_summary(
                                    self.session_key,
                                )
                                return (row.summary if row is not None else "") or ""
                            except Exception:
                                return ""

                        def _pt_assistant_name_provider() -> str:
                            return self._fact_check_assistant_name() or "Aiko"

                        def _pt_messages_builder(
                            question: str,
                        ) -> list[dict[str, Any]]:
                            return self._prompt_assembler.build_eval_messages(
                                question, full_context=False,
                            )

                        self._pre_thought_rate_limiter = FactCheckRateLimiter(
                            self._chat_db,
                            per_hour_cap=int(getattr(
                                settings.agent, "pre_thought_per_hour_cap", 6,
                            )),
                            per_day_cap=int(getattr(
                                settings.agent, "pre_thought_per_day_cap", 40,
                            )),
                            state_key="pre_thought.rate_state",
                        )
                        self._pre_thought_worker = PreThoughtWorker(
                            memory_store=self._memory_store,
                            embedder=self._embedder,
                            # Idle-scheduler worker → maintenance tier.
                            ollama=self._maintenance_client,
                            chat_model=self._effective_worker_model,
                            cancel_event=self._fact_check_cancel,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            rate_limiter=self._pre_thought_rate_limiter,
                            persona_messages_builder=_pt_messages_builder,
                            persona_provider=_pt_persona_provider,
                            rolling_summary_provider=_pt_summary_provider,
                            user_display_name_provider=(
                                lambda: self.user_display_name
                            ),
                            assistant_display_name_provider=(
                                _pt_assistant_name_provider
                            ),
                            notify_memory_added=self._notify_memory_added,
                        )
                        self._idle_scheduler.register(
                            self._pre_thought_worker,
                        )
                    except Exception:
                        log.warning(
                            "PreThoughtWorker boot failed", exc_info=True,
                        )
                        self._pre_thought_worker = None

                # K21: ThreadResummaryWorker. Periodically re-synthesises
                # a short "where this thread stands now" note (+ a short
                # title for the sidebar) for the active session. One LLM
                # call per due tick on the maintenance client.
                self._thread_resummary_worker = None
                if (
                    self._fact_check_cancel is not None
                    and bool(getattr(settings.agent, "thread_resummary_enabled", True))
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.proactive.thread_resummary_worker import (
                            ThreadResummaryWorker,
                        )

                        def _tr_assistant_name_provider() -> str:
                            return self._fact_check_assistant_name() or "Aiko"

                        self._thread_resummary_rate_limiter = FactCheckRateLimiter(
                            self._chat_db,
                            per_hour_cap=int(getattr(
                                settings.agent, "thread_resummary_per_hour_cap", 6,
                            )),
                            per_day_cap=int(getattr(
                                settings.agent, "thread_resummary_per_day_cap", 24,
                            )),
                            state_key="thread_resummary.rate_state",
                        )
                        self._thread_resummary_worker = ThreadResummaryWorker(
                            chat_db=self._chat_db,
                            ollama=self._maintenance_client,
                            chat_model=self._effective_worker_model,
                            cancel_event=self._fact_check_cancel,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            rate_limiter=self._thread_resummary_rate_limiter,
                            session_key_provider=lambda: self.session_key,
                            user_display_name_provider=(
                                lambda: self.user_display_name
                            ),
                            assistant_display_name_provider=(
                                _tr_assistant_name_provider
                            ),
                            notify_thread_note=self._notify_thread_note,
                        )
                        self._idle_scheduler.register(
                            self._thread_resummary_worker,
                        )
                    except Exception:
                        log.warning(
                            "ThreadResummaryWorker boot failed", exc_info=True,
                        )
                        self._thread_resummary_worker = None

                # K1: GoalWorker. Cold-start bootstrap when the ring
                # is empty, reflection ticks otherwise. Each LLM call
                # passes through a dedicated FactCheckRateLimiter so
                # the worker's daily budget stays independent of F1's.
                # Failures here only drop autonomous reflection; the
                # self-tag write path and agent tools still work
                # against ``self._goal_store``.
                self._goal_worker = None
                self._goal_worker_rate_limiter = None
                if (
                    self._goal_store is not None
                    and self._fact_check_cancel is not None
                    and bool(getattr(settings.agent, "goals_enabled", True))
                ):
                    try:
                        from app.core.memory.fact_check_rate_limiter import (
                            FactCheckRateLimiter,
                        )
                        from app.core.goals.goal_worker import GoalWorker

                        self._goal_worker_rate_limiter = FactCheckRateLimiter(
                            self._chat_db,
                            per_hour_cap=int(getattr(
                                settings.agent,
                                "goal_worker_per_hour_cap",
                                3,
                            )),
                            per_day_cap=int(getattr(
                                settings.agent,
                                "goal_worker_per_day_cap",
                                12,
                            )),
                            state_key="goal_worker.rate_state",
                        )

                        persona_path_goal = (
                            Path(__file__).resolve().parents[3]
                            / "data" / "persona" / "aiko_companion.txt"
                        )

                        def _persona_provider_goal() -> str:
                            try:
                                return persona_path_goal.read_text(
                                    encoding="utf-8",
                                )
                            except OSError:
                                return ""

                        def _summary_provider_goal() -> str:
                            try:
                                row = self._chat_db.get_latest_summary(
                                    self.session_key,
                                )
                                return (row.summary if row is not None else "") or ""
                            except Exception:
                                return ""

                        def _assistant_name_provider_goal() -> str:
                            return (
                                self._fact_check_assistant_name() or "Aiko"
                            )

                        self._goal_worker = GoalWorker(
                            goal_store=self._goal_store,
                            # Idle-scheduler worker → maintenance tier.
                            ollama=self._maintenance_client,
                            chat_model=self._effective_worker_model,
                            cancel_event=self._fact_check_cancel,
                            agent_settings=settings.agent,
                            memory_settings=self._memory_settings,
                            rate_limiter=self._goal_worker_rate_limiter,
                            persona_provider=_persona_provider_goal,
                            rolling_summary_provider=_summary_provider_goal,
                            user_display_name_provider=(
                                lambda: self.user_display_name
                            ),
                            assistant_display_name_provider=(
                                _assistant_name_provider_goal
                            ),
                            notify_memory_added=self._notify_memory_added,
                            notify_memory_updated=self._notify_memory_updated,
                        )
                        self._idle_scheduler.register(self._goal_worker)
                    except Exception:
                        log.warning(
                            "GoalWorker boot failed", exc_info=True,
                        )
                        self._goal_worker = None
                        self._goal_worker_rate_limiter = None

                # Aiko's living garden — plant stage promotion + visiting
                # the garden during idle daylight windows. Both workers
                # piggyback on the shared scheduler so they share the
                # quiet-window gate; they're a no-op when the WorldStore
                # never loaded. Failures here only drop garden cycling;
                # the manual tools still work.
                if getattr(self, "_world_store", None) is not None:
                    try:
                        from app.core.world.garden_visit_worker import (
                            GardenVisitWorker,
                        )
                        from app.core.world.plant_growth_worker import (
                            PlantGrowthWorker,
                        )
                        from app.core.world.circadian_settle_worker import (
                            CircadianSettleWorker,
                        )

                        self._idle_scheduler.register(
                            PlantGrowthWorker(
                                self._world_store,
                                notify=self._notify_world,
                            )
                        )
                        _gmem = self._memory_settings
                        self._garden_visit_worker = GardenVisitWorker(
                            self._world_store,
                            notify=self._notify_world,
                            kv_get=self._chat_db.kv_get,
                            kv_set=self._chat_db.kv_set,
                            intentional_hold_seconds=getattr(
                                self._settings.agent,
                                "world_intentional_hold_seconds",
                                7200.0,
                            ),
                            circadian_period_provider=(
                                lambda: self.current_circadian_period()
                            ),
                            enabled_provider=lambda: bool(
                                getattr(
                                    self._settings.agent,
                                    "garden_visits_enabled",
                                    True,
                                )
                            ),
                            # H15 — needs-driven + varied + journalled.
                            need_dry_days=getattr(
                                _gmem, "garden_need_dry_days", 2.0,
                            ),
                            need_visit_floor_seconds=(
                                getattr(
                                    _gmem, "garden_need_visit_floor_hours", 0.75,
                                )
                                * 3600.0
                            ),
                            relax_ratio=getattr(
                                _gmem, "garden_relax_ratio", 0.3,
                            ),
                            visit_min_minutes=getattr(
                                _gmem, "garden_visit_min_minutes", 4.0,
                            ),
                            visit_max_minutes=getattr(
                                _gmem, "garden_visit_max_minutes", 10.0,
                            ),
                            journal_max=getattr(
                                _gmem, "garden_journal_max", 8,
                            ),
                            # K85b — a tended garden is the clearest
                            # away beat she has; keep it past the ring.
                            pursuit_notes=self._pursuit_note_writer(),
                        )
                        self._idle_scheduler.register(
                            self._garden_visit_worker
                        )
                        # H16 — circadian "where you find her" default.
                        self._idle_scheduler.register(
                            CircadianSettleWorker(
                                self._world_store,
                                notify=self._notify_world,
                                kv_get=self._chat_db.kv_get,
                                enabled_provider=lambda: bool(
                                    getattr(
                                        self._settings.agent,
                                        "circadian_settle_enabled",
                                        True,
                                    )
                                ),
                                circadian_period_provider=(
                                    lambda: self.current_circadian_period()
                                ),
                                interval_seconds=(
                                    self._memory_settings
                                    .circadian_settle_interval_seconds
                                ),
                                settle_after_seconds=(
                                    self._memory_settings
                                    .circadian_settle_after_seconds
                                ),
                                intentional_hold_seconds=getattr(
                                    self._settings.agent,
                                    "world_intentional_hold_seconds",
                                    7200.0,
                                ),
                            )
                        )
                    except Exception:
                        log.warning(
                            "garden idle workers failed to register",
                            exc_info=True,
                        )
                self._idle_scheduler.start()
            except Exception:
                log.warning("idle worker scheduler boot failed", exc_info=True)
                self._idle_scheduler = None
        self._turn_runner = TurnRunner(
            self._chat_client,
            self._chat_db,
            self._prompt_assembler,
            model=self._effective_chat_model,
            context_window=self._context_window,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            summary_worker=self._summary_worker,
            memory_store=self._memory_store,
            embedder=self._embedder,
            self_tagged_salience=self._memory_settings.self_tagged_salience,
            max_prompt_tokens_pct=settings.agent.max_prompt_tokens_pct,
            on_memory_added=self._notify_memory_added,
            on_tool_call=lambda name, args: self._notify_tool_event(
                "call", {"name": name, "arguments": args},
            ),
            on_tool_result=lambda name, content, ok: self._notify_tool_event(
                "result", {"name": name, "ok": bool(ok), "preview": (content or "")[:200]},
            ),
            filler_threshold_ms=settings.agent.filler_first_token_ms,
            filler_enabled=settings.agent.filler_enabled,
            speech_texture_spoken=settings.agent.speech_texture_spoken,
            listen_extensions_provider=lambda: int(
                getattr(self, "_last_listen_extensions", 0) or 0
            ),
            tool_pass_gate_enabled=settings.agent.tool_pass_gate_enabled,
            # Brain-lane skill router: progressive tool disclosure with an
            # always-on core (time/recall/world) so spontaneous room
            # actions survive. Off by default = full toolset every turn.
            skill_router_enabled=settings.agent.skill_router_enabled,
            brain_core_families=settings.agent.brain_core_skills,
            # P14 continuity hook: the gate always runs the tool pass
            # while any task is running / awaiting_input / paused (the
            # user's message may be the answer a pending task needs).
            tasks_active_provider=self._any_tasks_active,
        )
        self._tool_event_listeners: list[Callable[[str, dict[str, Any]], None]] = []
        self._tool_registry = None
        try:
            self.rebuild_tool_registry()
        except Exception:
            log.warning("initial tool registry build failed", exc_info=True)
        # Phase 4c: conversation arc tracker (regex hot-path + LLM smoother).
        self._arc_store = None
        self._arc_estimator = None
        self._arc_smoother = None
        try:
            from app.core.conversation.conversation_arc import (
                ArcEstimator,
                ArcSmootherWorker,
                ArcStore,
            )

            self._arc_store = ArcStore(self._chat_db)
            self._arc_estimator = ArcEstimator(self._arc_store)
            self._arc_smoother = ArcSmootherWorker(
                ollama=self._ollama,
                store=self._arc_store,
                model=self._effective_worker_model,
                every_n_turns=max(
                    1, int(settings.agent.arc_update_every_n_turns) * 6
                ),
                user_display_name_provider=lambda: self.user_display_name,
            )
        except Exception:
            log.warning("ArcStore/ArcEstimator init failed", exc_info=True)
            self._arc_store = None
            self._arc_estimator = None
            self._arc_smoother = None

        # Phase 4c: prepared nudge store + narrative weaver.
        self._prepared_nudge_store = None
        self._narrative_weaver = None
        try:
            from app.core.proactive.prepared_nudge import (
                NarrativeWeaver,
                PreparedNudgeStore,
            )

            self._prepared_nudge_store = PreparedNudgeStore(self._chat_db)
            self._narrative_weaver = NarrativeWeaver(
                ollama=self._ollama,
                store=self._prepared_nudge_store,
                memory_store=self._memory_store,
                agenda_store=getattr(self, "_agenda_store", None),
                model=self._effective_worker_model,
                every_n_turns=4,
                ttl_seconds=settings.agent.prepared_nudge_ttl_seconds,
                user_display_name_provider=lambda: self.user_display_name,
                cue_store_provider=lambda: getattr(self, "_cue_store", None),
            )
        except Exception:
            log.warning("PreparedNudgeStore/NarrativeWeaver init failed", exc_info=True)
            self._prepared_nudge_store = None
            self._narrative_weaver = None
