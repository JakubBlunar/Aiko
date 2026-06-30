"""Idle/background worker bootstrap mixin.

Extracted from ``SessionController.__init__``. Holds the construction +
registration of the idle-window background workers (forward curiosity,
away-activity, wants ledger, promise follow-through, schedule learner,
conflict detector, consolidation, opinion rate limiter, belief store,
...). Runs in the same order it used to inline; state ownership is
unchanged.

NB: tests that patched ``app.core.session.session_controller.<symbol>``
for a symbol used here must patch
``app.core.session.idle_workers_init_mixin.<symbol>`` instead."""
from __future__ import annotations

import logging
from typing import Any


log = logging.getLogger("app.session")


class IdleWorkersInitMixin:
    """__init__ bootstrap: idle/background workers + their stores."""

    def _init_idle_workers(self, settings: AppSettings) -> None:
        if (
            self._idle_scheduler is not None
            and self._memory_store is not None
        ):
            try:
                from app.core.proactive.follow_up_worker import FollowUpWorker

                mem = self._memory_settings
                self._follow_up_worker = FollowUpWorker(
                    memory_store=self._memory_store,
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    user_id_provider=lambda: self._user_id,
                    user_display_name_provider=(
                        lambda: self.user_display_name
                    ),
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "follow_up_enabled",
                            True,
                        )
                    ),
                    ollama=self._maintenance_client,
                    model=self._effective_worker_model,
                    journal_max=getattr(mem, "follow_up_journal_max", 8),
                )
                self._idle_scheduler.register(self._follow_up_worker)
            except Exception:
                log.warning("FollowUpWorker init failed", exc_info=True)

        # WorldNoticeWorker — proactive "I noticed my room / the thing you
        # left me" nudges. Rides the same idle scheduler + prepared-nudge
        # store as the FollowUpWorker, and composes its line on the local
        # worker LLM (``_maintenance_client``) so it's free and non-blocking.
        # A no-op when the WorldStore never loaded; failures only drop the
        # proactive room path.
        if (
            self._idle_scheduler is not None
            and getattr(self, "_world_store", None) is not None
            and self._prepared_nudge_store is not None
        ):
            try:
                from app.core.world.world_notice_worker import WorldNoticeWorker

                mem = self._memory_settings
                self._idle_scheduler.register(
                    WorldNoticeWorker(
                        world_store=self._world_store,
                        prepared_nudge_store=self._prepared_nudge_store,
                        kv_get=self._chat_db.kv_get,
                        kv_set=self._chat_db.kv_set,
                        user_id_provider=lambda: self._user_id,
                        user_display_name_provider=(
                            lambda: self.user_display_name
                        ),
                        enabled_provider=lambda: bool(
                            getattr(
                                self._settings.agent,
                                "world_notice_enabled",
                                True,
                            )
                        ),
                        ollama=self._maintenance_client,
                        model=self._effective_worker_model,
                        interval_seconds=mem.world_notice_interval_seconds,
                        cooldown_seconds=mem.world_notice_cooldown_seconds,
                        daily_cap=mem.world_notice_daily_cap,
                        ttl_seconds=mem.world_notice_ttl_seconds,
                    )
                )
            except Exception:
                log.warning("WorldNoticeWorker init failed", exc_info=True)

        # H11 WeatherWorker — passive ambient weather feed. Fetches the
        # configured home-location conditions on a low cadence during idle
        # windows, stashes a snapshot in kv_meta for the ambient prompt cue
        # + persona overlay, and fans a ``weather_updated`` WS frame out via
        # ``_notify_weather``. Gated on ``agent.weather_sync_enabled`` AND a
        # resolved home location (``is_ready`` returns False otherwise).
        if self._idle_scheduler is not None:
            try:
                from app.core.world.weather_worker import WeatherWorker

                self._weather_worker = WeatherWorker(
                    chat_db=self._chat_db,
                    provider_getter=self._get_weather_provider,
                    home_provider=self._weather_home,
                    units_provider=self._weather_units,
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "weather_sync_enabled",
                            False,
                        )
                    ),
                    interval_provider=lambda: float(
                        getattr(
                            self._settings.weather,
                            "refresh_interval_minutes",
                            30,
                        )
                    ) * 60.0,
                    notify=self._notify_weather,
                    seasonal_hook=getattr(
                        self, "_apply_weather_seasonal_decor", None,
                    ),
                )
                self._idle_scheduler.register(self._weather_worker)
            except Exception:
                log.warning("WeatherWorker init failed", exc_info=True)

        # K36 IdleAwayActivityWorker — Aiko's quiet room life. Mutates the
        # world during idle windows + journals it; the away-activities
        # provider surfaces one line on the first turn back. Shares the
        # idle scheduler + world-store gate; no prepared-nudge dependency
        # (it's a silent producer, not a proactive nudge). Failures only
        # drop the away-activities path.
        if (
            self._idle_scheduler is not None
            and getattr(self, "_world_store", None) is not None
        ):
            try:
                from app.core.world.idle_activity_worker import (
                    IdleAwayActivityWorker,
                )

                mem = self._memory_settings
                self._away_activity_worker = IdleAwayActivityWorker(
                    world_store=self._world_store,
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    user_display_name_provider=(
                        lambda: self.user_display_name
                    ),
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "away_activities_enabled",
                            True,
                        )
                    ),
                    notify=self._notify_world,
                    ollama=self._maintenance_client,
                    model=self._effective_worker_model,
                    interval_seconds=mem.away_activities_interval_seconds,
                    cooldown_seconds=mem.away_activities_cooldown_seconds,
                    daily_cap=mem.away_activities_daily_cap,
                    journal_max=mem.away_activities_journal_max,
                    intentional_hold_seconds=getattr(
                        self._settings.agent,
                        "world_intentional_hold_seconds",
                        7200.0,
                    ),
                    # H14 — fraction of beats the worker LLM composes fresh.
                    llm_activity_ratio=getattr(
                        mem, "away_activities_llm_ratio", 0.5,
                    ),
                    # H17 — fraction of beats that also spawn a conversational
                    # seed (LLM-composed), plus the daily/ring bounds.
                    idle_seed_ratio=(
                        getattr(mem, "idle_seed_ratio", 0.25)
                        if getattr(
                            self._settings.agent, "idle_seed_enabled", True,
                        )
                        else 0.0
                    ),
                    idle_seed_daily_cap=getattr(
                        mem, "idle_seed_daily_cap", 3,
                    ),
                    idle_seed_max_ring=getattr(
                        mem, "idle_seed_max_ring", 6,
                    ),
                    # H22 — rare daylight "I stepped out for a bit" beat,
                    # paced by its own cooldown + daily cap.
                    outings_enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent, "outings_enabled", True,
                        )
                    ),
                    outing_cooldown_seconds=(
                        getattr(mem, "outing_cooldown_hours", 6.0) * 3600.0
                    ),
                    outing_daily_cap=getattr(mem, "outing_daily_cap", 2),
                    # H18 — tilt the weighted activity draw by time of day,
                    # current mood, and the daily personality colour.
                    circadian_period_provider=(
                        lambda: self.current_circadian_period()
                    ),
                    valence_provider=self._away_activity_valence,
                    day_color_provider=lambda: self._chat_db.kv_get(
                        "aiko.day_color"
                    ),
                )
                self._idle_scheduler.register(self._away_activity_worker)
            except Exception:
                log.warning(
                    "IdleAwayActivityWorker init failed", exc_info=True
                )

        # H19 HobbyWorker — Aiko's ongoing personal project. Maintains a
        # single multi-day "current hobby" that advances during quiet
        # windows + occasionally yields a takeaway seed (via the shared H17
        # idle-seed cue). Needs only kv + settings; the worker LLM is
        # optional (seeds are skipped without it). Failures drop the hobby.
        if self._idle_scheduler is not None:
            try:
                from app.core.proactive.hobby_worker import HobbyWorker

                mem = self._memory_settings
                self._hobby_worker = HobbyWorker(
                    chat_db=self._chat_db,
                    agent_settings=self._settings.agent,
                    memory_settings=mem,
                    user_display_name_provider=(
                        lambda: self.user_display_name
                    ),
                    ollama=self._maintenance_client,
                    model=self._effective_worker_model,
                    idle_seed_max_ring=getattr(
                        mem, "idle_seed_max_ring", 6,
                    ),
                )
                self._idle_scheduler.register(self._hobby_worker)
            except Exception:
                log.warning("HobbyWorker init failed", exc_info=True)

        # H20 RoomEvolutionWorker — a room that accrues a history. Slowly
        # drifts the seeded items (tea pot, cookie jar, book) during quiet
        # windows + broadcasts the world patch. Needs the world store; the
        # worker LLM is optional (book-finish seed falls back to a
        # template). Failures only drop the room-evolution path.
        if (
            self._idle_scheduler is not None
            and getattr(self, "_world_store", None) is not None
        ):
            try:
                from app.core.world.room_evolution_worker import (
                    RoomEvolutionWorker,
                )

                mem = self._memory_settings
                self._room_evolution_worker = RoomEvolutionWorker(
                    world_store=self._world_store,
                    chat_db=self._chat_db,
                    agent_settings=self._settings.agent,
                    memory_settings=mem,
                    user_display_name_provider=(
                        lambda: self.user_display_name
                    ),
                    notify=self._notify_world,
                    ollama=self._maintenance_client,
                    model=self._effective_worker_model,
                    idle_seed_max_ring=getattr(
                        mem, "idle_seed_max_ring", 6,
                    ),
                )
                self._idle_scheduler.register(self._room_evolution_worker)
            except Exception:
                log.warning(
                    "RoomEvolutionWorker init failed", exc_info=True
                )

        # H9 DiaryWorker — Aiko's away journal. During quiet windows with
        # NO UI client connected, she reflects on the recent conversation
        # and writes one short ``diary`` memory. While a window is open
        # the live ``[[diary:...]]`` tag owns the channel (the worker's
        # ``is_away_provider`` gate defers), so the two never
        # double-write. Needs the memory store + embedder; the worker LLM
        # is optional but compose is skipped without it. Failures only
        # drop the away-diary path.
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
            and getattr(self, "_embedder", None) is not None
        ):
            try:
                from app.core.proactive.diary_worker import (
                    DiaryWorker,
                    build_recent_context,
                )

                mem = self._memory_settings
                self._diary_worker = DiaryWorker(
                    memory_store=self._memory_store,
                    embed=lambda text: self._embedder.embed(text),
                    recent_context_provider=lambda: build_recent_context(
                        self._chat_db.get_messages(self.session_key, limit=14),
                        self.user_display_name,
                    ),
                    is_away_provider=lambda: self.is_user_away(),
                    user_display_name_provider=lambda: self.user_display_name,
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "diary_worker_enabled",
                            True,
                        )
                    ),
                    ollama=self._maintenance_client,
                    model=self._effective_worker_model,
                    on_memory_added=self._notify_memory_added,
                    day_color_provider=lambda: self._chat_db.kv_get(
                        "aiko.day_color"
                    ),
                    source_session_provider=lambda: self.session_key,
                    interval_seconds=mem.diary_worker_interval_seconds,
                    cooldown_seconds=mem.diary_worker_cooldown_seconds,
                    daily_cap=mem.diary_worker_daily_cap,
                    min_context_chars=mem.diary_worker_min_context_chars,
                )
                self._idle_scheduler.register(self._diary_worker)
            except Exception:
                log.warning("DiaryWorker init failed", exc_info=True)

        # K34 ForwardCuriosityWorker — drafts "I've been wondering ..."
        # questions about the user's life during quiet windows. No world
        # dependency (reads memory + profile only); shares the idle
        # scheduler + the gap-return surfacing path. Failures only drop
        # the forward-curiosity path.
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
        ):
            try:
                from app.core.proactive.forward_curiosity_worker import (
                    ForwardCuriosityWorker,
                )

                mem = self._memory_settings
                self._forward_curiosity_worker = ForwardCuriosityWorker(
                    memory_store=self._memory_store,
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    user_id_provider=lambda: self._user_id,
                    user_display_name_provider=(
                        lambda: self.user_display_name
                    ),
                    user_profile_store=getattr(
                        self, "_user_profile_store", None
                    ),
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "forward_curiosity_enabled",
                            True,
                        )
                    ),
                    ollama=self._maintenance_client,
                    model=self._effective_worker_model,
                    interval_seconds=mem.forward_curiosity_interval_seconds,
                    cooldown_seconds=mem.forward_curiosity_cooldown_seconds,
                    daily_cap=mem.forward_curiosity_daily_cap,
                    journal_max=mem.forward_curiosity_journal_max,
                )
                self._idle_scheduler.register(self._forward_curiosity_worker)
            except Exception:
                log.warning(
                    "ForwardCuriosityWorker init failed", exc_info=True
                )

        # F10f KnowledgeGapNoticeWorker — drafts a self-aware "I keep
        # circling X but never dug in" cue from dense, low-knowledge topic
        # clusters during quiet windows. Cheap kv pass (no LLM); the
        # provider only surfaces it when the live turn is on that topic.
        # Sibling of F9 IdleKnowledgeWorker, which silently *researches*
        # the same clusters.
        self._knowledge_gap_notice_worker = None
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
        ):
            try:
                from app.core.proactive.knowledge_gap_notice_worker import (
                    KnowledgeGapNoticeWorker,
                )

                mem = self._memory_settings
                self._knowledge_gap_notice_worker = KnowledgeGapNoticeWorker(
                    topic_graph_provider=lambda: getattr(
                        self, "_topic_graph", None
                    ),
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "knowledge_gap_notice_enabled",
                            True,
                        )
                    ),
                    interval_seconds=mem.knowledge_gap_notice_interval_seconds,
                    min_size=mem.knowledge_gap_notice_min_size,
                    max_knowledge_fraction=(
                        mem.knowledge_gap_notice_max_knowledge_fraction
                    ),
                    topic_cooldown_hours=(
                        mem.knowledge_gap_notice_topic_cooldown_hours
                    ),
                    journal_max=mem.knowledge_gap_notice_journal_max,
                )
                self._idle_scheduler.register(
                    self._knowledge_gap_notice_worker
                )
            except Exception:
                log.warning(
                    "KnowledgeGapNoticeWorker init failed", exc_info=True
                )

        # K64a AssociativeWanderWorker — drifts across the topic graph during
        # quiet windows, picks two *distant* clusters, and asks the worker
        # LLM for a genuine connection ("both reward following a faint trail
        # patiently"). The provider only surfaces it when the live turn is
        # on one of the two topics. First member of the K64 freedom-of-
        # thought family; paced hard (long interval + small daily cap + long
        # per-pair cooldown) because rarity is the whole point.
        self._associative_wander_worker = None
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
        ):
            try:
                from app.core.proactive.associative_wander_worker import (
                    AssociativeWanderWorker,
                )

                mem = self._memory_settings
                self._associative_wander_worker = AssociativeWanderWorker(
                    topic_graph_provider=lambda: getattr(
                        self, "_topic_graph", None
                    ),
                    memory_store=self._memory_store,
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "associative_wander_enabled",
                            True,
                        )
                    ),
                    ollama=self._maintenance_client,
                    model=self._effective_worker_model,
                    interval_seconds=mem.associative_wander_interval_seconds,
                    cooldown_seconds=mem.associative_wander_cooldown_seconds,
                    daily_cap=mem.associative_wander_daily_cap,
                    journal_max=mem.associative_wander_journal_max,
                    min_size=mem.associative_wander_min_size,
                    max_pair_cosine=mem.associative_wander_max_pair_cosine,
                    pair_cooldown_hours=(
                        mem.associative_wander_pair_cooldown_hours
                    ),
                    member_samples=mem.associative_wander_member_samples,
                )
                self._idle_scheduler.register(
                    self._associative_wander_worker
                )
            except Exception:
                log.warning(
                    "AssociativeWanderWorker init failed", exc_info=True
                )

        # K64b InterestDriftWorker — tracks each topic cluster's mass over
        # time and notices Aiko's own budding / fading interests ("I've been
        # weirdly into X lately"). Cheap kv pass (no LLM); the provider only
        # surfaces it when the live turn is on that topic. Second member of
        # the K64 freedom-of-thought family; the slow under-current sibling
        # of K27 day-colour.
        self._interest_drift_worker = None
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
        ):
            try:
                from app.core.proactive.interest_drift_worker import (
                    InterestDriftWorker,
                )

                mem = self._memory_settings
                self._interest_drift_worker = InterestDriftWorker(
                    topic_graph_provider=lambda: getattr(
                        self, "_topic_graph", None
                    ),
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "interest_drift_enabled",
                            True,
                        )
                    ),
                    interval_seconds=mem.interest_drift_interval_seconds,
                    daily_cap=mem.interest_drift_daily_cap,
                    journal_max=mem.interest_drift_journal_max,
                    min_size=mem.interest_drift_min_size,
                    max_clusters=mem.interest_drift_max_clusters,
                    window_samples=mem.interest_drift_window_samples,
                    min_samples=mem.interest_drift_min_samples,
                    rise_ratio=mem.interest_drift_rise_ratio,
                    fade_max_growth_ratio=(
                        mem.interest_drift_fade_max_growth_ratio
                    ),
                    topic_cooldown_hours=(
                        mem.interest_drift_topic_cooldown_hours
                    ),
                )
                self._idle_scheduler.register(self._interest_drift_worker)
            except Exception:
                log.warning(
                    "InterestDriftWorker init failed", exc_info=True
                )

        # K67 DormantInterestWorker — the symmetric sibling of K64b: notices
        # a topic cluster that was once a genuine, high-mass user interest and
        # has since gone quiet for weeks, and drafts a rare "we haven't talked
        # about X in ages" re-opener. Cheap kv pass (no LLM); the provider only
        # surfaces it on a natural conversational lull.
        self._dormant_interest_worker = None
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
        ):
            try:
                from app.core.proactive.dormant_interest_worker import (
                    DormantInterestWorker,
                )

                mem = self._memory_settings
                self._dormant_interest_worker = DormantInterestWorker(
                    topic_graph_provider=lambda: getattr(
                        self, "_topic_graph", None
                    ),
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "dormant_interest_enabled",
                            True,
                        )
                    ),
                    interval_seconds=mem.dormant_interest_interval_seconds,
                    daily_cap=mem.dormant_interest_daily_cap,
                    journal_max=mem.dormant_interest_journal_max,
                    min_size=mem.dormant_interest_min_size,
                    max_clusters=mem.dormant_interest_max_clusters,
                    dormant_days=mem.dormant_interest_dormant_days,
                    topic_cooldown_hours=(
                        mem.dormant_interest_topic_cooldown_hours
                    ),
                )
                self._idle_scheduler.register(self._dormant_interest_worker)
            except Exception:
                log.warning(
                    "DormantInterestWorker init failed", exc_info=True
                )

        # K64c CuriosityGradientWorker — finds a thin topic cluster on the
        # rim of a dense one (the under-explored edge of familiar territory)
        # and drafts a genuinely-curious-question cue. Cheap geometry pass
        # (no LLM); the provider only surfaces it when the live turn is on
        # either topic. Third member of the K64 freedom-of-thought family.
        self._curiosity_gradient_worker = None
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
        ):
            try:
                from app.core.proactive.curiosity_gradient_worker import (
                    CuriosityGradientWorker,
                )

                mem = self._memory_settings
                self._curiosity_gradient_worker = CuriosityGradientWorker(
                    topic_graph_provider=lambda: getattr(
                        self, "_topic_graph", None
                    ),
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "curiosity_gradient_enabled",
                            True,
                        )
                    ),
                    interval_seconds=mem.curiosity_gradient_interval_seconds,
                    daily_cap=mem.curiosity_gradient_daily_cap,
                    journal_max=mem.curiosity_gradient_journal_max,
                    dense_min_size=mem.curiosity_gradient_dense_min_size,
                    thin_min_size=mem.curiosity_gradient_thin_min_size,
                    thin_max_size=mem.curiosity_gradient_thin_max_size,
                    adjacency_min_cosine=(
                        mem.curiosity_gradient_adjacency_min_cosine
                    ),
                    adjacency_max_cosine=(
                        mem.curiosity_gradient_adjacency_max_cosine
                    ),
                    edge_cooldown_hours=(
                        mem.curiosity_gradient_edge_cooldown_hours
                    ),
                )
                self._idle_scheduler.register(
                    self._curiosity_gradient_worker
                )
            except Exception:
                log.warning(
                    "CuriosityGradientWorker init failed", exc_info=True
                )

        # K64d KnowledgeMapReflectionWorker — the introspective capstone of
        # the K64 family. On a ~daily interval it reads the *shape* of the
        # topic graph (richest territories + under-explored ones), runs a
        # worker-LLM meta-thought, and writes ONE [mindmap] kind="reflection"
        # memory. No new provider: that memory flows through the existing RAG
        # / K28 turning-over surfacing like every other reflection. Needs the
        # embedder + a worker LLM; failures only drop a single reflection.
        self._knowledge_map_reflection_worker = None
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
            and self._embedder is not None
            and self._maintenance_client is not None
            and self._chat_db is not None
        ):
            try:
                from app.core.proactive.knowledge_map_reflection_worker import (
                    KnowledgeMapReflectionWorker,
                )

                mem = self._memory_settings
                self._knowledge_map_reflection_worker = (
                    KnowledgeMapReflectionWorker(
                        topic_graph_provider=lambda: getattr(
                            self, "_topic_graph", None
                        ),
                        memory_store=self._memory_store,
                        embedder=self._embedder,
                        kv_get=self._chat_db.kv_get,
                        kv_set=self._chat_db.kv_set,
                        ollama=self._maintenance_client,
                        model=self._effective_worker_model,
                        enabled_provider=lambda: bool(
                            getattr(
                                self._settings.agent,
                                "knowledge_map_reflection_enabled",
                                True,
                            )
                        ),
                        notify_memory_added=self._notify_memory_added,
                        interval_seconds=(
                            mem.knowledge_map_reflection_interval_seconds
                        ),
                        cooldown_hours=(
                            mem.knowledge_map_reflection_cooldown_hours
                        ),
                        min_clusters=mem.knowledge_map_reflection_min_clusters,
                        rich_top_n=mem.knowledge_map_reflection_rich_top_n,
                        gap_top_n=mem.knowledge_map_reflection_gap_top_n,
                        max_tokens=mem.knowledge_map_reflection_max_tokens,
                        salience=mem.knowledge_map_reflection_salience,
                    )
                )
                self._idle_scheduler.register(
                    self._knowledge_map_reflection_worker
                )
            except Exception:
                log.warning(
                    "KnowledgeMapReflectionWorker init failed", exc_info=True
                )

        # K52 WantsLedgerWorker — keeps the wants ledger stocked from
        # curiosity seeds / forward-curiosity questions / goals during
        # quiet windows. Pure ingestion, no LLM. Failures only drop
        # the feeder; manual MCP adds still work.
        self._wants_ledger_worker = None
        if self._idle_scheduler is not None:
            try:
                from app.core.conversation.wants_ledger_worker import (
                    WantsLedgerWorker,
                )

                agent = settings.agent
                self._wants_ledger_worker = WantsLedgerWorker(
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    user_display_name_provider=(
                        lambda: self.user_display_name
                    ),
                    memory_store=getattr(self, "_memory_store", None),
                    goal_store=getattr(self, "_goal_store", None),
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "wants_ledger_enabled",
                            True,
                        )
                    ),
                    interval_seconds=float(
                        getattr(agent, "wants_worker_interval_seconds", 3600.0)
                    ),
                    cap=int(getattr(agent, "wants_cap", 8)),
                    growth_per_day=float(
                        getattr(agent, "wants_growth_per_day", 0.25)
                    ),
                    max_age_days=float(
                        getattr(agent, "wants_max_age_days", 14.0)
                    ),
                    reentry_cooldown_days=float(
                        getattr(agent, "wants_reentry_cooldown_days", 5.0)
                    ),
                )
                self._idle_scheduler.register(self._wants_ledger_worker)
            except Exception:
                log.warning(
                    "WantsLedgerWorker init failed", exc_info=True
                )

        # K43 PromiseFollowthroughWorker — closes the loop on Aiko's own
        # "I'll look into that" commitments. Scans assistant-side promise
        # memories during quiet windows, arms a one-shot follow-through
        # cue, and ages out stale promises. Failures only drop the
        # follow-through path.
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
        ):
            try:
                from app.core.proactive.promise_followthrough_worker import (
                    PromiseFollowthroughWorker,
                )

                mem = self._memory_settings
                self._promise_followthrough_worker = PromiseFollowthroughWorker(
                    memory_store=self._memory_store,
                    kv_get=self._chat_db.kv_get,
                    kv_set=self._chat_db.kv_set,
                    enabled_provider=lambda: bool(
                        getattr(
                            self._settings.agent,
                            "promise_followthrough_enabled",
                            True,
                        )
                    ),
                    interval_seconds=(
                        mem.promise_followthrough_interval_seconds
                    ),
                    min_age_hours=mem.promise_followthrough_min_age_hours,
                    cooldown_hours=mem.promise_followthrough_cooldown_hours,
                    drop_after_days=(
                        mem.promise_followthrough_drop_after_days
                    ),
                )
                self._idle_scheduler.register(
                    self._promise_followthrough_worker
                )
            except Exception:
                log.warning(
                    "PromiseFollowthroughWorker init failed", exc_info=True
                )

        # G2 — schedule learner. Independent of the FollowUpWorker
        # gate above (no prepared-nudge dependency), so wired after
        # the same idle scheduler. Reads only ``messages.created_at``
        # — never message content — and writes a single
        # ``usual_hours`` profile field. Failures only drop the
        # schedule field; the rest of the scheduler stays.
        if (
            self._idle_scheduler is not None
            and self._user_profile_store is not None
            and bool(
                getattr(settings.agent, "schedule_learner_enabled", True)
            )
        ):
            try:
                from app.core.infra.schedule_learner import ScheduleLearner

                self._idle_scheduler.register(
                    ScheduleLearner(
                        chat_db=self._chat_db,
                        profile_store=self._user_profile_store,
                        user_id_provider=lambda: self._user_id,
                        agent_settings=settings.agent,
                        memory_settings=self._memory_settings,
                    )
                )
            except Exception:
                log.warning("ScheduleLearner init failed", exc_info=True)

        # F5 — conflicting-memory detector. Always builds the store
        # (REST endpoints and the ``[[conflict:reason]]`` tag dispatch
        # need it even when the worker is disabled), then conditionally
        # builds + registers the worker. The cascade-cleanup hook on
        # ``MemoryStore.delete`` keeps ``memory_conflicts`` rows from
        # dangling when a user deletes a memory through the Memory
        # drawer.
        self._memory_conflict_store = None
        self._memory_conflict_worker = None
        self._memory_conflict_rate_limiter = None
        if self._memory_store is not None and self._chat_db is not None:
            try:
                from app.core.memory.memory_conflict_store import (
                    MemoryConflictStore,
                )

                self._memory_conflict_store = MemoryConflictStore(
                    self._chat_db,
                )
                self._memory_store.add_delete_listener(
                    self._memory_conflict_store.delete_for_memory,
                )
            except Exception:
                log.warning(
                    "MemoryConflictStore init failed", exc_info=True,
                )
                self._memory_conflict_store = None
        if (
            self._idle_scheduler is not None
            and self._memory_conflict_store is not None
            and self._fact_check_cancel is not None
            and bool(
                getattr(settings.agent, "conflict_detector_enabled", True)
            )
        ):
            try:
                from app.core.memory.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )
                from app.core.memory.memory_conflict_worker import (
                    MemoryConflictWorker,
                )

                self._memory_conflict_rate_limiter = FactCheckRateLimiter(
                    self._chat_db,
                    per_hour_cap=int(
                        getattr(
                            settings.agent,
                            "conflict_detector_per_hour_cap",
                            6,
                        )
                    ),
                    per_day_cap=int(
                        getattr(
                            settings.agent,
                            "conflict_detector_per_day_cap",
                            30,
                        )
                    ),
                    state_key="conflict_detector.rate_state",
                )
                self._memory_conflict_worker = MemoryConflictWorker(
                    memory_store=self._memory_store,
                    conflict_store=self._memory_conflict_store,
                    # Idle-scheduler worker → maintenance tier.
                    ollama=self._maintenance_client,
                    chat_model=self._effective_worker_model,
                    rate_limiter=self._memory_conflict_rate_limiter,
                    cancel_event=self._fact_check_cancel,
                    agent_settings=settings.agent,
                    memory_settings=self._memory_settings,
                    notify_memory_updated=self._notify_memory_updated,
                    topic_graph_provider=lambda: getattr(
                        self, "_topic_graph", None
                    ),
                )
                self._idle_scheduler.register(self._memory_conflict_worker)
            except Exception:
                log.warning(
                    "MemoryConflictWorker init failed", exc_info=True,
                )
                self._memory_conflict_worker = None
                self._memory_conflict_rate_limiter = None

        # K35 — memory consolidation worker. Fuses near-duplicate
        # scratchpad rows into one long_term memory during quiet
        # windows. Needs the embedder (re-embeds merged text) + a worker
        # LLM (rate-limited merge with deterministic fallback). Its own
        # FactCheckRateLimiter state_key keeps the merge budget
        # independent of F1 / F5 / G3. Failures only drop consolidation.
        self._memory_consolidation_worker = None
        self._memory_consolidation_rate_limiter = None
        if (
            self._idle_scheduler is not None
            and getattr(self, "_memory_store", None) is not None
            and self._embedder is not None
            and self._fact_check_cancel is not None
            and bool(
                getattr(settings.agent, "memory_consolidation_enabled", True)
            )
        ):
            try:
                from app.core.memory.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )
                from app.core.memory.memory_consolidation_worker import (
                    MemoryConsolidationWorker,
                )

                self._memory_consolidation_rate_limiter = FactCheckRateLimiter(
                    self._chat_db,
                    per_hour_cap=int(
                        getattr(
                            settings.agent,
                            "memory_consolidation_per_hour_cap",
                            6,
                        )
                    ),
                    per_day_cap=int(
                        getattr(
                            settings.agent,
                            "memory_consolidation_per_day_cap",
                            30,
                        )
                    ),
                    state_key="memory_consolidation.rate_state",
                )
                self._memory_consolidation_worker = MemoryConsolidationWorker(
                    memory_store=self._memory_store,
                    embedder=self._embedder,
                    # Idle-scheduler worker → maintenance tier.
                    ollama=self._maintenance_client,
                    chat_model=self._effective_worker_model,
                    rate_limiter=self._memory_consolidation_rate_limiter,
                    cancel_event=self._fact_check_cancel,
                    agent_settings=settings.agent,
                    memory_settings=self._memory_settings,
                    notify_memory_updated=self._notify_memory_updated,
                    topic_graph_provider=lambda: getattr(
                        self, "_topic_graph", None
                    ),
                )
                self._idle_scheduler.register(
                    self._memory_consolidation_worker
                )
            except Exception:
                log.warning(
                    "MemoryConsolidationWorker init failed", exc_info=True,
                )
                self._memory_consolidation_worker = None
                self._memory_consolidation_rate_limiter = None

        # K29 — opinion-injection rate limiter (LLM YES/NO gate on
        # borderline-heuristic stance contradictions). Independent
        # ``state_key`` so the budget can't be exhausted by the F5
        # conflict detector or the K2 belief worker. Lives off the
        # same ``FactCheckRateLimiter`` plumbing all three share.
        # Off-by-default if the chat_db isn't available (in-memory
        # transient configurations); the detector silently falls
        # back to Path C (definite-only) in that case via the
        # caller's ``llm_gate=None`` branch.
        if self._chat_db is not None:
            try:
                from app.core.memory.fact_check_rate_limiter import (
                    FactCheckRateLimiter,
                )

                self._opinion_injection_rate_limiter = FactCheckRateLimiter(
                    self._chat_db,
                    per_hour_cap=int(
                        getattr(
                            self._memory_settings,
                            "opinion_injection_per_hour_cap",
                            6,
                        )
                    ),
                    per_day_cap=int(
                        getattr(
                            self._memory_settings,
                            "opinion_injection_per_day_cap",
                            30,
                        )
                    ),
                    state_key="opinion_injection.rate_state",
                )
            except Exception:
                log.warning(
                    "OpinionInjection rate limiter init failed",
                    exc_info=True,
                )
                self._opinion_injection_rate_limiter = None

        # K2 — theory-of-mind / belief tracking. Always builds the store
        # (the [[predict:...]] tag dispatch + REST endpoints need it
        # even when the worker is disabled), then conditionally builds
        # the gap detector and the inference worker. Inner-life
        # provider is registered against the prompt assembler below
        # once the detector exists.
        self._belief_store = None
        self._belief_worker = None
        self._belief_rate_limiter = None
        self._belief_gap_detector = None
        # Cached gap list produced by the post-turn detector for the
        # NEXT turn's inner-life provider. Cleared after each render.
        self._pending_belief_gaps: list[Any] = []

    def _away_activity_valence(self) -> float | None:
        """Current affect valence for the H18 idle-activity weighting.

        Best-effort: returns ``None`` (no mood tilt) if the affect store is
        missing or raises, so the away-activity worker never crashes on it.
        """
        store = getattr(self, "_affect_store", None)
        if store is None:
            return None
        try:
            state = store.get(self._user_id)
        except Exception:
            return None
        if state is None:
            return None
        try:
            return float(state.valence)
        except Exception:
            return None
