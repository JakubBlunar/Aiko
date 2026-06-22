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
            pick = worker._pick_cluster(now=now)
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


