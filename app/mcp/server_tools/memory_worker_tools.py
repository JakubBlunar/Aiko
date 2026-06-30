from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController



def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def inspect_memory_tiers() -> str:
        """Return per-tier memory counts and a sample of the top rows
        in each tier.

        Quick health check for the memory-tier shuffler -- after a
        ``force_promotion_sweep`` you should see scratchpad shrink and
        long_term grow. Pinned rows always count under ``long_term``.
        """
        store = getattr(session, "_memory_store", None)
        if store is None:
            return json.dumps({"enabled": False})
        try:
            counts = store.count_by_tier()
        except Exception as exc:
            return f"count_by_tier failed: {exc}"
        samples: dict[str, list[dict[str, Any]]] = {}
        for tier in ("scratchpad", "long_term", "archive"):
            try:
                rows = store.iter_by_tier(tier)
            except Exception:
                rows = []
            rows.sort(
                key=lambda m: (
                    -float(m.salience),
                    -float(getattr(m, "revival_score", 0.0) or 0.0),
                ),
            )
            samples[tier] = [
                {
                    "id": int(m.id),
                    "kind": m.kind,
                    "salience": round(float(m.salience), 3),
                    "revival_score": round(float(getattr(m, "revival_score", 0.0) or 0.0), 3),
                    "use_count": int(m.use_count),
                    "pinned": bool(m.pinned),
                    "content": (m.content or "")[:160],
                }
                for m in rows[:5]
            ]
        payload = {
            "enabled": True,
            "counts": counts,
            "top_per_tier": samples,
        }
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool()
    def find_memories_by_content(
        query: str,
        *,
        kind: str = "",
        limit: int = 30,
    ) -> str:
        """Substring-search the memory store by content (case-insensitive).

        Diagnostic complement to ``inspect_memory_tiers`` — that tool
        only samples the top rows per tier, so it can't surface a
        specific topic. Use this when investigating "did Aiko store /
        retrieve / resolve memory X about topic Y?". Filter by ``kind``
        (e.g. ``knowledge_gap`` / ``open_question`` / ``preference``)
        to narrow further.

        Returns each match's id, kind, tier, salience, use_count,
        ``metadata.resolved_at`` (when relevant), and a 160-char
        content preview. Bounded by ``limit`` (default 30) so a
        common substring like "the" doesn't dump the whole store.
        """
        store = getattr(session, "_memory_store", None)
        if store is None:
            return json.dumps({"enabled": False})
        q = (query or "").strip().lower()
        if not q:
            return json.dumps({
                "error": "query is required (non-empty substring)",
            })
        kind_norm = (kind or "").strip().lower() or None
        try:
            mirror = getattr(store, "_mirror", None)
            rows = list(mirror.values()) if mirror is not None else []
        except Exception as exc:
            return f"mirror access failed: {exc}"
        hits: list[dict[str, Any]] = []
        for mem in rows:
            content = (mem.content or "")
            if q not in content.lower():
                continue
            if kind_norm is not None and mem.kind != kind_norm:
                continue
            meta = mem.metadata or {}
            row: dict[str, Any] = {
                "id": int(mem.id),
                "kind": mem.kind,
                "tier": mem.tier,
                "salience": round(float(mem.salience), 3),
                "use_count": int(mem.use_count),
                "pinned": bool(mem.pinned),
                "created_at": str(mem.created_at),
                "content": content[:160],
            }
            audit_keys = (
                "resolved_at",
                "resolved_by",
                "resolved_by_memory_id",
                "consumed_at",
                "topic",
            )
            audit = {k: meta[k] for k in audit_keys if k in meta}
            if audit:
                row["metadata"] = audit
            hits.append(row)
        hits.sort(key=lambda r: r["created_at"], reverse=True)
        payload = {
            "query": q,
            "kind_filter": kind_norm,
            "match_count": len(hits),
            "matches": hits[: max(1, int(limit))],
        }
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool()
    def get_style_signal() -> str:
        """K13: return the live stylometric mirror snapshot for the user.

        Surfaces what the :class:`StyleSignalAnalyzer` currently sees
        across recent user turns -- per-axis means (terseness,
        formality, emoji density, slang density, question rate),
        the bucketed labels that would render in the prompt, the
        rolling window size, and a "warmed" flag indicating whether
        cross-session warmup has run yet.

        Returns ``{"enabled": false}`` when the analyzer is disabled
        in settings or hasn't been instantiated. Returns a snapshot
        with ``signal=null`` (and ``rendered=""``) while the window
        is still in warmup.
        """
        analyzer = getattr(session, "_style_signal_analyzer", None)
        if analyzer is None:
            return json.dumps({"enabled": False})
        try:
            signal = analyzer.current_signal()
        except Exception as exc:
            return json.dumps({"error": f"current_signal raised: {exc}"})
        rendered = ""
        labels: list[str] = []
        signal_payload: Any = None
        if signal is not None:
            try:
                labels = analyzer.labels_for_signal(signal)
            except Exception:
                labels = []
            signal_payload = {
                "terseness": round(float(signal.terseness), 3),
                "formality": round(float(signal.formality), 3),
                "emoji_density": round(float(signal.emoji_density), 3),
                "slang_density": round(float(signal.slang_density), 3),
                "question_rate": round(float(signal.question_rate), 3),
                "window_size": int(signal.window_size),
            }
            try:
                from app.core.persona.style_signal import render_inner_life_block

                display_name = getattr(session, "user_display_name", "Jacob")
                rendered = render_inner_life_block(
                    signal,
                    labels,
                    user_display_name=display_name,
                )
            except Exception:
                rendered = ""
        payload = {
            "enabled": True,
            "warmed": bool(analyzer.is_warmed()),
            "window_size": int(analyzer.window_size()),
            "signal": signal_payload,
            "labels": labels,
            "rendered": rendered,
        }
        return json.dumps(payload, indent=2, default=str)

    @mcp.tool()
    def inspect_idle_workers() -> str:
        """Return per-worker run state from the IdleWorkerScheduler.

        Use this to confirm a worker actually ran (``last_run_at`` not
        ``None``), to see how long it's been since the last successful
        sweep (``run_count``), and to surface any swallowed exception
        (``last_error``).
        """
        sched = getattr(session, "_idle_scheduler", None)
        if sched is None:
            return json.dumps(
                {"enabled": False, "reason": "scheduler not running"},
                indent=2,
            )
        try:
            return json.dumps(
                {
                    "enabled": True,
                    "workers": sched.get_records(),
                },
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"inspect_idle_workers failed: {exc}"

    @mcp.tool()
    def get_idle_workers_status() -> str:
        """Return the enriched IdleWorkerScheduler view (P8).

        Adds ``next_due_at`` (when the worker is scheduled to fire
        next, given its interval), ``overdue_seconds`` (positive =
        already past due and waiting on a quiet window or budget;
        negative = not due yet), and per-worker timing stats
        (``avg_duration_ms`` EMA, ``last_duration_ms``,
        ``total_duration_ms``, ``error_count``). Workers are sorted
        most-overdue first so the lead is the worst-starved one.

        The header includes scheduler-level config
        (``wake_seconds``, ``tick_budget_ms``, ``max_per_tick``,
        ``quiet``) so a single tool call answers "is the scheduler
        dormant because it's not quiet, or because nothing is due, or
        because the budget is too small?".
        """
        sched = getattr(session, "_idle_scheduler", None)
        if sched is None:
            return json.dumps(
                {"enabled": False, "reason": "scheduler not running"},
                indent=2,
            )
        try:
            return json.dumps(
                {"enabled": True, **sched.get_status()},
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"get_idle_workers_status failed: {exc}"

    @mcp.tool()
    def force_promotion_sweep() -> str:
        """Run the MemoryPromotionWorker once, ignoring its interval gate.

        Returns the worker's result dict (``promoted``,
        ``deleted_scratchpad``, ``demoted_archive``, ``coerced_pinned``,
        ``pruned``). Useful when iterating on tier knobs -- skip the
        wait between scheduled sweeps.
        """
        sched = getattr(session, "_idle_scheduler", None)
        if sched is None:
            return "scheduler not running (memory.tiers_enabled may be off)"
        try:
            result = sched.force_run("memory_promotion")
        except KeyError:
            return "memory_promotion worker not registered"
        except Exception as exc:
            return f"force_promotion_sweep raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def get_engagement_state() -> str:
        """Inspect the K14 engagement tracker + K5 mood shell tilt.

        Returns the most recent ``EngagementResult`` (mode, label,
        closeness_delta, latency_seconds, length_z, latency_z, warmed,
        absence_seconds), the current voice latency window snapshot,
        the cached ``_last_engagement_label`` (consumed by the typed-
        proactive eligibility gate), any pending ``absence_seconds``
        slot (consumed by the next-turn absence-curiosity provider),
        and the live mood-shell tilt derived from the current
        ``AffectState`` + ``RelationshipAxesState`` (tilt name + line
        + contributors, or ``null`` when nothing notable crosses the
        gate).

        Useful when iterating on engagement thresholds, mood-shell
        rules, or chasing a "why didn't the absence-curiosity cue
        fire?" report. JSON output; safe to call any time.
        """
        out: dict[str, Any] = {
            "engagement_enabled": bool(
                getattr(
                    session._settings.agent,
                    "engagement_tracker_enabled",
                    True,
                )
            ),
            "mood_shell_enabled": bool(
                getattr(
                    session._settings.agent, "mood_shell_enabled", True,
                )
            ),
            "last_turn_mode": getattr(session, "_last_turn_mode", None),
            "last_engagement_label": getattr(
                session, "_last_engagement_label", None,
            ),
            "pending_absence_seconds": getattr(
                session, "_pending_absence_seconds", None,
            ),
        }
        tracker = getattr(session, "_engagement_tracker", None)
        if tracker is not None:
            try:
                result = tracker.last_result
                if result is not None:
                    out["last_result"] = {
                        "mode": result.mode,
                        "label": result.label,
                        "closeness_delta": result.closeness_delta,
                        "latency_seconds": result.latency_seconds,
                        "latency_z": result.latency_z,
                        "length_z": result.length_z,
                        "absence_seconds": result.absence_seconds,
                        "warmed": result.warmed,
                    }
                out["latency_window"] = tracker.latency_window_snapshot()
            except Exception as exc:  # pragma: no cover -- diag tool
                out["tracker_error"] = str(exc)
        else:
            out["tracker_error"] = "engagement tracker not constructed"
        try:
            from app.core.affect.mood_shell import (
                derive_mood_shell,
                render_mood_shell_block,
            )

            affect = None
            try:
                affect = session._affect_store.get(session._user_id)
            except Exception:
                affect = None
            axes = None
            store = getattr(session, "_relationship_axes_store", None)
            if store is not None:
                try:
                    axes = store.get(session._user_id)
                except Exception:
                    axes = None
            threshold = float(
                getattr(
                    session._settings.agent,
                    "mood_shell_axis_threshold",
                    0.5,
                )
            )
            shell = derive_mood_shell(
                affect=affect,
                axes=axes,
                axis_notable_threshold=threshold,
            )
            if shell is None:
                out["mood_shell"] = None
            else:
                out["mood_shell"] = {
                    "tilt": shell.tilt,
                    "line": shell.line,
                    "contributors": list(shell.contributors),
                    "rendered": render_mood_shell_block(shell),
                }
        except Exception as exc:  # pragma: no cover -- diag tool
            out["mood_shell_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_decay_sweep() -> str:
        """Run the MemoryDecayWorker once, ignoring its interval gate.

        Returns the decay stats (``elapsed_days``, ``applied``). On
        the very first call after boot, ``elapsed_days`` is 0 because
        the worker just installs the wall-clock anchor; the next call
        applies real decay.
        """
        sched = getattr(session, "_idle_scheduler", None)
        if sched is None:
            return "scheduler not running (memory.tiers_enabled may be off)"
        try:
            result = sched.force_run("memory_decay")
        except KeyError:
            return "memory_decay worker not registered"
        except Exception as exc:
            return f"force_decay_sweep raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def force_knowledge_enrichment() -> str:
        """F9 — run the IdleKnowledgeWorker once, ignoring its gates.

        Picks the densest under-researched topic-graph cluster,
        web-searches it, and distils ``knowledge`` facts. Returns the
        run result (``cluster``, ``topic``, ``wrote``, ``deduped``,
        ``outcome``) or a skip reason (``no_cluster``, ``rate_limited``,
        ``privacy_gate``, …). Bypasses the interval gate but still
        honours the rate limiter inside ``run()``.
        """
        sched = getattr(session, "_idle_scheduler", None)
        if sched is None:
            return "scheduler not running (memory.tiers_enabled may be off)"
        try:
            result = sched.force_run("idle_knowledge")
        except KeyError:
            return (
                "idle_knowledge worker not registered "
                "(agent.knowledge_enrichment_enabled may be off, or no "
                "embedder / web-search tool)"
            )
        except Exception as exc:
            return f"force_knowledge_enrichment raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def get_knowledge_worker_state() -> str:
        """F9 — dump the IdleKnowledgeWorker state as JSON.

        Shows the master switch, cadence, the live rate-limiter
        snapshot (hour/day used vs cap), the per-cluster cooldown map
        from ``kv_meta``, and a dry-run of the cluster picker (which
        interest cluster would be researched on the next tick, or
        ``None`` if everything is on cooldown / already researched).
        First stop for "why isn't Aiko learning anything new?".
        """
        from datetime import datetime, timezone

        worker = getattr(session, "_idle_knowledge", None)
        out: dict = {
            "registered": worker is not None,
            "enabled": bool(
                getattr(
                    session._settings.agent,
                    "knowledge_enrichment_enabled",
                    True,
                )
            ),
            "topic_extraction_enabled": bool(
                getattr(
                    session._settings.agent,
                    "knowledge_topic_extraction_enabled",
                    True,
                )
            ),
        }
        if worker is None:
            return json.dumps(out, indent=2, default=str)
        now = datetime.now(timezone.utc)
        try:
            out["interval_seconds"] = worker.interval_seconds
        except Exception as exc:  # pragma: no cover -- diag tool
            out["interval_error"] = str(exc)
        try:
            out["rate_limit"] = worker._rate_limiter.snapshot(now)
        except Exception as exc:  # pragma: no cover -- diag tool
            out["rate_limit_error"] = str(exc)
        try:
            out["cluster_cooldowns"] = worker._load_cooldowns()
        except Exception as exc:  # pragma: no cover -- diag tool
            out["cooldowns_error"] = str(exc)
        try:
            out["research_queue"] = worker._load_queue()
        except Exception as exc:  # pragma: no cover -- diag tool
            out["research_queue_error"] = str(exc)
        try:
            ranked = worker._score_candidates(now=now)
            out["candidates"] = [
                {
                    "cluster_key": p.cluster_key,
                    "topic": p.topic[:120],
                    "size": p.size,
                }
                for p in ranked[:5]
            ]
            pick = ranked[0] if ranked else None
            out["next_pick"] = (
                None
                if pick is None
                else {
                    "cluster_key": pick.cluster_key,
                    "topic": pick.topic[:160],
                    "size": pick.size,
                }
            )
        except Exception as exc:  # pragma: no cover -- diag tool
            out["next_pick_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def get_calibration_state() -> str:
        """K20 — dump the per-user CalibrationState as JSON.

        Returns the current ``global_score``, ``last_updated_at``,
        and per-topic-slot detail (score, last_signal_at,
        signal_count -- the centroid array is summarised to a
        ``dim``/``norm`` pair rather than dumped to keep the response
        readable). Reads the same lazy decay path the inner-life
        provider uses so the snapshot reflects the live state Aiko
        would see on her next turn.
        """
        store = getattr(session, "_calibration_store", None)
        if store is None:
            return json.dumps(
                {"error": "CalibrationStore not initialised"},
            )
        try:
            from app.core.affect import calibration_detector
            from datetime import datetime, timezone
            import numpy as np

            state = store.get(session._user_id)
            state = calibration_detector.decay(
                state,
                now=datetime.now(timezone.utc),
                half_life_days=float(
                    getattr(
                        session._memory_settings,
                        "calibration_half_life_days",
                        5.0,
                    )
                ),
                baseline=float(
                    getattr(
                        session._memory_settings,
                        "calibration_baseline",
                        0.80,
                    )
                ),
            )
            payload = {
                "user_id": session._user_id,
                "global_score": round(state.global_score, 4),
                "last_updated_at": (
                    state.last_updated_at.isoformat()
                    if state.last_updated_at is not None
                    else None
                ),
                "baseline": float(
                    getattr(
                        session._memory_settings,
                        "calibration_baseline",
                        0.80,
                    )
                ),
                "topics": [
                    {
                        "score": round(slot.score, 4),
                        "last_signal_at": slot.last_signal_at.isoformat(),
                        "signal_count": int(slot.signal_count),
                        "centroid_dim": int(slot.centroid.size),
                        "centroid_norm": round(
                            float(np.linalg.norm(slot.centroid)), 4,
                        ),
                    }
                    for slot in state.topics
                ],
            }
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"get_calibration_state raised: {exc}"

    @mcp.tool()
    def reset_calibration() -> str:
        """K20 — wipe the per-user CalibrationState row.

        After this call ``get_calibration_state`` returns a fresh
        baseline state (global_score = ``calibration_baseline``,
        no topics). Useful when end-to-end-testing the post-turn
        wire-in: hand-inject a state via the SQLite REPL or via
        upsert, observe Aiko's hedging behaviour, then reset.
        """
        store = getattr(session, "_calibration_store", None)
        if store is None:
            return json.dumps(
                {"error": "CalibrationStore not initialised"},
            )
        try:
            store.reset(session._user_id)
            return json.dumps(
                {"reset": True, "user_id": session._user_id},
            )
        except Exception as exc:
            return f"reset_calibration raised: {exc}"

    @mcp.tool()
    def get_sensory_anchor_state() -> str:
        """K24 — dump the in-memory :class:`SensoryAnchorCadence` snapshot.

        Returns a JSON dict with ``cooldown_remaining``,
        ``recent_slugs``, ``last_arc_seen``, ``last_fired_slug``,
        ``last_fired_verb_class``, ``fire_count``, ``tick_count``,
        plus a ``rendered_preview`` from a forced beat (if eligible)
        so you can see what cue would surface *right now* without
        burning the cooldown. Use ``force_sensory_anchor`` to
        actually fire one for end-to-end testing.
        """
        cadence = getattr(session, "_sensory_anchor_cadence", None)
        if cadence is None:
            return json.dumps(
                {"error": "SensoryAnchorCadence not initialised"},
            )
        try:
            snapshot = cadence.to_debug_dict()
            # Preview: read the current room without arming the
            # cooldown. Same gates the real provider uses, just no
            # state mutation.
            world_store = getattr(session, "_world_store", None)
            preview: str | None = None
            posture: str | None = None
            arc: str | None = None
            item_count = 0
            if world_store is not None:
                try:
                    state = world_store.get_state()
                    posture = (state.posture or "").strip().lower()
                    items = world_store.list_items(
                        location_id=state.location_id,
                    )
                    item_count = len(items)
                    arc_store = getattr(session, "_arc_store", None)
                    if arc_store is not None:
                        try:
                            arc_state = arc_store.get_or_default(
                                session._user_id,
                            )
                            arc = arc_state.arc
                        except Exception:
                            arc = None
                    from app.core.conversation import sensory_anchor as sa

                    beat = sa.pick_beat(
                        posture=posture or "sitting",
                        items=items,
                        arc=arc or "casual_check_in",
                        recent_slugs=tuple(snapshot["recent_slugs"]),
                    )
                    preview = sa.render_inner_life_block(
                        beat,
                        user_display_name=session.user_display_name,
                    ) or None
                except Exception:
                    preview = None
            return json.dumps(
                {
                    **snapshot,
                    "current_posture": posture,
                    "current_arc": arc,
                    "current_item_count": item_count,
                    "rendered_preview": preview,
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_sensory_anchor_state raised: {exc}"

    @mcp.tool()
    def force_sensory_anchor() -> str:
        """K24 — bypass cooldown + dice gate and emit one beat.

        Useful for testing the persona block end-to-end without
        waiting on the arc-weighted probability roll. Pushes the
        slug into the no-repeat ring and arms the cooldown as if
        the beat had fired naturally, so subsequent normal ticks
        behave as expected. Returns the rendered cue or an error
        message.
        """
        cadence = getattr(session, "_sensory_anchor_cadence", None)
        if cadence is None:
            return json.dumps(
                {"error": "SensoryAnchorCadence not initialised"},
            )
        world_store = getattr(session, "_world_store", None)
        if world_store is None:
            return json.dumps(
                {"error": "WorldStore not initialised"},
            )
        try:
            state = world_store.get_state()
            posture = (state.posture or "").strip().lower()
            if not posture:
                return json.dumps({"error": "no posture set"})
            items = world_store.list_items(location_id=state.location_id)
            arc_store = getattr(session, "_arc_store", None)
            arc = "casual_check_in"
            if arc_store is not None:
                try:
                    arc = arc_store.get_or_default(session._user_id).arc
                except Exception:
                    pass
            from app.core.conversation import sensory_anchor as sa

            beat = sa.pick_beat(
                posture=posture,
                items=items,
                arc=arc,
                recent_slugs=tuple(cadence.to_debug_dict()["recent_slugs"]),
            )
            if beat is None:
                return json.dumps(
                    {
                        "error": "no eligible beat (empty pool, "
                                 "all items in ring, or posture-kind "
                                 "matrix empty)",
                    },
                )
            # Mirror the side effects of a normal fire.
            cooldown = max(
                int(sa._ARC_WEIGHTS.get(arc, sa._DEFAULT_ARC_WEIGHT)[1]),
                int(
                    getattr(
                        session._settings.memory,
                        "sensory_anchor_min_turn_gap",
                        4,
                    )
                ),
            )
            cadence._cooldown_remaining = cooldown
            cadence._recent_slugs.append(beat.item_slug)
            cadence._last_fired_slug = beat.item_slug
            cadence._last_fired_verb_class = beat.verb_class
            cadence._fire_count += 1
            rendered = sa.render_inner_life_block(
                beat, user_display_name=session.user_display_name,
            )
            return json.dumps(
                {
                    "fired": True,
                    "item_slug": beat.item_slug,
                    "verb_class": beat.verb_class,
                    "arc": arc,
                    "posture": posture,
                    "cooldown_armed": cooldown,
                    "rendered": rendered,
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_sensory_anchor raised: {exc}"

    @mcp.tool()
    def get_misattunement_state() -> str:
        """K23 — dump the in-memory misattunement detector state.

        Returns a JSON dict with the master switch, current
        cooldown counter, the last-fire diagnostic fields, and the
        settings snapshot (so you can see what thresholds are
        actually in force after the user.json overrides land). The
        cooldown counter decrements by one each turn regardless of
        trigger state -- a value of 0 means the next eligible turn
        will fire. Use ``force_misattunement`` to bypass the
        cooldown for the next turn end-to-end.
        """
        try:
            agent = session._settings.agent
            cooldown = int(
                getattr(session, "_misattunement_cooldown", 0) or 0,
            )
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "misattunement_detection_enabled", True),
                    ),
                    "cooldown_remaining": cooldown,
                    "force_next": bool(
                        getattr(session, "_misattunement_force_next", False),
                    ),
                    "last_trigger": getattr(
                        session, "_last_misattunement_trigger", None,
                    ),
                    "last_fire_turn": getattr(
                        session, "_last_misattunement_fire_turn", None,
                    ),
                    "settings": {
                        "shrink_min_prev_words": int(
                            getattr(
                                agent,
                                "misattunement_shrink_min_prev_words",
                                30,
                            )
                        ),
                        "shrink_max_user_words": int(
                            getattr(
                                agent,
                                "misattunement_shrink_max_user_words",
                                8,
                            )
                        ),
                        "pivot_max_user_words": int(
                            getattr(
                                agent,
                                "misattunement_pivot_max_user_words",
                                8,
                            )
                        ),
                        "cooldown_turns": int(
                            getattr(
                                agent,
                                "misattunement_cooldown_turns",
                                3,
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_misattunement_state raised: {exc}"

    @mcp.tool()
    def force_misattunement() -> str:
        """K23 — arm a one-shot bypass on the misattunement cooldown.

        Sets ``_misattunement_force_next`` so the next call to the
        provider treats ``cooldown_remaining`` as 0 regardless of
        the actual counter. The bypass is consumed whether the
        trigger paths fire or not (so a one-shot is strictly
        one-turn). If the next user message doesn't satisfy either
        the shrink or the pivot trigger, the bypass simply expires
        with no cue and the normal cooldown resumes its countdown.

        For an end-to-end repro: call this tool, then send Aiko a
        short message ("ok" or "yeah") right after a long
        Aiko reply. The next turn's prompt should include the
        "Heads-up: {user} just gave a short reply..." block.
        """
        try:
            session._misattunement_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will ignore the cooldown; "
                        "send a short user message to land the cue"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_misattunement raised: {exc}"

    @mcp.tool()
    def get_mood_inertia_state() -> str:
        """K45 — dump the live mood-inertia detector state.

        Returns a JSON dict with the master switch (cue side), the
        avatar-side damping flag, the mismatch threshold + cooldown
        knobs, the recent reaction ring (whiplash input), the current
        cooldown remainder, whether a one-shot cue is pending, the
        force flag, and the last assessment (mismatch / band /
        whiplash / pre-impulse scalars) regardless of whether it
        armed.
        """
        try:
            last = getattr(session, "_mood_inertia_last", None)
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(
                            session._settings.agent,
                            "mood_inertia_enabled",
                            True,
                        )
                    ),
                    "avatar_damping_enabled": bool(
                        getattr(
                            session._settings.avatar,
                            "mood_inertia_damping",
                            True,
                        )
                    ),
                    "mismatch_threshold": float(
                        getattr(
                            session._settings.memory,
                            "mood_inertia_mismatch_threshold",
                            0.45,
                        )
                    ),
                    "cooldown_turns": int(
                        getattr(
                            session._settings.memory,
                            "mood_inertia_cooldown_turns",
                            3,
                        )
                    ),
                    "cooldown_remaining": int(
                        getattr(
                            session, "_mood_inertia_cooldown_remaining", 0,
                        )
                    ),
                    "recent_reactions": list(
                        getattr(session, "_mood_inertia_reactions", []) or []
                    ),
                    "pending_cue": getattr(
                        session, "_pending_mood_inertia", None,
                    ),
                    "force_next": bool(
                        getattr(session, "_mood_inertia_force", False)
                    ),
                    "last_assessment": dict(last) if last else None,
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_mood_inertia_state raised: {exc}"

    @mcp.tool()
    def force_mood_inertia() -> str:
        """K45 — arm a one-shot forced mood-inertia cue.

        Sets ``_mood_inertia_force`` so the next provider call renders
        a synthetic strong-band cue built from the live affect state
        and the most recent reaction tag, bypassing the mismatch
        threshold and the cooldown. The flag is consumed on the next
        provider call whether or not a real turn follows.

        End-to-end repro: call this tool, then ``send_message``
        (skip_tts=true) and verify the "your face just jumped to ..."
        line lands via ``get_last_response_detail``'s system_prompt.
        """
        try:
            session._mood_inertia_force = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call renders a synthetic strong-band "
                        "cue from the live affect state"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_mood_inertia raised: {exc}"

    @mcp.tool()
    def get_opinion_injection_state() -> str:
        """K29 — dump the in-memory opinion-injection detector state.

        Returns a JSON dict with the master switch, current cooldown,
        per-session counter (vs cap), force-next flag, the most
        recent fire (full diagnostics: trigger / cosine / heuristic /
        signals / matched stance text), the LLM rate-limiter budget,
        and a settings snapshot so you can see what thresholds are
        actually in force after the user.json overrides land.

        The cooldown counter decrements by one each turn regardless
        of trigger state -- a value of 0 means the next eligible
        turn can fire. ``session_count`` resets on session boundary
        (``switch_session`` / ``clear_conversation_memory``) and
        caps fires within the current conversation. Use
        ``force_opinion_injection`` to bypass cooldown + cap on the
        next turn for end-to-end repro.
        """
        try:
            agent = session._settings.agent
            memory = session._memory_settings
            cooldown = int(
                getattr(session, "_opinion_injection_cooldown", 0) or 0,
            )
            session_count = int(
                getattr(session, "_opinion_injection_session_count", 0) or 0,
            )
            last = getattr(session, "_last_opinion_injection", None)
            last_payload = None
            if last is not None:
                last_payload = {
                    "trigger": getattr(last, "trigger", None),
                    "cosine": float(getattr(last, "cosine", 0.0)),
                    "stance_memory_id": int(
                        getattr(last, "stance_memory_id", -1)
                    ),
                    "stance_text": (
                        (getattr(last, "stance_text", "") or "")[:200]
                    ),
                    "heuristic_label": getattr(last, "heuristic_label", None),
                    "heuristic_signals": list(
                        getattr(last, "heuristic_signals", []) or []
                    ),
                    "llm_verdict": getattr(last, "llm_verdict", None),
                }
            rate_limiter = getattr(
                session, "_opinion_injection_rate_limiter", None
            )
            llm_budget = None
            if rate_limiter is not None:
                try:
                    llm_budget = rate_limiter.snapshot()
                except Exception:
                    llm_budget = None
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "opinion_injection_enabled", True),
                    ),
                    "require_definite": bool(
                        getattr(
                            agent,
                            "opinion_injection_require_definite",
                            False,
                        ),
                    ),
                    "cooldown_remaining": cooldown,
                    "session_count": session_count,
                    "session_cap": int(
                        getattr(
                            memory, "opinion_injection_per_session_cap", 3,
                        )
                    ),
                    "force_next": bool(
                        getattr(
                            session, "_opinion_injection_force_next", False,
                        ),
                    ),
                    "last_fire": last_payload,
                    "llm_budget": llm_budget,
                    "settings": {
                        "min_cosine": float(
                            getattr(
                                memory,
                                "opinion_injection_min_cosine",
                                0.55,
                            )
                        ),
                        "min_user_words": int(
                            getattr(
                                memory,
                                "opinion_injection_min_user_words",
                                4,
                            )
                        ),
                        "cooldown_turns": int(
                            getattr(
                                memory,
                                "opinion_injection_cooldown_turns",
                                5,
                            )
                        ),
                        "per_hour_cap": int(
                            getattr(
                                memory,
                                "opinion_injection_per_hour_cap",
                                6,
                            )
                        ),
                        "per_day_cap": int(
                            getattr(
                                memory,
                                "opinion_injection_per_day_cap",
                                30,
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_opinion_injection_state raised: {exc}"

    @mcp.tool()
    def force_opinion_injection() -> str:
        """K29 — arm a one-shot bypass on the opinion-injection cooldown + cap.

        Sets ``_opinion_injection_force_next`` so the next call to
        the provider ignores BOTH the cooldown counter AND the
        per-session cap. The predicate filter, cosine threshold,
        and heuristic gate still run -- you can't force-fire a cue
        when there's no contradicting stance memory or the user
        message doesn't touch one.

        Repro recipe for the smoking scenario:

        1. Make sure Aiko has a ``kind="self"`` stance memory that
           reads roughly like "I really don't like smoke -- it
           gives me a headache." (manual REST insert or a self-
           tag during a previous chat).
        2. Call this tool.
        3. Send Aiko: "I like smoking, it helps me think."
        4. Check ``tail_logs(module_contains="opinion")`` for
           ``opinion-injection fire: trigger=contradiction_definite ...``.
        5. Verify Aiko's reply owns her stance ("smoke and I don't
           really get along") rather than lecturing about health.
        """
        try:
            session._opinion_injection_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will ignore the cooldown "
                        "AND the per-session cap; predicate filter + "
                        "cosine + heuristic gates still apply"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_opinion_injection raised: {exc}"

    @mcp.tool()
    def get_stance_persistence_state() -> str:
        """K46 — dump the "don't cave on taste pushback" state.

        Shows the master switch, the configured warm-stance window, the
        live ``_stance_recent_window`` countdown + stashed stance snippet
        (armed post-turn whenever a K29 cue fires, decremented per turn),
        the one-shot force flag, and the last fire diagnostic. First stop
        for "why didn't Aiko hold her take when I said 'really?'".
        """
        out: dict[str, Any] = {
            "enabled": bool(
                getattr(
                    session._settings.agent,
                    "stance_persistence_enabled",
                    True,
                )
            ),
            "window_setting": int(
                getattr(
                    session._memory_settings,
                    "stance_persistence_window",
                    3,
                )
            ),
            "recent_window": int(
                getattr(session, "_stance_recent_window", 0) or 0
            ),
            "recent_text": str(
                getattr(session, "_stance_recent_text", "") or ""
            ),
            "force_next": bool(
                getattr(session, "_stance_persistence_force_next", False)
            ),
            "last_fire": getattr(session, "_last_stance_persistence", None),
        }
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_stance_persistence() -> str:
        """K46 — arm a one-shot bypass on the warm-stance window.

        Sets ``_stance_persistence_force_next`` so the next provider call
        fires the "hold your take" cue even without a recent K29 stance.
        The mild-pushback band gate still applies: send a *mild* push
        ("really?", "are you sure?") so the calibration regex classifies
        ``pushback_mild``; a strong correction or a plain statement won't
        fire. Repro: call this, then send Aiko "really? you don't like
        that?" and check ``tail_logs(module_contains="stance")`` for
        ``stance-persistence fire:``.
        """
        try:
            session._stance_persistence_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores the recent-stance "
                        "window; a mild-pushback band is still required"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_stance_persistence raised: {exc}"

    @mcp.tool()
    def get_long_arc_callback_state() -> str:
        """K63 — dump the long-arc callback ("weeks ago you said…") state.

        Shows the master switch, the age / cosine / cooldown / cap / min-
        words knobs, the live per-session count, the wall-clock cooldown
        kv stamp + don't-repeat ring, the one-shot force flag, the last
        fire diagnostic, and (when ``user_text`` is supplied) a dry-run of
        the aged retrieval lane so you can see which old memories *would*
        qualify for the current turn. First stop for "why didn't Aiko reach
        back to that thing from months ago?".
        """
        from datetime import datetime, timezone

        mem = session._memory_settings
        out: dict[str, Any] = {
            "enabled": bool(
                getattr(session._settings.agent, "long_arc_callback_enabled", True)
            ),
            "settings": {
                "min_age_days": int(
                    getattr(mem, "long_arc_callback_min_age_days", 21)
                ),
                "min_cosine": float(
                    getattr(mem, "long_arc_callback_min_cosine", 0.55)
                ),
                "cooldown_hours": float(
                    getattr(mem, "long_arc_callback_cooldown_hours", 6.0)
                ),
                "per_session_cap": int(
                    getattr(mem, "long_arc_callback_per_session_cap", 1)
                ),
                "min_user_words": int(
                    getattr(mem, "long_arc_callback_min_user_words", 5)
                ),
            },
            "session_count": int(
                getattr(session, "_long_arc_callback_session_count", 0) or 0
            ),
            "force_next": bool(
                getattr(session, "_long_arc_callback_force_next", False)
            ),
            "last_fire": getattr(session, "_last_long_arc_callback", None),
        }
        try:
            from app.core.conversation import long_arc_callback as _lac

            now = datetime.now(timezone.utc)
            out["last_fired_at"] = session._chat_db.kv_get(_lac.KV_LAST_FIRED_AT)
            out["recent_ids"] = _lac.load_recent_ids(session._chat_db.kv_get)
            out["cooldown_elapsed"] = _lac.cooldown_elapsed(
                session._chat_db.kv_get,
                now=now,
                cooldown_hours=float(
                    getattr(mem, "long_arc_callback_cooldown_hours", 6.0)
                ),
            )
        except Exception as exc:
            out["kv_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_long_arc_callback() -> str:
        """K63 — arm a one-shot bypass on the cap + cooldown + min-words.

        Sets ``_long_arc_callback_force_next`` so the next provider call
        skips the per-session cap, the wall-clock cooldown, and the min-
        words gate. The age / cosine / kind gates still apply: the turn
        must mention something that actually cosine-matches an old (>=
        ``min_age_days``) memory, or the bypass silently expires. Repro:
        seed an old memory, call this, then send a message on that topic
        and check ``tail_logs(module_contains="long_arc")`` for
        ``long-arc-callback fire:``.
        """
        try:
            session._long_arc_callback_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores the cap/cooldown/min-words "
                        "gates; an old topically-matching memory is still "
                        "required to fire"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_long_arc_callback raised: {exc}"

    @mcp.tool()
    def get_knowledge_gap_notice_state() -> str:
        """F10f — dump the KnowledgeGapNoticeWorker + provider state.

        Shows the master switch, cadence/threshold knobs, the kv journal
        ring of drafted notices (``aiko.knowledge_gap_notices``), the
        per-topic cooldown map, the surfaced-keys set the provider tracks,
        the one-shot provider force flag, and a dry-run of the topic-graph
        gap picker (which dense, low-knowledge clusters would be drafted
        on the next tick). First stop for "why didn't Aiko admit she
        doesn't know about X?".
        """
        worker = getattr(session, "_knowledge_gap_notice_worker", None)
        out: dict[str, Any] = {
            "registered": worker is not None,
            "enabled": bool(
                getattr(
                    session._settings.agent,
                    "knowledge_gap_notice_enabled",
                    True,
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_knowledge_gap_notice_force_next", False)
            ),
        }
        chat_db = getattr(session, "_chat_db", None)
        if chat_db is not None and hasattr(chat_db, "kv_get"):
            try:
                from app.core.proactive.knowledge_gap_notice_worker import (
                    load_notices,
                )

                out["journal"] = load_notices(chat_db.kv_get)
            except Exception as exc:  # pragma: no cover -- diag tool
                out["journal_error"] = str(exc)
            try:
                raw = chat_db.kv_get("knowledge_gap_notice.surfaced_keys")
                out["surfaced_keys"] = json.loads(raw) if raw else []
            except Exception:
                out["surfaced_keys"] = []
        if worker is not None:
            try:
                out["interval_seconds"] = worker.interval_seconds
                out["topic_cooldowns"] = worker._load_cooldowns()
            except Exception as exc:  # pragma: no cover -- diag tool
                out["worker_error"] = str(exc)
            graph = getattr(session, "_topic_graph", None)
            if graph is not None:
                try:
                    cands = graph.knowledge_gap_clusters(
                        min_size=worker._min_size,
                        max_knowledge_fraction=worker._max_knowledge_fraction,
                        top_n=5,
                    )
                    out["candidates"] = [
                        {
                            "label": c.label[:120],
                            "size": c.size,
                            "knowledge_count": c.knowledge_count,
                            "knowledge_fraction": round(
                                c.knowledge_fraction, 3
                            ),
                        }
                        for c in cands
                    ]
                except Exception as exc:  # pragma: no cover -- diag tool
                    out["candidates_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_knowledge_gap_notice() -> str:
        """F10f — run the KnowledgeGapNoticeWorker once, bypassing cooldown.

        Drafts a notice for the strongest knowledge-gap cluster even if
        it's on its per-topic cooldown (the topic graph must still have a
        qualifying dense, low-knowledge cluster). Returns the run result
        (``drafted``, ``topic``, ``size``, …) or a skip reason. Pair with
        ``force_knowledge_gap_notice_surface`` + ``send_message`` to land
        the cue end-to-end.
        """
        worker = getattr(session, "_knowledge_gap_notice_worker", None)
        if worker is None:
            return (
                "knowledge_gap_notice worker not registered "
                "(agent.knowledge_gap_notice_enabled may be off, or no "
                "memory store / topic graph)"
            )
        try:
            worker.force_next()
            result = worker.run()
        except Exception as exc:
            return f"force_knowledge_gap_notice raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def force_knowledge_gap_notice_surface() -> str:
        """F10f — arm a one-shot bypass on the provider's gates.

        Sets ``_knowledge_gap_notice_force_next`` so the next provider
        call surfaces the newest journal entry regardless of topic
        relevance or the surfaced-keys set (the ring must still be
        non-empty — draft one first via ``force_knowledge_gap_notice``).
        Then ``send_message`` and verify the "X keeps coming up but you've
        never dug in" line lands in ``get_last_response_detail``'s
        system_prompt.
        """
        try:
            session._knowledge_gap_notice_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call surfaces the newest notice, "
                        "ignoring topic-relevance + surfaced gates"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_knowledge_gap_notice_surface raised: {exc}"

    @mcp.tool()
    def get_associative_wander_state() -> str:
        """K64a — dump the associative-wandering worker + provider state.

        Shows the master switch, the worker registration, the kv journal
        ring of drafted connections (``aiko.associative_wanders``), the
        per-pair cooldown map, the surfaced-keys set the provider tracks,
        the one-shot provider force flag, and a dry-run of the distant-pair
        picker (which two topic clusters would be connected on the next
        tick). First stop for "why didn't Aiko make that connection?".
        """
        worker = getattr(session, "_associative_wander_worker", None)
        out: dict[str, Any] = {
            "registered": worker is not None,
            "enabled": bool(
                getattr(
                    session._settings.agent,
                    "associative_wander_enabled",
                    True,
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_associative_wander_force_next", False)
            ),
        }
        chat_db = getattr(session, "_chat_db", None)
        if chat_db is not None and hasattr(chat_db, "kv_get"):
            try:
                from app.core.proactive.associative_wander_worker import (
                    load_wanders,
                )

                out["journal"] = load_wanders(chat_db.kv_get)
            except Exception as exc:  # pragma: no cover -- diag tool
                out["journal_error"] = str(exc)
            try:
                raw = chat_db.kv_get("associative_wander.surfaced_keys")
                out["surfaced_keys"] = json.loads(raw) if raw else []
            except Exception:
                out["surfaced_keys"] = []
        if worker is not None:
            try:
                out["interval_seconds"] = worker.interval_seconds
                out["pair_cooldowns"] = worker._load_cooldowns()
            except Exception as exc:  # pragma: no cover -- diag tool
                out["worker_error"] = str(exc)
            graph = getattr(session, "_topic_graph", None)
            if graph is not None:
                try:
                    from app.core.proactive.associative_wander_worker import (
                        find_distant_pairs,
                    )

                    pairs = find_distant_pairs(
                        graph.topic_clusters(),
                        max_cosine=worker._max_pair_cosine,
                        min_size=worker._min_size,
                    )
                    out["candidate_pairs"] = [
                        {
                            "topic_a": p.label_a[:80],
                            "topic_b": p.label_b[:80],
                            "cosine": round(p.cosine, 3),
                            "key": p.key,
                        }
                        for p in pairs[:5]
                    ]
                except Exception as exc:  # pragma: no cover -- diag tool
                    out["candidate_pairs_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_associative_wander() -> str:
        """K64a — run the AssociativeWanderWorker once, bypassing cooldowns.

        Picks the single most-distant qualifying pair (ignoring the global
        cooldown, daily cap, and per-pair cooldown), asks the worker LLM for
        a connection, and drafts it into the ring (the topic graph must
        still have two qualifying distant clusters, and the LLM must judge
        them genuinely connectable). Returns the run result (``drafted``,
        ``topic_a``, ``topic_b``, ``connection`` …) or a skip reason. Pair
        with ``force_associative_wander_surface`` + ``send_message`` to land
        the cue end-to-end.
        """
        worker = getattr(session, "_associative_wander_worker", None)
        if worker is None:
            return (
                "associative_wander worker not registered "
                "(agent.associative_wander_enabled may be off, or no memory "
                "store / topic graph)"
            )
        try:
            worker.force_next()
            result = worker.run()
        except Exception as exc:
            return f"force_associative_wander raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def force_associative_wander_surface() -> str:
        """K64a — arm a one-shot bypass on the provider's gates.

        Sets ``_associative_wander_force_next`` so the next provider call
        surfaces the newest journal entry regardless of topic relevance or
        the surfaced-keys set (the ring must still be non-empty — draft one
        first via ``force_associative_wander``). Then ``send_message`` and
        verify the "you noticed a connection between X and Y" line lands in
        ``get_last_response_detail``'s system_prompt.
        """
        try:
            session._associative_wander_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call surfaces the newest connection, "
                        "ignoring topic-relevance + surfaced gates"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_associative_wander_surface raised: {exc}"

    @mcp.tool()
    def get_interest_drift_state() -> str:
        """K64b — dump the interest-drift worker + provider state.

        Shows the master switch, the worker registration, the kv journal
        ring of drafted drifts (``aiko.interest_drifts``), the per-topic
        mass time-series (``aiko.interest_mass`` — label + size history),
        the per-topic cooldown map, the surfaced-keys set the provider
        tracks, and the one-shot provider force flag. First stop for "why
        didn't Aiko notice she's been into X / lost interest in Y?".
        """
        worker = getattr(session, "_interest_drift_worker", None)
        out: dict[str, Any] = {
            "registered": worker is not None,
            "enabled": bool(
                getattr(
                    session._settings.agent, "interest_drift_enabled", True,
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_interest_drift_force_next", False)
            ),
        }
        chat_db = getattr(session, "_chat_db", None)
        if chat_db is not None and hasattr(chat_db, "kv_get"):
            try:
                from app.core.proactive.interest_drift_worker import (
                    load_drifts,
                )

                out["journal"] = load_drifts(chat_db.kv_get)
            except Exception as exc:  # pragma: no cover -- diag tool
                out["journal_error"] = str(exc)
            try:
                raw = chat_db.kv_get("aiko.interest_mass")
                out["mass_series"] = json.loads(raw) if raw else {}
            except Exception:
                out["mass_series"] = {}
            try:
                raw = chat_db.kv_get("interest_drift.surfaced_keys")
                out["surfaced_keys"] = json.loads(raw) if raw else []
            except Exception:
                out["surfaced_keys"] = []
            try:
                raw = chat_db.kv_get("interest_drift.topic_cooldowns")
                out["topic_cooldowns"] = json.loads(raw) if raw else {}
            except Exception:
                out["topic_cooldowns"] = {}
        if worker is not None:
            try:
                out["interval_seconds"] = worker.interval_seconds
            except Exception as exc:  # pragma: no cover -- diag tool
                out["worker_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_interest_drift() -> str:
        """K64b — run the InterestDriftWorker once, bypassing the caps.

        Snapshots current cluster mass, then drafts the strongest drift
        candidate even if it's on its per-topic cooldown or the daily cap is
        spent (a topic must still have enough mass samples in the window to
        classify — run it a few times, or pre-seed ``aiko.interest_mass``,
        if the series is cold). Returns the run result (``drafted``,
        ``topic``, ``direction`` …) or a skip reason. Pair with
        ``force_interest_drift_surface`` + ``send_message`` to land the cue.
        """
        worker = getattr(session, "_interest_drift_worker", None)
        if worker is None:
            return (
                "interest_drift worker not registered "
                "(agent.interest_drift_enabled may be off, or no memory "
                "store / topic graph)"
            )
        try:
            worker.force_next()
            result = worker.run()
        except Exception as exc:
            return f"force_interest_drift raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def force_interest_drift_surface() -> str:
        """K64b — arm a one-shot bypass on the provider's gates.

        Sets ``_interest_drift_force_next`` so the next provider call
        surfaces the newest journal entry regardless of topic relevance or
        the surfaced-keys set (the ring must still be non-empty — draft one
        first via ``force_interest_drift``). Then ``send_message`` and
        verify the "you've been drawn to X lately" / "X has gone quiet" line
        lands in ``get_last_response_detail``'s system_prompt.
        """
        try:
            session._interest_drift_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call surfaces the newest drift, "
                        "ignoring topic-relevance + surfaced gates"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_interest_drift_surface raised: {exc}"

    @mcp.tool()
    def get_dormant_interest_state() -> str:
        """K67 — dump the dormant-interest re-opener worker + provider state.

        Shows the master switch, the worker registration, the kv journal ring
        of drafted re-openers (``aiko.dormant_interests`` — topic + dormancy
        age + size), the per-topic cooldown map, the surfaced-keys set and the
        wall-clock surfacing-cooldown stamp the provider tracks, and the
        one-shot provider force flag. First stop for "why didn't Aiko re-open
        X that we used to talk about all the time?". Remember the provider also
        needs a live conversational lull (K18 ``last_mean`` below the
        mild-stagnation threshold) to surface — ``force_dormant_interest_surface``
        bypasses that.
        """
        worker = getattr(session, "_dormant_interest_worker", None)
        out: dict[str, Any] = {
            "registered": worker is not None,
            "enabled": bool(
                getattr(
                    session._settings.agent, "dormant_interest_enabled", True,
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_dormant_interest_force_next", False)
            ),
        }
        detector = getattr(session, "_topic_stagnation_detector", None)
        out["lull_mean"] = getattr(detector, "last_mean", None)
        out["lull_threshold"] = float(
            getattr(
                session._memory_settings, "stagnation_mild_threshold", 0.18,
            )
        )
        chat_db = getattr(session, "_chat_db", None)
        if chat_db is not None and hasattr(chat_db, "kv_get"):
            try:
                from app.core.proactive.dormant_interest_worker import (
                    load_dormant,
                )

                out["journal"] = load_dormant(chat_db.kv_get)
            except Exception as exc:  # pragma: no cover -- diag tool
                out["journal_error"] = str(exc)
            try:
                raw = chat_db.kv_get("dormant_interest.surfaced_keys")
                out["surfaced_keys"] = json.loads(raw) if raw else []
            except Exception:
                out["surfaced_keys"] = []
            try:
                out["surfaced_clock"] = chat_db.kv_get(
                    "dormant_interest.surfaced_clock"
                )
            except Exception:
                out["surfaced_clock"] = None
            try:
                raw = chat_db.kv_get("dormant_interest.topic_cooldowns")
                out["topic_cooldowns"] = json.loads(raw) if raw else {}
            except Exception:
                out["topic_cooldowns"] = {}
        if worker is not None:
            try:
                out["interval_seconds"] = worker.interval_seconds
            except Exception as exc:  # pragma: no cover -- diag tool
                out["worker_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_dormant_interest() -> str:
        """K67 — run the DormantInterestWorker once, bypassing the caps.

        Scans cluster activity and drafts the most-dormant qualifying interest
        even if it's on its per-topic cooldown or the daily cap is spent (a
        cluster must still clear the ``min_size`` peak-mass + ``dormant_days``
        age gates — there must actually be a once-big topic that's gone quiet).
        Returns the run result (``drafted``, ``topic``, ``days_since`` …) or a
        skip reason. Pair with ``force_dormant_interest_surface`` +
        ``send_message`` to land the cue.
        """
        worker = getattr(session, "_dormant_interest_worker", None)
        if worker is None:
            return (
                "dormant_interest worker not registered "
                "(agent.dormant_interest_enabled may be off, or no memory "
                "store / topic graph)"
            )
        try:
            worker.force_next()
            result = worker.run()
        except Exception as exc:
            return f"force_dormant_interest raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def force_dormant_interest_surface() -> str:
        """K67 — arm a one-shot bypass on the provider's gates.

        Sets ``_dormant_interest_force_next`` so the next provider call
        surfaces the newest journal entry regardless of the natural-lull gate,
        the wall-clock surfacing cooldown, or the surfaced-keys set (the ring
        must still be non-empty — draft one first via
        ``force_dormant_interest``). Then ``send_message`` and verify the "we
        haven't talked about X in ages" line lands in
        ``get_last_response_detail``'s system_prompt.
        """
        try:
            session._dormant_interest_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call surfaces the newest re-opener, "
                        "ignoring the lull + cooldown + surfaced gates"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_dormant_interest_surface raised: {exc}"

    @mcp.tool()
    def get_curiosity_gradient_state() -> str:
        """K64c — dump the curiosity-gradient worker + provider state.

        Shows the master switch, the worker registration, the kv journal
        ring of drafted edges (``aiko.curiosity_gradients``), the per-edge
        cooldown map, the surfaced-keys set the provider tracks, the
        one-shot provider force flag, and a dry-run of the gradient-edge
        picker (which thin-on-the-rim-of-dense edges would be drafted next).
        First stop for "why isn't Aiko curious about the edges of X?".
        """
        worker = getattr(session, "_curiosity_gradient_worker", None)
        out: dict[str, Any] = {
            "registered": worker is not None,
            "enabled": bool(
                getattr(
                    session._settings.agent,
                    "curiosity_gradient_enabled",
                    True,
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_curiosity_gradient_force_next", False)
            ),
        }
        chat_db = getattr(session, "_chat_db", None)
        if chat_db is not None and hasattr(chat_db, "kv_get"):
            try:
                from app.core.proactive.curiosity_gradient_worker import (
                    load_gradients,
                )

                out["journal"] = load_gradients(chat_db.kv_get)
            except Exception as exc:  # pragma: no cover -- diag tool
                out["journal_error"] = str(exc)
            try:
                raw = chat_db.kv_get("curiosity_gradient.surfaced_keys")
                out["surfaced_keys"] = json.loads(raw) if raw else []
            except Exception:
                out["surfaced_keys"] = []
            try:
                raw = chat_db.kv_get("curiosity_gradient.edge_cooldowns")
                out["edge_cooldowns"] = json.loads(raw) if raw else {}
            except Exception:
                out["edge_cooldowns"] = {}
        if worker is not None:
            try:
                out["interval_seconds"] = worker.interval_seconds
            except Exception as exc:  # pragma: no cover -- diag tool
                out["worker_error"] = str(exc)
            graph = getattr(session, "_topic_graph", None)
            if graph is not None:
                try:
                    from app.core.proactive.curiosity_gradient_worker import (
                        find_gradient_edges,
                    )

                    edges = find_gradient_edges(
                        graph.topic_clusters(),
                        dense_min_size=worker._dense_min_size,
                        thin_min_size=worker._thin_min_size,
                        thin_max_size=worker._thin_max_size,
                        adjacency_min=worker._adjacency_min_cosine,
                        adjacency_max=worker._adjacency_max_cosine,
                    )
                    out["candidate_edges"] = [
                        {
                            "dense_topic": e.dense_label[:80],
                            "thin_topic": e.thin_label[:80],
                            "cosine": round(e.cosine, 3),
                            "key": e.key,
                        }
                        for e in edges[:5]
                    ]
                except Exception as exc:  # pragma: no cover -- diag tool
                    out["candidate_edges_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_curiosity_gradient() -> str:
        """K64c — run the CuriosityGradientWorker once, bypassing caps.

        Drafts the strongest curiosity edge (thin cluster on the rim of a
        dense one) even if it's on its per-edge cooldown or the daily cap is
        spent (the topic graph must still have a qualifying edge). Returns
        the run result (``drafted``, ``dense_topic``, ``thin_topic`` …) or a
        skip reason. Pair with ``force_curiosity_gradient_surface`` +
        ``send_message`` to land the cue end-to-end.
        """
        worker = getattr(session, "_curiosity_gradient_worker", None)
        if worker is None:
            return (
                "curiosity_gradient worker not registered "
                "(agent.curiosity_gradient_enabled may be off, or no memory "
                "store / topic graph)"
            )
        try:
            worker.force_next()
            result = worker.run()
        except Exception as exc:
            return f"force_curiosity_gradient raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def force_curiosity_gradient_surface() -> str:
        """K64c — arm a one-shot bypass on the provider's gates.

        Sets ``_curiosity_gradient_force_next`` so the next provider call
        surfaces the newest journal entry regardless of topic relevance or
        the surfaced-keys set (the ring must still be non-empty — draft one
        first via ``force_curiosity_gradient``). Then ``send_message`` and
        verify the "you spend a lot of time around X but Y sits on its edge"
        line lands in ``get_last_response_detail``'s system_prompt.
        """
        try:
            session._curiosity_gradient_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call surfaces the newest edge, "
                        "ignoring topic-relevance + surfaced gates"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_curiosity_gradient_surface raised: {exc}"

    @mcp.tool()
    def get_knowledge_map_reflection_state() -> str:
        """K64d — dump the knowledge-map self-reflection worker state.

        Shows the master switch, the worker registration + interval, the
        wall-clock cooldown stamp (``knowledge_map_reflection.last_fired_at``),
        and a dry-run of the graph *shape* the worker would reflect on (the
        richest territories via ``interest_map`` + the under-researched ones
        via ``knowledge_gap_clusters``). First stop for "why hasn't Aiko
        noticed the lopsidedness of what she knows?". The reflection itself
        lands as a ``[mindmap]`` ``kind="reflection"`` memory that surfaces
        through the existing RAG / K28 turning-over path (no dedicated
        provider), so verify the written row via the Memory tab / RAG, not a
        prompt block.
        """
        worker = getattr(session, "_knowledge_map_reflection_worker", None)
        out: dict[str, Any] = {
            "registered": worker is not None,
            "enabled": bool(
                getattr(
                    session._settings.agent,
                    "knowledge_map_reflection_enabled",
                    True,
                )
            ),
        }
        chat_db = getattr(session, "_chat_db", None)
        if chat_db is not None and hasattr(chat_db, "kv_get"):
            try:
                out["last_fired_at"] = chat_db.kv_get(
                    "knowledge_map_reflection.last_fired_at"
                )
            except Exception:
                out["last_fired_at"] = None
        if worker is not None:
            try:
                out["interval_seconds"] = worker.interval_seconds
                out["cooldown_hours"] = worker._cooldown_hours
                out["min_clusters"] = worker._min_clusters
                out["force_next"] = bool(worker._force_next)
            except Exception as exc:  # pragma: no cover -- diag tool
                out["worker_error"] = str(exc)
            graph = getattr(session, "_topic_graph", None)
            if graph is not None and worker is not None:
                try:
                    rich, gaps = worker._read_shape(graph)
                    out["rich_territories"] = [
                        {"topic": label[:80], "size": size}
                        for label, size in rich[: worker._rich_top_n]
                    ]
                    out["under_explored"] = [
                        {"topic": label[:80], "size": size}
                        for label, size in gaps
                    ]
                except Exception as exc:  # pragma: no cover -- diag tool
                    out["shape_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_knowledge_map_reflection() -> str:
        """K64d — run the KnowledgeMapReflectionWorker once, bypassing cooldown.

        Reads the live topic-graph shape, runs the worker-LLM meta-thought,
        and writes one ``[mindmap]`` reflection memory (the graph must still
        have at least ``min_clusters`` labelled clusters). Returns the run
        result (``wrote``, ``memory_id``, ``reflection`` …) or a skip reason
        (``no_graph`` / ``no_context`` / ``no_llm`` / ``deduped`` …). The
        written reflection then surfaces naturally on a later turn via RAG /
        K28 turning-over — confirm it landed in the Memory tab.
        """
        worker = getattr(session, "_knowledge_map_reflection_worker", None)
        if worker is None:
            return (
                "knowledge_map_reflection worker not registered "
                "(agent.knowledge_map_reflection_enabled may be off, or no "
                "memory store / embedder / worker LLM / chat db)"
            )
        try:
            worker.force_next()
            result = worker.run()
        except Exception as exc:
            return f"force_knowledge_map_reflection raised: {exc}"
        return json.dumps(result or {}, indent=2, default=str)

    @mcp.tool()
    def get_topic_temperature_state() -> str:
        """F10h — dump per-cluster affect ("topic temperature") state.

        Shows the master switch, the similarity / charge / cooldown knobs,
        the live cooldown remaining, the last fire, and a dry-run scan of
        every topic cluster's temperature scored from its shared-moment
        vibes (only the *charged* clusters — warm or tender — are listed).
        First stop for "why didn't Aiko soften on that tender topic?".
        """
        out: dict[str, Any] = {
            "enabled": bool(
                getattr(
                    session._settings.agent, "topic_temperature_enabled", True
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_topic_temperature_force_next", False)
            ),
            "cooldown_remaining": int(
                getattr(session, "_topic_temperature_cooldown", 0) or 0
            ),
            "last_fire": getattr(session, "_topic_temperature_last", None),
            "mood_origin_enabled": bool(
                getattr(
                    session._settings.agent,
                    "topic_mood_origin_enabled",
                    True,
                )
            ),
        }
        mem = getattr(session, "_memory_settings", None)
        out["settings"] = {
            "min_sim": float(getattr(mem, "topic_temperature_min_sim", 0.45)),
            "threshold": float(
                getattr(mem, "topic_temperature_threshold", 0.5)
            ),
            "cooldown_turns": int(
                getattr(mem, "topic_temperature_cooldown_turns", 6)
            ),
        }
        # H8: surface the per-cluster mood-origin side-table.
        try:
            from app.core.conversation.topic_temperature import KV_MOOD_ORIGIN

            chat_db = getattr(session, "_chat_db", None)
            raw = chat_db.kv_get(KV_MOOD_ORIGIN) if chat_db else None
            out["mood_origins"] = json.loads(raw) if raw else {}
        except Exception as exc:  # pragma: no cover -- diag tool
            out["mood_origins_error"] = str(exc)
        graph = getattr(session, "_topic_graph", None)
        store = getattr(session, "_memory_store", None)
        if graph is not None and store is not None:
            try:
                from app.core.conversation.topic_temperature import (
                    score_cluster,
                )

                threshold = out["settings"]["threshold"]
                charged: list[dict[str, Any]] = []
                for cluster in graph.topic_clusters():
                    ids = getattr(cluster, "member_ids", ()) or ()
                    kinds = getattr(cluster, "member_kinds", ()) or ()
                    vibes: list[str] = []
                    for mid, kind in zip(ids, kinds):
                        if kind != "shared_moment":
                            continue
                        m = store.get(mid)
                        meta = (
                            getattr(m, "metadata", None) or {}
                            if m is not None
                            else {}
                        )
                        vibe = (
                            meta.get("vibe")
                            if isinstance(meta, dict)
                            else None
                        )
                        if vibe:
                            vibes.append(str(vibe))
                    if not vibes:
                        continue
                    temp = score_cluster(vibes, threshold=threshold)
                    if temp.dominant is None:
                        continue
                    charged.append(
                        {
                            "cluster_id": cluster.cluster_id,
                            "label": (cluster.summary or "")[:120],
                            "dominant": temp.dominant,
                            "warmth": temp.warmth,
                            "tenderness": temp.tenderness,
                            "moment_count": temp.moment_count,
                        }
                    )
                out["charged_clusters"] = charged
            except Exception as exc:  # pragma: no cover -- diag tool
                out["charged_clusters_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_topic_temperature_surface() -> str:
        """F10h — arm a one-shot bypass on the topic-temperature provider.

        Sets ``_topic_temperature_force_next`` so the next provider call
        ignores the cooldown and drops the similarity + charge thresholds
        to 0 (the matched cluster must still have at least one vibed
        shared moment). Then ``send_message`` with text on that topic and
        verify the warm / tender Heads-up line lands in
        ``get_last_response_detail``'s system_prompt.
        """
        try:
            session._topic_temperature_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores cooldown + drops the "
                        "min_sim / charge thresholds to 0 for the matched "
                        "cluster"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_topic_temperature_surface raised: {exc}"

    @mcp.tool()
    def clear_topic_mood_origins() -> str:
        """H8 — wipe the per-cluster mood-origin side-table.

        Clears ``aiko.topic_mood_origin`` so the next time each charged
        cluster surfaces it re-stamps its origin moment from scratch. Use
        to re-test the stamping path end-to-end after editing shared
        moments. Does not touch the temperature scoring itself.
        """
        try:
            from app.core.conversation.topic_temperature import KV_MOOD_ORIGIN

            chat_db = getattr(session, "_chat_db", None)
            if chat_db is None:
                return json.dumps({"cleared": False, "reason": "no chat_db"})
            before = chat_db.kv_get(KV_MOOD_ORIGIN)
            count = 0
            if before:
                try:
                    count = len(json.loads(before) or {})
                except Exception:
                    count = 0
            chat_db.kv_set(KV_MOOD_ORIGIN, "{}")
            return json.dumps(
                {"cleared": True, "origins_removed": count}, indent=2
            )
        except Exception as exc:
            return f"clear_topic_mood_origins raised: {exc}"

    @mcp.tool()
    def get_topic_confidence_state() -> str:
        """F10i — dump per-topic confidence self-model state.

        Shows the master switch, the similarity / thin / familiar / cooldown
        knobs, the live cooldown remaining, the last fire, and a dry-run
        scan of every cluster's confidence (size + learned-fact coverage),
        listing only the *banded* ones (thin → hedge, familiar → speak
        from what you know). First stop for "why did Aiko bluff / over-hedge
        about that topic?".
        """
        out: dict[str, Any] = {
            "enabled": bool(
                getattr(
                    session._settings.agent, "topic_confidence_enabled", True
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_topic_confidence_force_next", False)
            ),
            "cooldown_remaining": int(
                getattr(session, "_topic_confidence_cooldown", 0) or 0
            ),
            "last_fire": getattr(session, "_topic_confidence_last", None),
        }
        mem = getattr(session, "_memory_settings", None)
        out["settings"] = {
            "min_sim": float(getattr(mem, "topic_confidence_min_sim", 0.45)),
            "thin_threshold": float(
                getattr(mem, "topic_confidence_thin_threshold", 0.25)
            ),
            "familiar_threshold": float(
                getattr(mem, "topic_confidence_familiar_threshold", 0.7)
            ),
            "cooldown_turns": int(
                getattr(mem, "topic_confidence_cooldown_turns", 6)
            ),
        }
        graph = getattr(session, "_topic_graph", None)
        if graph is not None:
            try:
                from app.core.conversation.topic_confidence import (
                    score_confidence,
                )

                learned_kinds = {"knowledge", "curiosity_finding"}
                thin = out["settings"]["thin_threshold"]
                familiar = out["settings"]["familiar_threshold"]
                banded: list[dict[str, Any]] = []
                for cluster in graph.topic_clusters():
                    kinds = getattr(cluster, "member_kinds", ()) or ()
                    size = len(kinds)
                    learned = sum(1 for k in kinds if k in learned_kinds)
                    conf = score_confidence(
                        size,
                        learned,
                        thin_threshold=thin,
                        familiar_threshold=familiar,
                    )
                    if conf.band is None:
                        continue
                    banded.append(
                        {
                            "cluster_id": cluster.cluster_id,
                            "label": (cluster.summary or "")[:120],
                            "band": conf.band,
                            "confidence": conf.confidence,
                            "size": conf.size,
                            "learned_count": conf.learned_count,
                        }
                    )
                out["banded_clusters"] = banded
            except Exception as exc:  # pragma: no cover -- diag tool
                out["banded_clusters_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_topic_confidence_surface() -> str:
        """F10i — arm a one-shot bypass on the topic-confidence provider.

        Sets ``_topic_confidence_force_next`` so the next provider call
        ignores the cooldown, drops ``min_sim`` to 0, and splits the bands
        at 0.5 (so the matched cluster always lands in thin or familiar).
        Then ``send_message`` with text on that topic and verify the hedge
        / earned-familiarity line lands in ``get_last_response_detail``'s
        system_prompt.
        """
        try:
            session._topic_confidence_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores cooldown + min_sim and "
                        "forces a thin/familiar band (split at 0.5) on the "
                        "matched cluster"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_topic_confidence_surface raised: {exc}"

    @mcp.tool()
    def get_earned_familiarity_state() -> str:
        """K66 — dump the earned-familiarity register-cue state + dry-run.

        Shows the master switch, the similarity / deep-mass / cooldown
        knobs, the live cooldown remaining, the last fire, and a dry-run
        scan of every cluster that currently reads as *deep* shared ground
        (mass at/above the deep threshold). First stop for "why did Aiko
        (not) lean on shorthand for that topic?". Orthogonal to
        ``get_topic_confidence_state`` (knowledge richness): this is pure
        shared-history depth.
        """
        out: dict[str, Any] = {
            "enabled": bool(
                getattr(
                    session._settings.agent, "earned_familiarity_enabled", True
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_earned_familiarity_force_next", False)
            ),
            "cooldown_remaining": int(
                getattr(session, "_earned_familiarity_cooldown", 0) or 0
            ),
            "last_fire": getattr(session, "_earned_familiarity_last", None),
        }
        mem = getattr(session, "_memory_settings", None)
        deep_threshold = int(
            getattr(mem, "earned_familiarity_deep_threshold", 14)
        )
        out["settings"] = {
            "min_sim": float(
                getattr(mem, "earned_familiarity_min_sim", 0.45)
            ),
            "deep_threshold": deep_threshold,
            "cooldown_turns": int(
                getattr(mem, "earned_familiarity_cooldown_turns", 12)
            ),
        }
        graph = getattr(session, "_topic_graph", None)
        if graph is not None:
            try:
                from app.core.conversation.earned_familiarity import (
                    score_familiarity,
                )

                deep: list[dict[str, Any]] = []
                for cluster in graph.topic_clusters():
                    kinds = getattr(cluster, "member_kinds", ()) or ()
                    size = len(kinds)
                    read = score_familiarity(
                        size, deep_threshold=deep_threshold
                    )
                    if read.band is None:
                        continue
                    deep.append(
                        {
                            "cluster_id": cluster.cluster_id,
                            "label": (cluster.summary or "")[:120],
                            "size": read.size,
                        }
                    )
                deep.sort(key=lambda d: d["size"], reverse=True)
                out["deep_clusters"] = deep
            except Exception as exc:  # pragma: no cover -- diag tool
                out["deep_clusters_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_earned_familiarity_surface() -> str:
        """K66 — arm a one-shot bypass on the earned-familiarity provider.

        Sets ``_earned_familiarity_force_next`` so the next provider call
        ignores the cooldown, drops ``min_sim`` to 0, and forces the deep
        band on the matched cluster (deep_threshold → 1). Then
        ``send_message`` with text on that topic and verify the shorthand /
        skip-the-recap line lands in ``get_last_response_detail``'s
        system_prompt.
        """
        try:
            session._earned_familiarity_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores cooldown + min_sim and "
                        "forces the deep band on the matched cluster"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_earned_familiarity_surface raised: {exc}"

    @mcp.tool()
    def get_upcoming_horizon_state() -> str:
        """K-time3 — dump the upcoming-horizon cue state + a dry-run scan.

        Shows the master switch, the horizon / max-items / cooldown knobs,
        the live cooldown remaining + last surfaced signature, and a
        dry-run of the forward sweep: every ``future_plan`` memory due
        within the window with its pre-resolved relative phrase. First
        stop for "why didn't Aiko mention the thing coming up tomorrow?".
        """
        out: dict[str, Any] = {
            "enabled": bool(
                getattr(
                    session._settings.agent, "upcoming_horizon_enabled", True
                )
            ),
            "provider_force_next": bool(
                getattr(session, "_upcoming_horizon_force_next", False)
            ),
            "cooldown_remaining": int(
                getattr(session, "_upcoming_horizon_cooldown", 0) or 0
            ),
            "last_signature": getattr(session, "_upcoming_horizon_sig", ""),
        }
        mem = getattr(session, "_memory_settings", None)
        out["settings"] = {
            "horizon_days": int(getattr(mem, "upcoming_horizon_days", 7)),
            "max_items": int(getattr(mem, "upcoming_horizon_max_items", 3)),
            "cooldown_turns": int(
                getattr(mem, "upcoming_horizon_cooldown_turns", 6)
            ),
        }
        store = getattr(session, "_memory_store", None)
        if store is not None:
            try:
                from app.core.conversation.upcoming_horizon import (
                    build_signature,
                    select_upcoming,
                )
                from app.core.infra import timephrase

                now = timephrase.now()
                events = select_upcoming(
                    store.list_by_temporal_type("future_plan"),
                    now,
                    horizon_days=out["settings"]["horizon_days"],
                    max_items=out["settings"]["max_items"],
                )
                out["upcoming"] = [
                    {
                        "id": getattr(m, "id", None),
                        "content": (getattr(m, "content", "") or "")[:120],
                        "event_time": getattr(m, "event_time", None),
                        "resolved": timephrase.humanize_future(
                            getattr(m, "event_time", None), now
                        ),
                    }
                    for m in events
                ]
                out["current_signature"] = build_signature(events)
            except Exception as exc:  # pragma: no cover -- diag tool
                out["upcoming_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_upcoming_horizon_surface() -> str:
        """K-time3 — arm a one-shot bypass on the upcoming-horizon provider.

        Sets ``_upcoming_horizon_force_next`` so the next provider call
        ignores the cooldown + signature gate (the horizon window must
        still hold at least one ``future_plan``). Then ``send_message``
        and verify the "Coming up ..." line lands in
        ``get_last_response_detail``'s system_prompt.
        """
        try:
            session._upcoming_horizon_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores the cooldown + "
                        "signature gate (window must hold >=1 future_plan)"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_upcoming_horizon_surface raised: {exc}"

    @mcp.tool()
    def get_session_clock_state() -> str:
        """K-time4 — dump the session-clock cue state + a dry-run measure.

        Shows the master switch, the elapsed / break / pause-band knobs,
        the live watermarks (current sitting key, strongest elapsed band
        already surfaced, last pause anchor, force flag), and a dry-run of
        the derived signal off the recent-message timestamps: the current
        continuous-sitting duration + band and the pause before the latest
        message. First stop for "why didn't Aiko notice we'd been at this
        for an hour / that I stepped away?".
        """
        agent = session._settings.agent
        out: dict[str, Any] = {
            "enabled": bool(getattr(agent, "session_clock_enabled", True)),
            "force_next": bool(
                getattr(session, "_session_clock_force_next", False)
            ),
            "burst_key": getattr(session, "_session_clock_burst_key", None),
            "fired_band": getattr(session, "_session_clock_fired_band", None),
            "gap_anchor": getattr(session, "_session_clock_gap_anchor", None),
            "settings": {
                "long_minutes": float(
                    getattr(agent, "session_clock_long_minutes", 60.0)
                ),
                "very_long_minutes": float(
                    getattr(agent, "session_clock_very_long_minutes", 150.0)
                ),
                "break_minutes": float(
                    getattr(agent, "session_clock_break_minutes", 30.0)
                ),
                "gap_min_minutes": float(
                    getattr(agent, "session_clock_gap_min_minutes", 10.0)
                ),
                "gap_max_minutes": float(
                    getattr(agent, "session_clock_gap_max_minutes", 30.0)
                ),
            },
        }
        try:
            from app.core.conversation import session_clock as _sc
            from app.core.infra import timephrase

            rows = session._chat_db.get_messages(session.session_key, limit=60)
            times_desc = [
                ts
                for ts in (
                    timephrase.parse_iso(getattr(r, "created_at", None))
                    for r in reversed(rows)
                )
                if ts is not None
            ]
            now = timephrase.now()
            signal = _sc.classify(
                times_desc,
                now,
                long_seconds=out["settings"]["long_minutes"] * 60.0,
                very_long_seconds=out["settings"]["very_long_minutes"] * 60.0,
                break_seconds=out["settings"]["break_minutes"] * 60.0,
                gap_min_seconds=out["settings"]["gap_min_minutes"] * 60.0,
                gap_max_seconds=out["settings"]["gap_max_minutes"] * 60.0,
            )
            out["measure"] = {
                "elapsed_seconds": round(signal.elapsed_seconds, 1),
                "elapsed_band": signal.elapsed_band,
                "burst_start_iso": signal.burst_start_iso,
                "gap_seconds": round(signal.gap_seconds, 1),
                "gap_notable": signal.gap_notable,
                "rows_seen": len(times_desc),
            }
        except Exception as exc:  # pragma: no cover -- diag tool
            out["measure_error"] = str(exc)
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def force_session_clock_surface() -> str:
        """K-time4 — arm a one-shot bypass on the session-clock provider.

        Sets ``_session_clock_force_next`` so the next provider call
        ignores the per-band / per-pause watermarks and renders whatever
        the live signal currently is (an elapsed band and/or a notable
        pause must still actually hold). Then ``send_message`` and verify
        the "been talking for ..." / "was away about ..." line lands in
        ``get_last_response_detail``'s system_prompt.
        """
        try:
            session._session_clock_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call ignores the band / pause "
                        "watermarks (a live elapsed band and/or notable "
                        "pause must still hold)"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_session_clock_surface raised: {exc}"

    @mcp.tool()
    def get_topic_tracking_state() -> str:
        """F10k — dump the novelty detector's semantic topic-tracking state.

        Shows the master switch, the ``topic_tracking_min_sim`` floor, and
        the per-turn cluster signals from the last ``detect()`` call
        (matched cluster id + label, whether it changed, whether it's a
        *return* to a previously-visited cluster, and the label of the
        cluster moved from), plus the rolling prev-cluster + visited-set
        state. First stop for "why did the novelty cue say brand-new when
        Jacob came back to a topic?" — a low ``last_cluster_*`` with the
        graph warm usually means the turn fell below ``topic_tracking_min_sim``.
        """
        det = getattr(session, "_novelty_detector", None)
        out: dict[str, Any] = {
            "enabled": bool(
                getattr(session._settings.agent, "topic_tracking_enabled", True)
            ),
            "detector_present": det is not None,
            "tracking_active": bool(
                det is not None
                and getattr(det, "_topic_graph_provider", None) is not None
            ),
            "min_sim": float(
                getattr(
                    getattr(session, "_memory_settings", None),
                    "topic_tracking_min_sim",
                    0.30,
                )
            ),
        }
        if det is not None:
            out["last_turn"] = {
                "cluster_id": getattr(det, "last_cluster_id", None),
                "cluster_label": getattr(det, "last_cluster_label", ""),
                "changed": bool(getattr(det, "last_cluster_changed", False)),
                "returning": bool(getattr(det, "last_cluster_returning", False)),
                "prev_cluster_label": getattr(
                    det, "last_prev_cluster_label", ""
                ),
            }
            out["rolling"] = {
                "prev_cluster_id": getattr(det, "_prev_cluster_id", None),
                "prev_cluster_label": getattr(det, "_prev_cluster_label", ""),
                "visited_count": len(getattr(det, "_visited_clusters", ()) or ()),
            }
        return json.dumps(out, indent=2, default=str)

    @mcp.tool()
    def get_topic_digest_state() -> str:
        """F10g — dump the per-cluster rolling digest worker's state.

        Shows the master switch, the RAG surfacing switch + sibling cap,
        and the live ``cluster_digest_map`` (``{cluster_id: memory_id}``)
        the worker rebuilds each tick and the retriever reads to surface a
        cluster's digest as the coarse "what I know about X" line. For each
        mapped cluster it resolves the current label (from the live graph)
        and a short preview of the digest memory's content. First stop for
        "why didn't Aiko's reply lean on the topic digest?" — an empty map
        means the worker hasn't run yet (or no cluster is dense enough);
        a populated map with a missing/garbage preview means the digest
        memory was deleted out from under the map (stale until next tick).
        """
        worker = getattr(session, "_topic_digest_worker", None)
        graph = getattr(session, "_topic_graph", None)
        store = getattr(session, "_memory_store", None)
        out: dict[str, Any] = {
            "enabled": bool(
                getattr(session._settings.agent, "topic_digest_enabled", True)
            ),
            "worker_present": worker is not None,
            "surface_in_rag": bool(
                getattr(
                    session._settings.agent, "topic_digest_surface_in_rag", True
                )
            ),
            "sibling_cap": int(
                getattr(session._settings.agent, "rag_digest_sibling_cap", 1)
            ),
            "min_cluster_size": int(
                getattr(session._settings.agent, "topic_digest_min_cluster_size", 6)
            ),
        }
        labels: dict[int, str] = {}
        if graph is not None:
            try:
                for c in graph.topic_clusters():
                    labels[int(c.cluster_id)] = str(
                        getattr(c, "summary", "") or ""
                    )[:60]
            except Exception:
                pass
        mapped: list[dict[str, Any]] = []
        if worker is not None:
            for cid, mem_id in dict(
                getattr(worker, "cluster_digest_map", {}) or {}
            ).items():
                row: dict[str, Any] = {
                    "cluster_id": int(cid),
                    "memory_id": int(mem_id),
                    "label": labels.get(int(cid), ""),
                }
                if store is not None:
                    try:
                        mem = store.get(int(mem_id))
                    except Exception:
                        mem = None
                    row["preview"] = (
                        (str(getattr(mem, "content", "")) or "")[:120]
                        if mem is not None
                        else None
                    )
                mapped.append(row)
        out["mapped_count"] = len(mapped)
        out["mapped"] = mapped[:30]
        return json.dumps(out, indent=2, default=str)


