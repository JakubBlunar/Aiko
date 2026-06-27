from __future__ import annotations

from typing import Any

from app.core.infra.agent_settings import AgentSettings


def parse_agent_settings(agent_raw: dict[str, Any]) -> "AgentSettings":
    from app.core.infra.settings import (
        _normalize_approval_mode,
        _parse_approval_overrides,
        _parse_extension_list,
        _parse_file_write_settings,
        _parse_grounding_line_mode,
        _parse_task_file_allowed_roots,
        _parse_vision_settings,
    )

    return AgentSettings(
            proactive_silence_seconds=max(10.0, float(agent_raw.get("proactive_silence_seconds", 45.0))),
            proactive_cooldown_seconds=max(30.0, float(agent_raw.get("proactive_cooldown_seconds", 120.0))),
            # Typed-mode floors: silence 60s (anything shorter reads as
            # nag-y at typed speed) and cooldown 120s. The defaults are
            # well above both floors; the clamps are belt-and-braces
            # for hand-edited config files.
            proactive_typed_enabled=bool(agent_raw.get("proactive_typed_enabled", True)),
            proactive_silence_seconds_typed=max(
                60.0, float(agent_raw.get("proactive_silence_seconds_typed", 240.0)),
            ),
            proactive_cooldown_seconds_typed=max(
                120.0, float(agent_raw.get("proactive_cooldown_seconds_typed", 600.0)),
            ),
            proactive_typed_tts_enabled=bool(
                agent_raw.get("proactive_typed_tts_enabled", False)
            ),
            proactive_typed_when_away=bool(
                agent_raw.get("proactive_typed_when_away", False),
            ),
            world_notice_enabled=bool(
                agent_raw.get("world_notice_enabled", True),
            ),
            activity_awareness_enabled=bool(
                agent_raw.get("activity_awareness_enabled", False),
            ),
            fact_checker_enabled=bool(
                agent_raw.get("fact_checker_enabled", True),
            ),
            fact_checker_per_hour_cap=max(
                0, int(agent_raw.get("fact_checker_per_hour_cap", 10))
            ),
            fact_checker_per_day_cap=max(
                0, int(agent_raw.get("fact_checker_per_day_cap", 50))
            ),
            schedule_learner_enabled=bool(
                agent_raw.get("schedule_learner_enabled", True),
            ),
            schedule_learner_min_samples=max(
                1, int(agent_raw.get("schedule_learner_min_samples", 5)),
            ),
            schedule_learner_window_days=max(
                1, int(agent_raw.get("schedule_learner_window_days", 30)),
            ),
            routine_detection_enabled=bool(
                agent_raw.get("routine_detection_enabled", True),
            ),
            idle_curiosity_enabled=bool(
                agent_raw.get("idle_curiosity_enabled", True),
            ),
            idle_curiosity_per_hour_cap=max(
                0, int(agent_raw.get("idle_curiosity_per_hour_cap", 2)),
            ),
            idle_curiosity_per_day_cap=max(
                0, int(agent_raw.get("idle_curiosity_per_day_cap", 6)),
            ),
            knowledge_enrichment_enabled=bool(
                agent_raw.get("knowledge_enrichment_enabled", True),
            ),
            knowledge_enrichment_per_hour_cap=max(
                0, int(agent_raw.get("knowledge_enrichment_per_hour_cap", 1)),
            ),
            knowledge_enrichment_per_day_cap=max(
                0, int(agent_raw.get("knowledge_enrichment_per_day_cap", 4)),
            ),
            knowledge_topic_extraction_enabled=bool(
                agent_raw.get("knowledge_topic_extraction_enabled", True),
            ),
            associative_wander_enabled=bool(
                agent_raw.get("associative_wander_enabled", True),
            ),
            interest_drift_enabled=bool(
                agent_raw.get("interest_drift_enabled", True),
            ),
            curiosity_gradient_enabled=bool(
                agent_raw.get("curiosity_gradient_enabled", True),
            ),
            knowledge_map_reflection_enabled=bool(
                agent_raw.get("knowledge_map_reflection_enabled", True),
            ),
            knowledge_gap_notice_enabled=bool(
                agent_raw.get("knowledge_gap_notice_enabled", True),
            ),
            topic_temperature_enabled=bool(
                agent_raw.get("topic_temperature_enabled", True),
            ),
            topic_confidence_enabled=bool(
                agent_raw.get("topic_confidence_enabled", True),
            ),
            upcoming_horizon_enabled=bool(
                agent_raw.get("upcoming_horizon_enabled", True),
            ),
            cluster_scoped_memory_hygiene_enabled=bool(
                agent_raw.get("cluster_scoped_memory_hygiene_enabled", True),
            ),
            knowledge_grounding_enabled=bool(
                agent_raw.get("knowledge_grounding_enabled", True),
            ),
            conflict_detector_enabled=bool(
                agent_raw.get("conflict_detector_enabled", True),
            ),
            conflict_detector_per_hour_cap=max(
                0, int(agent_raw.get("conflict_detector_per_hour_cap", 6)),
            ),
            conflict_detector_per_day_cap=max(
                0, int(agent_raw.get("conflict_detector_per_day_cap", 30)),
            ),
            memory_consolidation_enabled=bool(
                agent_raw.get("memory_consolidation_enabled", True),
            ),
            memory_consolidation_per_hour_cap=max(
                0, int(agent_raw.get("memory_consolidation_per_hour_cap", 6)),
            ),
            memory_consolidation_per_day_cap=max(
                0, int(agent_raw.get("memory_consolidation_per_day_cap", 30)),
            ),
            belief_tracking_enabled=bool(
                agent_raw.get("belief_tracking_enabled", True),
            ),
            belief_worker_enabled=bool(
                agent_raw.get("belief_worker_enabled", True),
            ),
            belief_worker_per_hour_cap=max(
                0, int(agent_raw.get("belief_worker_per_hour_cap", 8)),
            ),
            belief_worker_per_day_cap=max(
                0, int(agent_raw.get("belief_worker_per_day_cap", 40)),
            ),
            promise_worker_enabled=bool(
                agent_raw.get("promise_worker_enabled", True),
            ),
            promise_worker_per_hour_cap=max(
                0, int(agent_raw.get("promise_worker_per_hour_cap", 10)),
            ),
            promise_worker_per_day_cap=max(
                0, int(agent_raw.get("promise_worker_per_day_cap", 60)),
            ),
            novelty_detection_enabled=bool(
                agent_raw.get("novelty_detection_enabled", True),
            ),
            topic_stagnation_enabled=bool(
                agent_raw.get("topic_stagnation_enabled", True),
            ),
            topic_tracking_enabled=bool(
                agent_raw.get("topic_tracking_enabled", True),
            ),
            topic_graph_enabled=bool(
                agent_raw.get("topic_graph_enabled", True),
            ),
            topic_graph_persistent_enabled=bool(
                agent_raw.get("topic_graph_persistent_enabled", True),
            ),
            topic_graph_rebuild_interval_seconds=max(
                60.0,
                float(agent_raw.get(
                    "topic_graph_rebuild_interval_seconds", 86_400.0,
                )),
            ),
            topic_graph_refit_pending_threshold=max(
                1,
                int(agent_raw.get(
                    "topic_graph_refit_pending_threshold", 25,
                )),
            ),
            topic_label_enabled=bool(
                agent_raw.get("topic_label_enabled", True),
            ),
            topic_label_interval_seconds=max(
                60.0,
                float(agent_raw.get("topic_label_interval_seconds", 1800.0)),
            ),
            topic_label_max_per_run=max(
                1,
                int(agent_raw.get("topic_label_max_per_run", 4)),
            ),
            topic_label_max_tokens=max(
                8,
                int(agent_raw.get("topic_label_max_tokens", 32)),
            ),
            topic_digest_enabled=bool(
                agent_raw.get("topic_digest_enabled", True),
            ),
            topic_digest_interval_seconds=max(
                60.0,
                float(agent_raw.get("topic_digest_interval_seconds", 3600.0)),
            ),
            topic_digest_max_per_run=max(
                1,
                int(agent_raw.get("topic_digest_max_per_run", 3)),
            ),
            topic_digest_max_tokens=max(
                32,
                int(agent_raw.get("topic_digest_max_tokens", 256)),
            ),
            topic_digest_min_cluster_size=max(
                2,
                int(agent_raw.get("topic_digest_min_cluster_size", 6)),
            ),
            topic_digest_surface_in_rag=bool(
                agent_raw.get("topic_digest_surface_in_rag", True),
            ),
            rag_cluster_diversity_enabled=bool(
                agent_raw.get("rag_cluster_diversity_enabled", True),
            ),
            rag_max_per_cluster=max(
                1,
                int(agent_raw.get("rag_max_per_cluster", 3)),
            ),
            rag_topic_expansion_enabled=bool(
                agent_raw.get("rag_topic_expansion_enabled", True),
            ),
            rag_expand_max=max(
                0,
                int(agent_raw.get("rag_expand_max", 2)),
            ),
            rag_expand_trigger_score=float(
                agent_raw.get("rag_expand_trigger_score", 0.55),
            ),
            rag_expand_min_sim=float(
                agent_raw.get("rag_expand_min_sim", 0.45),
            ),
            rag_digest_sibling_cap=max(
                0,
                int(agent_raw.get("rag_digest_sibling_cap", 1)),
            ),
            rag_direct_recall_enabled=bool(
                agent_raw.get("rag_direct_recall_enabled", True),
            ),
            rag_direct_recall_max_messages=max(
                0,
                int(agent_raw.get("rag_direct_recall_max_messages", 6)),
            ),
            interest_map_enabled=bool(
                agent_raw.get("interest_map_enabled", True),
            ),
            interest_map_max_clusters=max(
                1,
                int(agent_raw.get("interest_map_max_clusters", 5)),
            ),
            interest_map_min_size=max(
                1,
                int(agent_raw.get("interest_map_min_size", 4)),
            ),
            curiosity_seed_enabled=bool(
                agent_raw.get("curiosity_seed_enabled", True),
            ),
            curiosity_seed_max_active=max(
                1, int(agent_raw.get("curiosity_seed_max_active", 6)),
            ),
            curiosity_seed_max_per_run=max(
                1, int(agent_raw.get("curiosity_seed_max_per_run", 2)),
            ),
            curiosity_seed_min_novelty=max(
                0.0,
                min(1.0, float(agent_raw.get("curiosity_seed_min_novelty", 0.85))),
            ),
            curiosity_seed_resolve_threshold=max(
                0.0,
                min(
                    1.0,
                    float(agent_raw.get(
                        "curiosity_seed_resolve_threshold", 0.50,
                    )),
                ),
            ),
            pre_thought_enabled=bool(
                agent_raw.get("pre_thought_enabled", True),
            ),
            pre_thought_max_active=max(
                1, int(agent_raw.get("pre_thought_max_active", 12)),
            ),
            pre_thought_candidates=max(
                1, int(agent_raw.get("pre_thought_candidates", 4)),
            ),
            pre_thought_max_per_run=max(
                1, int(agent_raw.get("pre_thought_max_per_run", 2)),
            ),
            pre_thought_min_novelty=max(
                0.0,
                min(1.0, float(agent_raw.get("pre_thought_min_novelty", 0.85))),
            ),
            pre_thought_per_hour_cap=max(
                0, int(agent_raw.get("pre_thought_per_hour_cap", 6)),
            ),
            pre_thought_per_day_cap=max(
                0, int(agent_raw.get("pre_thought_per_day_cap", 40)),
            ),
            thread_resummary_enabled=bool(
                agent_raw.get("thread_resummary_enabled", True),
            ),
            thread_resummary_min_messages=max(
                1, int(agent_raw.get("thread_resummary_min_messages", 12)),
            ),
            thread_resummary_message_interval=max(
                1, int(agent_raw.get("thread_resummary_message_interval", 50)),
            ),
            thread_resummary_max_age_hours=max(
                0.0, float(agent_raw.get("thread_resummary_max_age_hours", 24.0)),
            ),
            thread_resummary_per_hour_cap=max(
                0, int(agent_raw.get("thread_resummary_per_hour_cap", 6)),
            ),
            thread_resummary_per_day_cap=max(
                0, int(agent_raw.get("thread_resummary_per_day_cap", 24)),
            ),
            topic_graph_filter_threshold=max(
                0.0,
                min(
                    1.0,
                    float(agent_raw.get(
                        "topic_graph_filter_threshold", 0.65,
                    )),
                ),
            ),
            wants_ledger_enabled=bool(
                agent_raw.get("wants_ledger_enabled", True),
            ),
            wants_growth_per_day=max(
                0.0, float(agent_raw.get("wants_growth_per_day", 0.25)),
            ),
            wants_imperative_threshold=max(
                0.0,
                min(
                    1.0,
                    float(agent_raw.get("wants_imperative_threshold", 0.7)),
                ),
            ),
            wants_cap=max(1, int(agent_raw.get("wants_cap", 8))),
            wants_max_age_days=max(
                1.0, float(agent_raw.get("wants_max_age_days", 14.0)),
            ),
            wants_reentry_cooldown_days=max(
                0.0,
                float(agent_raw.get("wants_reentry_cooldown_days", 5.0)),
            ),
            wants_worker_interval_seconds=max(
                30.0,
                float(agent_raw.get("wants_worker_interval_seconds", 3600.0)),
            ),
            initiative_turns_enabled=bool(
                agent_raw.get("initiative_turns_enabled", True),
            ),
            initiative_base_period=max(
                3, int(agent_raw.get("initiative_base_period", 8)),
            ),
            initiative_warmup_turns=max(
                0, int(agent_raw.get("initiative_warmup_turns", 3)),
            ),
            initiative_substantial_chars=max(
                1,
                int(agent_raw.get("initiative_substantial_chars", 240)),
            ),
            thread_ownership_enabled=bool(
                agent_raw.get("thread_ownership_enabled", True),
            ),
            thread_engaged_chars=max(
                1, int(agent_raw.get("thread_engaged_chars", 80)),
            ),
            thread_min_topical_similarity=min(
                1.0,
                max(
                    0.0,
                    float(
                        agent_raw.get("thread_min_topical_similarity", 0.30)
                    ),
                ),
            ),
            topic_appetite_enabled=bool(
                agent_raw.get("topic_appetite_enabled", True),
            ),
            appetite_short_reply_chars=max(
                1, int(agent_raw.get("appetite_short_reply_chars", 160)),
            ),
            appetite_short_share_threshold=min(
                1.0,
                max(
                    0.0,
                    float(
                        agent_raw.get("appetite_short_share_threshold", 0.6)
                    ),
                ),
            ),
            appetite_window=max(
                2, int(agent_raw.get("appetite_window", 6)),
            ),
            appetite_min_want_pressure=max(
                0.0,
                float(agent_raw.get("appetite_min_want_pressure", 0.35)),
            ),
            appetite_min_axes=min(
                1.0,
                max(
                    -1.0,
                    float(agent_raw.get("appetite_min_axes", 0.15)),
                ),
            ),
            emotion_episodes_enabled=bool(
                agent_raw.get("emotion_episodes_enabled", True),
            ),
            emotion_episode_cap=max(
                1, int(agent_raw.get("emotion_episode_cap", 3)),
            ),
            emotion_lonely_threshold_hours=max(
                0.5,
                float(
                    agent_raw.get("emotion_lonely_threshold_hours", 5.0)
                ),
            ),
            emotion_high_band=min(
                1.0,
                max(
                    0.0,
                    float(agent_raw.get("emotion_high_band", 0.5)),
                ),
            ),
            tease_economy_enabled=bool(
                agent_raw.get("tease_economy_enabled", True),
            ),
            tease_cap=max(1, int(agent_raw.get("tease_cap", 5))),
            tease_expiry_days=max(
                0.5, float(agent_raw.get("tease_expiry_days", 14.0)),
            ),
            tease_collect_cooldown_hours=max(
                0.0,
                float(
                    agent_raw.get("tease_collect_cooldown_hours", 12.0)
                ),
            ),
            tease_min_humor=min(
                1.0,
                max(
                    -1.0, float(agent_raw.get("tease_min_humor", 0.2)),
                ),
            ),
            tease_min_age_hours=max(
                0.0, float(agent_raw.get("tease_min_age_hours", 1.0)),
            ),
            expression_mask=(
                str(agent_raw.get("expression_mask", "off")).strip().lower()
                if str(
                    agent_raw.get("expression_mask", "off")
                ).strip().lower()
                in ("off", "tsundere_light", "tsundere_full")
                else "off"
            ),
            mask_slip_cooldown_days=max(
                0.0,
                float(agent_raw.get("mask_slip_cooldown_days", 2.0)),
            ),
            grounding_line_mode=_parse_grounding_line_mode(
                agent_raw.get("grounding_line_mode", "off"),
            ),
            history_age_prefix_enabled=bool(
                agent_raw.get("history_age_prefix_enabled", True),
            ),
            cue_register_rotation_enabled=bool(
                agent_raw.get("cue_register_rotation_enabled", True),
            ),
            goals_enabled=bool(
                agent_raw.get("goals_enabled", True),
            ),
            goal_worker_bootstrap_enabled=bool(
                agent_raw.get("goal_worker_bootstrap_enabled", True),
            ),
            goal_worker_per_hour_cap=max(
                0, int(agent_raw.get("goal_worker_per_hour_cap", 3)),
            ),
            goal_worker_per_day_cap=max(
                0, int(agent_raw.get("goal_worker_per_day_cap", 12)),
            ),
            shared_moments_enabled=bool(
                agent_raw.get("shared_moments_enabled", True),
            ),
            shared_moments_llm_enabled=bool(
                agent_raw.get("shared_moments_llm_enabled", True),
            ),
            shared_moments_min_turn_gap=max(
                1, int(agent_raw.get("shared_moments_min_turn_gap", 5)),
            ),
            shared_moments_cooldown_seconds=max(
                30.0,
                float(agent_raw.get("shared_moments_cooldown_seconds", 300.0)),
            ),
            anniversary_surfacing_enabled=bool(
                agent_raw.get("anniversary_surfacing_enabled", True),
            ),
            relationship_axes_enabled=bool(
                agent_raw.get("relationship_axes_enabled", True),
            ),
            milestone_celebration_enabled=bool(
                agent_raw.get("milestone_celebration_enabled", True),
            ),
            reconnection_enabled=bool(
                agent_raw.get("reconnection_enabled", True),
            ),
            reconnection_base_gap_hours=max(
                1.0, float(agent_raw.get("reconnection_base_gap_hours", 24.0)),
            ),
            session_clock_enabled=bool(
                agent_raw.get("session_clock_enabled", True),
            ),
            session_clock_long_minutes=max(
                1.0, float(agent_raw.get("session_clock_long_minutes", 60.0)),
            ),
            session_clock_very_long_minutes=max(
                1.0,
                float(
                    agent_raw.get("session_clock_very_long_minutes", 150.0)
                ),
            ),
            session_clock_break_minutes=max(
                1.0, float(agent_raw.get("session_clock_break_minutes", 30.0)),
            ),
            session_clock_gap_min_minutes=max(
                0.0, float(agent_raw.get("session_clock_gap_min_minutes", 10.0)),
            ),
            session_clock_gap_max_minutes=max(
                0.0, float(agent_raw.get("session_clock_gap_max_minutes", 30.0)),
            ),
            appreciation_beats_enabled=bool(
                agent_raw.get("appreciation_beats_enabled", True),
            ),
            appreciation_min_closeness=max(
                -1.0, min(1.0, float(
                    agent_raw.get("appreciation_min_closeness", 0.25),
                )),
            ),
            appreciation_cooldown_hours=max(
                1.0, float(agent_raw.get("appreciation_cooldown_hours", 72.0)),
            ),
            appreciation_max_anchor_age_days=max(
                1.0,
                float(agent_raw.get("appreciation_max_anchor_age_days", 21.0)),
            ),
            reciprocal_vulnerability_enabled=bool(
                agent_raw.get("reciprocal_vulnerability_enabled", True),
            ),
            reciprocal_vulnerability_cooldown_hours=max(
                1.0,
                float(
                    agent_raw.get(
                        "reciprocal_vulnerability_cooldown_hours", 96.0,
                    )
                ),
            ),
            reciprocal_vulnerability_min_trust=max(
                -1.0, min(1.0, float(
                    agent_raw.get("reciprocal_vulnerability_min_trust", 0.2),
                )),
            ),
            conflict_repair_enabled=bool(
                agent_raw.get("conflict_repair_enabled", True),
            ),
            conflict_repair_watch_turns=max(
                1, int(agent_raw.get("conflict_repair_watch_turns", 5)),
            ),
            conflict_repair_recovery_epsilon=max(
                0.0, float(
                    agent_raw.get("conflict_repair_recovery_epsilon", 0.05),
                ),
            ),
            conflict_repair_min_recovery_rise=max(
                0.0, float(
                    agent_raw.get("conflict_repair_min_recovery_rise", 0.10),
                ),
            ),
            conflict_repair_cooldown_hours=max(
                0.0, float(
                    agent_raw.get("conflict_repair_cooldown_hours", 12.0),
                ),
            ),
            summary_idle_seconds=max(2.0, float(agent_raw.get("summary_idle_seconds", 15.0))),
            summary_min_unsummarized_messages=max(2, int(agent_raw.get("summary_min_unsummarized_messages", 6))),
            summary_target_tokens=max(120, int(agent_raw.get("summary_target_tokens", 600))),
            max_prompt_tokens_pct=max(0.3, min(0.95, float(agent_raw.get("max_prompt_tokens_pct", 0.8)))),
            scheduler_idle_seconds=max(2.0, float(agent_raw.get("scheduler_idle_seconds", 20.0))),
            scheduler_speaking_window_grace_ms=max(0, int(agent_raw.get("scheduler_speaking_window_grace_ms", 200))),
            scheduler_max_job_seconds=max(1.0, float(agent_raw.get("scheduler_max_job_seconds", 8.0))),
            reflection_min_seconds_between=max(0.0, float(agent_raw.get("reflection_min_seconds_between", 8.0))),
            reflection_emotional_delta_threshold=max(0.0, float(agent_raw.get("reflection_emotional_delta_threshold", 0.05))),
            user_profile_min_turns=max(1, int(agent_raw.get("user_profile_min_turns", 6))),
            agenda_groom_every_n_turns=max(1, int(agent_raw.get("agenda_groom_every_n_turns", 8))),
            arc_update_every_n_turns=max(1, int(agent_raw.get("arc_update_every_n_turns", 1))),
            self_image_pulse_enabled=bool(agent_raw.get("self_image_pulse_enabled", True)),
            self_image_max_tokens=max(120, int(agent_raw.get("self_image_max_tokens", 320))),
            prepared_nudge_ttl_seconds=max(30.0, float(agent_raw.get("prepared_nudge_ttl_seconds", 600.0))),
            filler_enabled=bool(agent_raw.get("filler_enabled", True)),
            filler_first_token_ms=max(150, int(agent_raw.get("filler_first_token_ms", 800))),
            tool_pass_gate_enabled=bool(agent_raw.get("tool_pass_gate_enabled", True)),
            skill_router_enabled=bool(agent_raw.get("skill_router_enabled", False)),
            brain_core_skills=tuple(
                str(s).strip()
                for s in (
                    agent_raw.get("brain_core_skills")
                    if isinstance(agent_raw.get("brain_core_skills"), list)
                    else ["time", "recall", "world"]
                )
                if str(s).strip()
            )
            or ("time", "recall", "world"),
            workflow_skill_router_enabled=bool(
                agent_raw.get("workflow_skill_router_enabled", False)
            ),
            consolidator_enabled=bool(agent_raw.get("consolidator_enabled", True)),
            consolidator_min_hours_between=max(0.5, float(agent_raw.get("consolidator_min_hours_between", 18.0))),
            consolidator_chunk_size=max(8, int(agent_raw.get("consolidator_chunk_size", 40))),
            consolidator_similarity_threshold=max(0.5, min(0.99, float(agent_raw.get("consolidator_similarity_threshold", 0.84)))),
            consolidator_min_cluster_size=max(2, int(agent_raw.get("consolidator_min_cluster_size", 2))),
            consolidator_use_llm_merge=bool(agent_raw.get("consolidator_use_llm_merge", True)),
            relationship_pulse_enabled=bool(agent_raw.get("relationship_pulse_enabled", True)),
            relationship_pulse_min_hours=max(24.0, float(agent_raw.get("relationship_pulse_min_hours", 168.0))),
            relationship_pulse_min_turns=max(5, int(agent_raw.get("relationship_pulse_min_turns", 30))),
            relationship_pulse_max_tokens=max(80, int(agent_raw.get("relationship_pulse_max_tokens", 256))),
            cadence_enabled=bool(agent_raw.get("cadence_enabled", True)),
            earcon_auto_sprinkle=bool(
                agent_raw.get("earcon_auto_sprinkle", True),
            ),
            tts_runtime_temp_enabled=bool(
                agent_raw.get("tts_runtime_temp_enabled", False),
            ),
            tts_runtime_speed_enabled=bool(
                agent_raw.get("tts_runtime_speed_enabled", False),
            ),
            style_tracker_enabled=bool(
                agent_raw.get("style_tracker_enabled", True),
            ),
            style_tracker_window=max(
                2, int(agent_raw.get("style_tracker_window", 12)),
            ),
            style_tracker_warmup=max(
                2, int(agent_raw.get("style_tracker_warmup", 6)),
            ),
            style_tracker_opener_count_threshold=max(
                2,
                int(
                    agent_raw.get(
                        "style_tracker_opener_count_threshold", 4,
                    )
                ),
            ),
            style_tracker_opener_topk_share=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_tracker_opener_topk_share", 0.60,
                        )
                    ),
                ),
            ),
            style_tracker_question_rate_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_tracker_question_rate_threshold", 0.75,
                        )
                    ),
                ),
            ),
            style_tracker_avg_questions_threshold=max(
                0.0,
                float(
                    agent_raw.get(
                        "style_tracker_avg_questions_threshold", 1.5,
                    )
                ),
            ),
            style_tracker_length_avg_threshold=max(
                1.0,
                float(
                    agent_raw.get(
                        "style_tracker_length_avg_threshold", 50.0,
                    )
                ),
            ),
            style_tracker_cue_cooldown_turns=max(
                0,
                int(
                    agent_raw.get("style_tracker_cue_cooldown_turns", 5)
                ),
            ),
            question_balance_enabled=bool(
                agent_raw.get("question_balance_enabled", True),
            ),
            question_balance_ratio_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get("question_balance_ratio_threshold", 0.55)
                    ),
                ),
            ),
            question_balance_window=max(
                2, int(agent_raw.get("question_balance_window", 10)),
            ),
            question_balance_suppress_turns=max(
                0, int(agent_raw.get("question_balance_suppress_turns", 2)),
            ),
            tease_rhythm_enabled=bool(
                agent_raw.get("tease_rhythm_enabled", True),
            ),
            tease_rhythm_window=max(
                2, int(agent_raw.get("tease_rhythm_window", 6)),
            ),
            tease_rhythm_consecutive_cap=max(
                1, int(agent_raw.get("tease_rhythm_consecutive_cap", 3)),
            ),
            tease_rhythm_green_light_humor=max(
                -1.0,
                min(
                    1.0,
                    float(
                        agent_raw.get("tease_rhythm_green_light_humor", 0.2)
                    ),
                ),
            ),
            tease_rhythm_cooldown_turns=max(
                0, int(agent_raw.get("tease_rhythm_cooldown_turns", 3)),
            ),
            style_signal_enabled=bool(
                agent_raw.get("style_signal_enabled", True),
            ),
            style_signal_window=max(
                2, int(agent_raw.get("style_signal_window", 30)),
            ),
            style_signal_warmup_min=max(
                2, int(agent_raw.get("style_signal_warmup_min", 8)),
            ),
            style_signal_terse_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_terse_threshold", 0.55,
                        )
                    ),
                ),
            ),
            style_signal_formal_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_formal_threshold", 0.55,
                        )
                    ),
                ),
            ),
            style_signal_emoji_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_emoji_threshold", 0.05,
                        )
                    ),
                ),
            ),
            style_signal_slang_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_slang_threshold", 0.15,
                        )
                    ),
                ),
            ),
            style_signal_question_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "style_signal_question_threshold", 0.40,
                        )
                    ),
                ),
            ),
            engagement_tracker_enabled=bool(
                agent_raw.get("engagement_tracker_enabled", True),
            ),
            engagement_window=max(
                2, int(agent_raw.get("engagement_window", 12)),
            ),
            engagement_warmup_min=max(
                2, int(agent_raw.get("engagement_warmup_min", 6)),
            ),
            engagement_latency_z_strong_drop=max(
                0.1,
                float(
                    agent_raw.get("engagement_latency_z_strong_drop", 1.5),
                ),
            ),
            engagement_length_z_strong_drop=min(
                -0.1,
                float(
                    agent_raw.get("engagement_length_z_strong_drop", -1.0),
                ),
            ),
            engagement_closeness_delta_max=max(
                0.0,
                min(
                    0.08,
                    float(
                        agent_raw.get(
                            "engagement_closeness_delta_max", 0.04,
                        )
                    ),
                ),
            ),
            engagement_absence_curiosity_enabled=bool(
                agent_raw.get(
                    "engagement_absence_curiosity_enabled", True,
                ),
            ),
            engagement_absence_curiosity_min_seconds=max(
                60.0,
                float(
                    agent_raw.get(
                        "engagement_absence_curiosity_min_seconds",
                        1800.0,
                    )
                ),
            ),
            engagement_proactive_gate=bool(
                agent_raw.get("engagement_proactive_gate", True),
            ),
            mood_shell_enabled=bool(
                agent_raw.get("mood_shell_enabled", True),
            ),
            mood_shell_axis_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get("mood_shell_axis_threshold", 0.5),
                    ),
                ),
            ),
            clarification_repair_enabled=bool(
                agent_raw.get("clarification_repair_enabled", True),
            ),
            rupture_repair_enabled=bool(
                agent_raw.get("rupture_repair_enabled", True),
            ),
            rupture_valence_drop_threshold=max(
                0.0,
                min(
                    2.0,
                    float(
                        agent_raw.get(
                            "rupture_valence_drop_threshold", 0.12,
                        )
                    ),
                ),
            ),
            contagion_enabled=bool(
                agent_raw.get("contagion_enabled", True),
            ),
            contagion_strength=max(
                0.0,
                min(1.0, float(agent_raw.get("contagion_strength", 0.15))),
            ),
            contagion_max_per_turn=max(
                0.0,
                min(0.5, float(agent_raw.get("contagion_max_per_turn", 0.05))),
            ),
            misattunement_detection_enabled=bool(
                agent_raw.get("misattunement_detection_enabled", True),
            ),
            misattunement_shrink_min_prev_words=max(
                0,
                int(
                    agent_raw.get(
                        "misattunement_shrink_min_prev_words", 30,
                    )
                ),
            ),
            misattunement_shrink_max_user_words=max(
                0,
                int(
                    agent_raw.get(
                        "misattunement_shrink_max_user_words", 8,
                    )
                ),
            ),
            misattunement_pivot_max_user_words=max(
                0,
                int(
                    agent_raw.get(
                        "misattunement_pivot_max_user_words", 8,
                    )
                ),
            ),
            misattunement_cooldown_turns=max(
                0,
                int(
                    agent_raw.get(
                        "misattunement_cooldown_turns", 3,
                    )
                ),
            ),
            self_noticing_enabled=bool(
                agent_raw.get("self_noticing_enabled", True),
            ),
            self_noticing_agreement_streak_enabled=bool(
                agent_raw.get(
                    "self_noticing_agreement_streak_enabled", True,
                ),
            ),
            self_noticing_flat_affect_enabled=bool(
                agent_raw.get(
                    "self_noticing_flat_affect_enabled", True,
                ),
            ),
            self_noticing_repeated_thought_enabled=bool(
                agent_raw.get(
                    "self_noticing_repeated_thought_enabled", True,
                ),
            ),
            self_noticing_window=max(
                1,
                int(agent_raw.get("self_noticing_window", 6)),
            ),
            self_noticing_warmup=max(
                1,
                int(agent_raw.get("self_noticing_warmup", 4)),
            ),
            self_noticing_agreement_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "self_noticing_agreement_threshold", 0.80,
                        )
                    ),
                ),
            ),
            self_noticing_max_pushback=max(
                0,
                int(agent_raw.get("self_noticing_max_pushback", 0)),
            ),
            self_noticing_flat_valence_range=max(
                0.0,
                float(
                    agent_raw.get(
                        "self_noticing_flat_valence_range", 0.10,
                    )
                ),
            ),
            self_noticing_flat_arousal_range=max(
                0.0,
                float(
                    agent_raw.get(
                        "self_noticing_flat_arousal_range", 0.10,
                    )
                ),
            ),
            self_noticing_repeated_cosine_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "self_noticing_repeated_cosine_threshold", 0.85,
                        )
                    ),
                ),
            ),
            self_noticing_cooldown_turns=max(
                0,
                int(agent_raw.get("self_noticing_cooldown_turns", 5)),
            ),
            day_color_enabled=bool(
                agent_raw.get("day_color_enabled", True),
            ),
            day_color_check_interval_seconds=max(
                60,
                int(
                    agent_raw.get("day_color_check_interval_seconds", 3600)
                ),
            ),
            vulnerability_budget_enabled=bool(
                agent_raw.get("vulnerability_budget_enabled", True),
            ),
            vulnerability_budget_min_capacity=max(
                1, int(agent_raw.get("vulnerability_budget_min_capacity", 1)),
            ),
            vulnerability_budget_max_capacity=max(
                1, int(agent_raw.get("vulnerability_budget_max_capacity", 12)),
            ),
            vulnerability_budget_regen_per_hour=max(
                0.01,
                float(
                    agent_raw.get("vulnerability_budget_regen_per_hour", 0.5)
                ),
            ),
            vulnerability_budget_tier1_cost=max(
                0, int(agent_raw.get("vulnerability_budget_tier1_cost", 1)),
            ),
            vulnerability_budget_tier2_cost=max(
                0, int(agent_raw.get("vulnerability_budget_tier2_cost", 3)),
            ),
            vulnerability_budget_tier3_cost=max(
                0, int(agent_raw.get("vulnerability_budget_tier3_cost", 6)),
            ),
            touch_enabled=bool(
                agent_raw.get("touch_enabled", True),
            ),
            touch_per_kind_overrides=(
                dict(agent_raw.get("touch_per_kind_overrides", {}))
                if isinstance(
                    agent_raw.get("touch_per_kind_overrides"), dict,
                )
                else {}
            ),
            persona_regression_enabled=bool(
                agent_raw.get("persona_regression_enabled", True),
            ),
            persona_regression_fixture_path=str(
                agent_raw.get(
                    "persona_regression_fixture_path",
                    "data/persona/golden_turns.jsonl",
                ),
            ),
            tasks_enabled=bool(agent_raw.get("tasks_enabled", True)),
            tasks_per_user_cap=max(
                1, int(agent_raw.get("tasks_per_user_cap", 8))
            ),
            tasks_resume_on_boot=bool(
                agent_raw.get("tasks_resume_on_boot", True)
            ),
            tasks_running_block_enabled=bool(
                agent_raw.get("tasks_running_block_enabled", True)
            ),
            brain_loop_deferred_grace_ms=max(
                10,
                min(
                    5000,
                    int(agent_raw.get("brain_loop_deferred_grace_ms", 100)),
                ),
            ),
            task_cue_max_age_seconds=max(
                60,
                min(
                    86400,
                    int(
                        agent_raw.get("task_cue_max_age_seconds", 1800)
                    ),
                ),
            ),
            task_cue_max_aggregated=max(
                1,
                min(
                    20,
                    int(agent_raw.get("task_cue_max_aggregated", 5)),
                ),
            ),
            task_reply_on_complete_enabled=bool(
                agent_raw.get("task_reply_on_complete_enabled", True)
            ),
            task_inline_grace_seconds=max(
                0.0,
                min(
                    30.0,
                    float(agent_raw.get("task_inline_grace_seconds", 3.0)),
                ),
            ),
            task_report_decision_enabled=bool(
                agent_raw.get("task_report_decision_enabled", True)
            ),
            task_report_decision_floor_mode=(
                str(
                    agent_raw.get("task_report_decision_floor_mode", "shadow")
                ).strip().lower()
                if str(
                    agent_raw.get("task_report_decision_floor_mode", "shadow")
                ).strip().lower()
                in ("shadow", "enforce")
                else "shadow"
            ),
            task_report_angle_enabled=bool(
                agent_raw.get("task_report_angle_enabled", True)
            ),
            task_file_allowed_roots=_parse_task_file_allowed_roots(
                agent_raw.get("task_file_allowed_roots", ())
            ),
            builtin_file_skills_enabled=bool(
                agent_raw.get("builtin_file_skills_enabled", True)
            ),
            task_file_read_max_bytes=max(
                1024,
                min(
                    16 * 1024 * 1024,
                    int(agent_raw.get("task_file_read_max_bytes", 262144)),
                ),
            ),
            task_file_read_max_lines=max(
                10,
                min(
                    50000,
                    int(agent_raw.get("task_file_read_max_lines", 2000)),
                ),
            ),
            task_file_read_allowed_extensions=_parse_extension_list(
                agent_raw.get(
                    "task_file_read_allowed_extensions",
                    (
                        ".txt", ".md", ".rst", ".log",
                        ".py", ".js", ".ts", ".tsx", ".jsx",
                        ".json", ".yaml", ".yml", ".toml",
                        ".ini", ".cfg", ".conf",
                        ".html", ".css", ".xml",
                        ".csv", ".tsv",
                        ".sh", ".bat", ".ps1",
                        ".sql",
                        ".go", ".rs", ".c", ".h", ".cpp", ".hpp",
                        ".java", ".kt",
                        ".rb", ".lua",
                    ),
                )
            ),
            mcp_clients_enabled=bool(agent_raw.get("mcp_clients_enabled", True)),
            workflow_enabled=bool(agent_raw.get("workflow_enabled", True)),
            workflow_max_iterations=max(
                1, min(30, int(agent_raw.get("workflow_max_iterations", 6)))
            ),
            workflow_max_children=max(
                1, min(50, int(agent_raw.get("workflow_max_children", 8)))
            ),
            workflow_max_concurrent=max(
                1, min(8, int(agent_raw.get("workflow_max_concurrent", 2)))
            ),
            workflow_planner_history_budget_chars=max(
                500,
                min(
                    20000,
                    int(
                        agent_raw.get(
                            "workflow_planner_history_budget_chars", 4000
                        )
                    ),
                ),
            ),
            workflow_reply_budget_chars=max(
                1000,
                min(
                    40000,
                    int(agent_raw.get("workflow_reply_budget_chars", 6000)),
                ),
            ),
            workflow_child_wait_timeout_seconds=max(
                5,
                min(
                    600,
                    int(
                        agent_raw.get(
                            "workflow_child_wait_timeout_seconds", 120
                        )
                    ),
                ),
            ),
            workflow_planner_max_tokens=max(
                64,
                min(
                    2048,
                    int(agent_raw.get("workflow_planner_max_tokens", 512)),
                ),
            ),
            workflow_max_consecutive_failures=max(
                1,
                min(
                    20,
                    int(agent_raw.get("workflow_max_consecutive_failures", 2)),
                ),
            ),
            workflow_max_wall_seconds=max(
                0,
                min(
                    3600,
                    int(agent_raw.get("workflow_max_wall_seconds", 300)),
                ),
            ),
            workflow_capability_gap_log_max=max(
                1,
                min(
                    500,
                    int(agent_raw.get("workflow_capability_gap_log_max", 50)),
                ),
            ),
            task_approval_mode=_normalize_approval_mode(
                agent_raw.get("task_approval_mode", "ask")
            ),
            task_approval_overrides=_parse_approval_overrides(
                agent_raw.get("task_approval_overrides", {})
            ),
            file_write=_parse_file_write_settings(
                agent_raw.get("file_write", {})
            ),
            vision=_parse_vision_settings(
                agent_raw.get("vision", {})
            ),
            worker_llm_gate_enabled=bool(
                agent_raw.get("worker_llm_gate_enabled", True)
            ),
            worker_llm_max_concurrency=max(
                1, min(8, int(agent_raw.get("worker_llm_max_concurrency", 1)))
            ),
            worker_llm_priority_overrides=(
                {
                    str(k): str(v)
                    for k, v in agent_raw.get(
                        "worker_llm_priority_overrides", {}
                    ).items()
                }
                if isinstance(
                    agent_raw.get("worker_llm_priority_overrides"), dict
                )
                else {}
            ),
            user_reactions_enabled=bool(
                agent_raw.get("user_reactions_enabled", True),
            ),
            user_reactions_axes_enabled=bool(
                agent_raw.get("user_reactions_axes_enabled", True),
            ),
            user_reactions_daily_axis_cap=max(
                0.0,
                float(
                    agent_raw.get("user_reactions_daily_axis_cap", 0.15),
                ),
            ),
            persona_touch_banner_enabled=bool(
                agent_raw.get("persona_touch_banner_enabled", True),
            ),
            persona_touch_banner_duration_seconds=max(
                1,
                min(
                    120,
                    int(
                        agent_raw.get(
                            "persona_touch_banner_duration_seconds", 20,
                        )
                    ),
                ),
            ),
            persona_task_banner_enabled=bool(
                agent_raw.get("persona_task_banner_enabled", True),
            ),
            task_heartbeat_check_interval_seconds=max(
                5,
                min(
                    3600,
                    int(
                        agent_raw.get(
                            "task_heartbeat_check_interval_seconds", 30
                        )
                    ),
                ),
            ),
            task_stalled_seconds=max(
                60,
                min(
                    86400,
                    int(agent_raw.get("task_stalled_seconds", 300)),
                ),
            ),
            task_stalled_action=(
                str(agent_raw.get("task_stalled_action", "warn")).strip().lower()
                if str(agent_raw.get("task_stalled_action", "warn")).strip().lower()
                in ("warn", "fail")
                else "warn"
            ),
            task_cascade_cancel_children=bool(
                agent_raw.get("task_cascade_cancel_children", True),
            ),
            task_cleanup_retention_days=max(
                1,
                min(
                    3650,
                    int(agent_raw.get("task_cleanup_retention_days", 30)),
                ),
            ),
            task_cleanup_interval_seconds=max(
                600,
                min(
                    604800,
                    int(
                        agent_raw.get("task_cleanup_interval_seconds", 21600)
                    ),
                ),
            ),
            opinion_injection_enabled=bool(
                agent_raw.get("opinion_injection_enabled", True),
            ),
            opinion_injection_require_definite=bool(
                agent_raw.get("opinion_injection_require_definite", False),
            ),
            turning_over_enabled=bool(
                agent_raw.get("turning_over_enabled", True),
            ),
            away_activities_enabled=bool(
                agent_raw.get("away_activities_enabled", True),
            ),
            diary_worker_enabled=bool(
                agent_raw.get("diary_worker_enabled", True),
            ),
            forward_curiosity_enabled=bool(
                agent_raw.get("forward_curiosity_enabled", True),
            ),
            follow_up_enabled=bool(
                agent_raw.get("follow_up_enabled", True),
            ),
            promise_followthrough_enabled=bool(
                agent_raw.get("promise_followthrough_enabled", True),
            ),
            self_correction_enabled=bool(
                agent_raw.get("self_correction_enabled", True),
            ),
            mood_inertia_enabled=bool(
                agent_raw.get("mood_inertia_enabled", True),
            ),
            confidence_time_decay_enabled=bool(
                agent_raw.get("confidence_time_decay_enabled", True),
            ),
            callback_detector_enabled=bool(
                agent_raw.get("callback_detector_enabled", True),
            ),
            calibration_detection_enabled=bool(
                agent_raw.get("calibration_detection_enabled", True),
            ),
            sensory_anchor_enabled=bool(
                agent_raw.get("sensory_anchor_enabled", True),
            ),
            resume_opener_min_hours=max(0.0, float(agent_raw.get("resume_opener_min_hours", 4.0))),
            resume_opener_ttl_seconds=max(60.0, float(agent_raw.get("resume_opener_ttl_seconds", 1800.0))),
            dream_worker_enabled=bool(agent_raw.get("dream_worker_enabled", True)),
            dream_worker_min_hours_since_last=max(
                0.0, float(agent_raw.get("dream_worker_min_hours_since_last", 6.0)),
            ),
            catchphrase_miner_enabled=bool(agent_raw.get("catchphrase_miner_enabled", True)),
            catchphrase_miner_min_seconds_between=max(
                30.0, float(agent_raw.get("catchphrase_miner_min_seconds_between", 600.0)),
            ),
            catchphrase_miner_min_new_user_turns=max(
                1, int(agent_raw.get("catchphrase_miner_min_new_user_turns", 6)),
            ),
            catchphrase_miner_min_total_count=max(
                2, int(agent_raw.get("catchphrase_miner_min_total_count", 3)),
            ),
            curiosity_worker_enabled=bool(
                agent_raw.get("curiosity_worker_enabled", True),
            ),
            curiosity_worker_min_turns_between=max(
                1, int(agent_raw.get("curiosity_worker_min_turns_between", 3)),
            ),
            curiosity_worker_min_seconds_between=max(
                0.0, float(agent_raw.get("curiosity_worker_min_seconds_between", 60.0)),
            ),
            curiosity_worker_max_user_word_count=max(
                1, int(agent_raw.get("curiosity_worker_max_user_word_count", 8)),
            ),
            gap_resolver_enabled=bool(
                agent_raw.get("gap_resolver_enabled", True),
            ),
            gap_resolver_interval_seconds=max(
                30,
                int(agent_raw.get("gap_resolver_interval_seconds", 600)),
            ),
            gap_resolver_threshold=max(
                0.0,
                min(
                    1.0,
                    float(agent_raw.get("gap_resolver_threshold", 0.55)),
                ),
            ),
            gap_resolver_per_tick=max(
                1, int(agent_raw.get("gap_resolver_per_tick", 5)),
            ),
            gap_user_answer_resolve_threshold=max(
                0.0,
                min(
                    1.0,
                    float(
                        agent_raw.get(
                            "gap_user_answer_resolve_threshold", 0.50,
                        )
                    ),
                ),
            ),
    )

