from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")


def register(mcp, session: "SessionController") -> None:
    @mcp.tool()
    def send_message(message: str, skip_tts: bool = False) -> str:
        """Send a message to Aiko and return her full response.

        The UI updates live (chat bubble, etc.). Set ``skip_tts=True`` to
        suppress audio playback during automated testing.

        Chunk 7 of the brain-orchestration refactor: this tool now
        routes through :meth:`SessionController.enqueue_user_message`
        which puts a :class:`UserMessageEvent` on the brain queue
        and blocks on a :class:`concurrent.futures.Future` for the
        reply. The serialisation guarantees a real Cursor MCP call
        can't race a user-typed message in the WS chat path —
        whichever lands on the queue first gets the turn, the other
        is dispatched on the next loop tick. The legacy
        ``session.chat_once`` fallback is hit only when the task
        subsystem is disabled (``agent.tasks_enabled=False``).
        """
        session._notify_message("You (MCP)", message)
        response = session.enqueue_user_message(
            text=message,
            mode="mcp",
            skip_tts=bool(skip_tts),
            wait_for_reply=True,
            # MCP debug clients tolerate long blocking calls; a real
            # turn rarely exceeds 30s but reflection + dream passes
            # piggy-backed on the turn can push to a minute. 120s
            # matches the mixin default — explicit here so the limit
            # is documented at the call site.
            timeout=120.0,
        )
        session._notify_message("Assistant", response or "")
        return response or "(empty response)"

    @mcp.tool()
    def get_status() -> str:
        """Return JSON: model, context window, TTS state, last metrics."""
        info = {
            "model": session.effective_chat_model,
            "context_window": session.context_window_size,
            "tts_provider": session.tts_provider,
            "tts_voice": session.tts_voice,
            "tts_enabled": session._settings.tts.enabled,
            "session_key": session.session_key,
            "live_mode": getattr(session, "_live_voice_session_active", False),
            "last_metrics": session.get_last_metrics(),
        }
        return json.dumps(info, indent=2, default=str)

    @mcp.tool()
    def get_last_response_detail() -> str:
        """Return the last turn's full timing + token usage as JSON."""
        return json.dumps(session.get_last_metrics(), indent=2, default=str)

    @mcp.tool()
    def clear_history() -> str:
        """Wipe the active session's conversation memory."""
        try:
            session.clear_conversation_memory()
            return f"History cleared for session '{session.session_key}'."
        except Exception as exc:
            return f"Failed to clear history: {exc}"

    @mcp.tool()
    def list_agent_tools() -> str:
        """Return JSON list of every tool currently registered on the agent.

        Walks the live ``SessionController._tool_registry`` so the
        result reflects the actual catalogue the LLM sees in
        ``chat_with_tools`` -- including world tools (look_around /
        move_to / change_posture / inspect_item / consume_item)
        whenever ``settings.tools.world`` is enabled. Returns an
        empty list only if the registry hasn't been built yet
        (e.g. during the very first session boot).
        """
        registry = getattr(session, "_tool_registry", None)
        if registry is None:
            return json.dumps([], indent=2)
        try:
            return json.dumps(registry.describe(), indent=2)
        except Exception as exc:
            return json.dumps(
                {"error": f"failed to introspect tool registry: {exc}"},
                indent=2,
            )

    @mcp.tool()
    def list_skills() -> str:
        """Return JSON of every skill across both lanes (skills framework).

        Unifies the two catalogues into one ``{name, lane, group}`` view:
        the fast **brain** tools (with their P14 family as ``group`` — the
        unit the brain skill router narrows to) and the heavy **worker**
        skills (with their per-capability / per-MCP-server ``group``). Use
        this to see, in one place, which lane and group a capability lives
        in when debugging the skill router. See docs/skills-framework.md.
        """
        from app.core.session.tool_pass_gate import _TOOL_FAMILY

        out: list[dict[str, str]] = []
        brain = getattr(session, "_tool_registry", None)
        if brain is not None:
            try:
                for entry in brain.describe():
                    name = entry.get("name", "")
                    out.append({
                        "name": name,
                        "lane": "brain",
                        "group": _TOOL_FAMILY.get(name, ""),
                    })
            except Exception:
                pass
        worker = getattr(session, "_workflow_skill_registry", None)
        if worker is not None:
            try:
                for entry in worker.describe_for_planner():
                    out.append({
                        "name": entry.get("name", ""),
                        "lane": "worker",
                        "group": entry.get("group", ""),
                    })
            except Exception:
                pass
        return json.dumps(out, indent=2)

    @mcp.tool()
    def get_tool_gate_state() -> str:
        """P14 — dump the heuristic tool-pass gate state.

        Returns a JSON dict with the master switch
        (``agent.tool_pass_gate_enabled``), the one-shot
        ``force_next`` flag, the last gate decision (``run`` /
        ``reason`` / matched pattern families), the per-process
        counters (``turns_gated`` / ``passes_skipped`` /
        ``passes_run``), the rolling average cost of a real pass
        (``avg_pass_ms``), the estimated total ms saved by skips,
        and the ``last_turn_dispatched_tool`` continuity flag. Also
        carries the skills-framework fields: ``router_enabled``
        (``agent.skill_router_enabled``), ``core_skills`` (the always-on
        families, default time/recall/world), and ``last_active_tools``
        (the narrowed tool set sent on the last run pass, or null).

        First stop when "Aiko stopped using tools" comes up: a
        ``last_decision.reason`` of ``no_signal`` on a turn that
        *should* have used a tool means the signal-pattern table in
        ``app/core/session/tool_pass_gate.py`` is missing a shape —
        either extend the family patterns or flip
        ``agent.tool_pass_gate_enabled=false`` as the kill-switch.
        Per-decision tracing: ``tail_logs(module_contains=
        "tool_pass_gate")`` shows one ``tool-gate:`` line per turn.
        """
        try:
            runner = getattr(session, "_turn_runner", None)
            if runner is None:
                return json.dumps({"error": "turn runner not built yet"})
            return json.dumps(runner.get_tool_gate_state(), indent=2)
        except Exception as exc:
            return f"get_tool_gate_state raised: {exc}"

    @mcp.tool()
    def force_tool_pass() -> str:
        """P14 — arm a one-shot bypass on the tool-pass gate.

        Sets the runner's ``_tool_gate_force_next`` flag so the next
        turn runs the forced tool-decision pass regardless of the
        text heuristic (``reason="force"``). Consumed on the next
        turn whether or not a tool is actually dispatched — strictly
        one-shot.

        End-to-end repro: call this, then ``send_message`` with pure
        banter ("hey, how are you?", ``skip_tts=true``) and read
        ``get_last_response_detail`` — ``tool_gate_event`` should be
        ``run:force`` with a non-zero ``tool_pass_ms``. Without the
        flag the same message lands ``skip:no_signal`` and
        ``tool_pass_ms=0``.
        """
        try:
            runner = getattr(session, "_turn_runner", None)
            if runner is None:
                return json.dumps({"error": "turn runner not built yet"})
            runner._tool_gate_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next turn runs the tool-decision pass "
                        "unconditionally; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_tool_pass raised: {exc}"

    @mcp.tool()
    def feed_stt_partial(partial_text: str) -> str:
        """Inject a fake STT partial transcript for testing backchannel hints.

        Useful while the audio-side partial pipeline is still being wired:
        send any sentence and we'll run it through the regex classifier and
        broadcast the resulting backchannel WS event (if any). Returns the
        hint that fired or 'none' when the text was neutral.
        """
        try:
            hint = session.feed_stt_partial(partial_text)
        except Exception as exc:
            return f"feed_stt_partial failed: {exc}"
        return hint or "none"

    @mcp.tool()
    def get_mood_state() -> str:
        """Return Aiko's current persistent mood snapshot (Phase 2b)."""
        try:
            store = session._affect_store  # type: ignore[attr-defined]
            user_id = session._user_id  # type: ignore[attr-defined]
            state = store.get(user_id)
            return json.dumps(state.to_payload(), indent=2, default=str)
        except Exception as exc:
            return f"get_mood_state failed: {exc}"

    @mcp.tool()
    def get_contagion_state(user_text: str = "") -> str:
        """K37 — inspect emotional-contagion config + a dry-run estimate.

        Returns the master switch + strength/cap knobs, Aiko's current
        (valence, arousal), and — when ``user_text`` is supplied — the
        ``estimate_user_affect`` result for that text (mood/energy +
        dialogue-act, no vocal tone) plus the capped (dv, da) Aiko would
        move this turn. ``user_affect=null`` means no readable signal, so
        contagion would stay silent.
        """
        try:
            from app.core.affect.affect_state import (
                _apply_user_contagion,
                estimate_user_affect,
            )

            agent = session._settings.agent
            out: dict[str, Any] = {
                "enabled": bool(getattr(agent, "contagion_enabled", True)),
                "strength": float(getattr(agent, "contagion_strength", 0.15)),
                "max_per_turn": float(
                    getattr(agent, "contagion_max_per_turn", 0.05)
                ),
            }
            state = session._affect_store.get(session._user_id)
            out["aiko"] = {"valence": state.valence, "arousal": state.arousal}
            if user_text.strip():
                mood = energy = None
                est = getattr(session, "_user_state_estimator", None)
                if est is not None:
                    now = est.estimate(session._user_id, user_text=user_text)
                    mood, energy = now.perceived_mood, now.perceived_energy
                try:
                    from app.core.conversation.dialogue_act_tagger import tag_regex
                    dact = tag_regex(user_text).act
                except Exception:
                    dact = None
                user_affect = estimate_user_affect(
                    mood=mood, energy=energy, dialogue_act=dact,
                )
                out["detected"] = {
                    "mood": mood, "energy": energy, "dialogue_act": dact,
                }
                out["user_affect"] = user_affect
                if user_affect is not None:
                    nv, na = _apply_user_contagion(
                        state.valence, state.arousal, user_affect,
                        strength=out["strength"], cap=out["max_per_turn"],
                    )
                    out["would_move"] = {
                        "dv": round(nv - state.valence, 4),
                        "da": round(na - state.arousal, 4),
                    }
            return json.dumps(out, indent=2, default=str)
        except Exception as exc:
            return f"get_contagion_state failed: {exc}"

    @mcp.tool()
    def get_question_balance_state() -> str:
        """K47 — inspect the question/share balance gate.

        Returns the master switch + ratio/window/suppress knobs, the
        current rolling question-turn flags ring (newest last), the live
        ratio, whether suppression is currently armed (and how many turns
        remain), and the share-first cue that would render right now.
        While ``suppress_remaining > 0`` the question-pushing inner-life
        providers (curiosity_seeds / forward_curiosity / follow_up /
        knowledge_gaps + the narrative open_question nudge) are muted.
        """
        try:
            from app.core.conversation.question_balance import (
                compute_ratio,
                render_share_first_cue,
            )

            agent = session._settings.agent
            flags = list(getattr(session, "_question_turn_flags", []) or [])
            remaining = int(
                getattr(session, "_question_balance_suppress_remaining", 0)
            )
            out = {
                "enabled": bool(
                    getattr(agent, "question_balance_enabled", True)
                ),
                "ratio_threshold": float(
                    getattr(agent, "question_balance_ratio_threshold", 0.55)
                ),
                "window": int(
                    getattr(agent, "question_balance_window", 10)
                ),
                "suppress_turns": int(
                    getattr(agent, "question_balance_suppress_turns", 2)
                ),
                "flags": [bool(f) for f in flags],
                "samples": len(flags),
                "ratio": round(compute_ratio(flags), 4),
                "suppress_remaining": remaining,
                "suppressed_now": remaining > 0,
                "cue_preview": (
                    render_share_first_cue(session.user_display_name)
                    if remaining > 0 else None
                ),
            }
            return json.dumps(out, indent=2, default=str)
        except Exception as exc:
            return f"get_question_balance_state failed: {exc}"

    @mcp.tool()
    def force_question_balance() -> str:
        """K47 — arm the question/share gate so the next turn suppresses
        the question-pushing cues and surfaces the share-first cue.

        Sets ``_question_balance_suppress_remaining`` to the configured
        ``suppress_turns`` without needing a real high-ratio streak. The
        post-turn hook will decay it normally afterward.
        """
        try:
            agent = session._settings.agent
            turns = max(
                1, int(getattr(agent, "question_balance_suppress_turns", 2))
            )
            session._question_balance_suppress_remaining = turns
            return json.dumps(
                {"ok": True, "suppress_remaining": turns},
                indent=2,
            )
        except Exception as exc:
            return f"force_question_balance failed: {exc}"

    @mcp.tool()
    def get_tease_rhythm_state() -> str:
        """K48 — inspect the tease-rhythm banter budget.

        Returns the master switch + knobs, the rolling tease-flag ring
        (newest last), the trailing tease streak, the id of the most
        recent tease being watched for a landing verdict, the live humor
        axis, the pending cue (if armed) + its rendered text, and the
        cooldown remainder.
        """
        try:
            from app.core.conversation.tease_rhythm import (
                render_cue,
                trailing_tease_streak,
            )

            agent = session._settings.agent
            flags = list(getattr(session, "_tease_flags", []) or [])
            pending = getattr(session, "_pending_tease_cue", None)
            humor = 0.0
            try:
                store = getattr(session, "_relationship_axes_store", None)
                if store is not None:
                    humor = float(store.get(session._user_id).humor)
            except Exception:
                pass
            out = {
                "enabled": bool(
                    getattr(agent, "tease_rhythm_enabled", True)
                ),
                "window": int(getattr(agent, "tease_rhythm_window", 6)),
                "consecutive_cap": int(
                    getattr(agent, "tease_rhythm_consecutive_cap", 3)
                ),
                "green_light_humor": float(
                    getattr(agent, "tease_rhythm_green_light_humor", 0.2)
                ),
                "cooldown_turns": int(
                    getattr(agent, "tease_rhythm_cooldown_turns", 3)
                ),
                "flags": [bool(f) for f in flags],
                "tease_streak": trailing_tease_streak(flags),
                "last_tease_message_id": getattr(
                    session, "_last_tease_message_id", None
                ),
                "humor": round(humor, 4),
                "cooldown_remaining": int(
                    getattr(session, "_tease_cue_cooldown", 0)
                ),
                "pending_cue": pending,
                "pending_cue_text": (
                    render_cue(pending, user_name=session.user_display_name)
                    if pending else None
                ),
            }
            return json.dumps(out, indent=2, default=str)
        except Exception as exc:
            return f"get_tease_rhythm_state failed: {exc}"

    @mcp.tool()
    def force_tease_rhythm(cue: str = "green_light") -> str:
        """K48 — arm a tease-rhythm cue for the next turn.

        ``cue`` is ``ease_off`` or ``green_light``. The provider renders
        it on the next assembly (bypassing the cooldown + landing logic).
        """
        try:
            from app.core.conversation.tease_rhythm import (
                CUE_EASE_OFF,
                CUE_GREEN_LIGHT,
            )

            norm = (cue or "").strip().lower()
            if norm not in (CUE_EASE_OFF, CUE_GREEN_LIGHT):
                return json.dumps(
                    {
                        "error": "unknown cue",
                        "valid": [CUE_EASE_OFF, CUE_GREEN_LIGHT],
                    },
                    indent=2,
                )
            session._tease_rhythm_force = norm
            return json.dumps({"ok": True, "cue": norm}, indent=2)
        except Exception as exc:
            return f"force_tease_rhythm failed: {exc}"

    @mcp.tool()
    def get_circadian_state() -> str:
        """Return the current circadian state (Phase 2e)."""
        try:
            from app.core.affect import circadian as _circ
            state = _circ.compute()
            payload = {
                "period": state.period,
                "energy": state.energy,
                "drowsy": state.drowsy,
                "sociability_bias": state.sociability_bias,
                "hour": state.hour,
                "minute": state.minute,
                "ambient_line": state.ambient_line(),
            }
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"get_circadian_state failed: {exc}"

    @mcp.tool()
    def get_scheduler_stats() -> str:
        """Return SpeakingWindowScheduler counters + queue depth (Phase 2a)."""
        try:
            return json.dumps(
                session.scheduler.snapshot(), indent=2, default=str,
            )
        except Exception as exc:
            return f"get_scheduler_stats failed: {exc}"

    @mcp.tool()
    def get_rag_prefetcher_stats() -> str:
        """Return RagPrefetcher counters + cache size (Phase 1b)."""
        try:
            prefetcher = getattr(session, "_rag_prefetcher", None)
            if prefetcher is None:
                return json.dumps({"enabled": False}, indent=2)
            payload = {"enabled": True, **prefetcher.stats()}
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"get_rag_prefetcher_stats failed: {exc}"

    @mcp.tool()
    def get_message_indexer_stats() -> str:
        """Return MessageIndexer counters + live queue / thread state (P6).

        Watch ``queue_depth`` for embed back-pressure, ``pending_retries``
        for transient embed/write failures in back-off, and ``gave_up`` +
        ``last_give_up`` for rows that fell out of RAG until the next
        startup backfill.
        """
        try:
            indexer = getattr(session, "_message_indexer", None)
            if indexer is None:
                return json.dumps({"enabled": False}, indent=2)
            payload = {"enabled": True, **indexer.stats()}
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"get_message_indexer_stats failed: {exc}"

    @mcp.tool()
    def get_reflection_stats() -> str:
        """Return ReflectionWorker counters (Phase 2c)."""
        try:
            worker = getattr(session, "_reflection_worker", None)
            if worker is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **worker.stats()}, indent=2, default=str,
            )
        except Exception as exc:
            return f"get_reflection_stats failed: {exc}"

    @mcp.tool()
    def get_self_image_stats() -> str:
        """Return SelfImageWorker counters + last-known mtime (Phase 2d)."""
        try:
            worker = getattr(session, "_self_image_worker", None)
            payload: dict[str, object] = {"enabled": worker is not None}
            if worker is not None:
                payload.update(worker.stats())
                try:
                    target = worker._target_path  # type: ignore[attr-defined]
                    if target.exists():
                        payload["target_path"] = str(target)
                        payload["mtime"] = target.stat().st_mtime
                        payload["should_run_now"] = worker.should_run()
                except Exception:
                    pass
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"get_self_image_stats failed: {exc}"

    @mcp.tool()
    def get_user_profile() -> str:
        """Return Aiko's persisted profile of the user (Phase 3a)."""
        try:
            store = getattr(session, "_user_profile_store", None)
            if store is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, "fields": store.as_dict(session._user_id)},
                indent=2, default=str,
            )
        except Exception as exc:
            return f"get_user_profile failed: {exc}"

    @mcp.tool()
    def list_agenda(status: str = "open", limit: int = 20) -> str:
        """List agenda items (Phase 4a). status: open | done | dropped | all."""
        try:
            store = getattr(session, "_agenda_store", None)
            if store is None:
                return json.dumps({"enabled": False}, indent=2)
            if status == "all":
                items = store.list_all(session._user_id, limit=int(limit))
            else:
                items = [
                    i for i in store.list_all(session._user_id, limit=int(limit) * 4)
                    if i.status == status
                ][: int(limit)]
            return json.dumps(
                {"items": [i.to_dict() for i in items]},
                indent=2, default=str,
            )
        except Exception as exc:
            return f"list_agenda failed: {exc}"

    @mcp.tool()
    def get_agenda_stats() -> str:
        """Return AgendaWorker counters (Phase 4a)."""
        try:
            worker = getattr(session, "_agenda_worker", None)
            if worker is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **worker.stats()}, indent=2, default=str,
            )
        except Exception as exc:
            return f"get_agenda_stats failed: {exc}"

    @mcp.tool()
    def get_consolidator_stats() -> str:
        """Return MemoryConsolidator counters (Phase 4b)."""
        try:
            worker = getattr(session, "_consolidator", None)
            if worker is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **worker.stats()}, indent=2, default=str,
            )
        except Exception as exc:
            return f"get_consolidator_stats failed: {exc}"

    @mcp.tool()
    def trigger_consolidator() -> str:
        """Manually run the consolidator now (bypasses throttling)."""
        try:
            worker = getattr(session, "_consolidator", None)
            if worker is None:
                return json.dumps({"enabled": False}, indent=2)
            result = worker.force_run(session._user_id)
            if result is None:
                return json.dumps({"ran": False, "reason": "no memories"}, indent=2)
            return json.dumps(
                {
                    "ran": True,
                    "clusters_found": result.clusters_found,
                    "merges_applied": result.merges_applied,
                    "deletions": result.deletions,
                    "elapsed_seconds": round(result.elapsed_seconds, 2),
                },
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"trigger_consolidator failed: {exc}"

    @mcp.tool()
    def get_arc_state() -> str:
        """Return the current conversation arc + confidence (Phase 4c)."""
        try:
            store = getattr(session, "_arc_store", None)
            if store is None:
                return json.dumps({"enabled": False}, indent=2)
            state = store.get(session._user_id)
            if state is None:
                return json.dumps({"arc": "casual_check_in", "confidence": 0.5}, indent=2)
            return json.dumps(state.to_payload(), indent=2, default=str)
        except Exception as exc:
            return f"get_arc_state failed: {exc}"

    @mcp.tool()
    def get_arc_smoother_stats() -> str:
        """Return ArcSmootherWorker counters (Phase 4c)."""
        try:
            worker = getattr(session, "_arc_smoother", None)
            if worker is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **worker.stats()}, indent=2, default=str,
            )
        except Exception as exc:
            return f"get_arc_smoother_stats failed: {exc}"

    @mcp.tool()
    def get_prepared_nudge() -> str:
        """Return the current prepared nudge if fresh (Phase 4c)."""
        try:
            store = getattr(session, "_prepared_nudge_store", None)
            if store is None:
                return json.dumps({"enabled": False}, indent=2)
            nudge = store.get_fresh(session._user_id)
            if nudge is None:
                return json.dumps({"prepared": None}, indent=2)
            return json.dumps(nudge.to_payload(), indent=2, default=str)
        except Exception as exc:
            return f"get_prepared_nudge failed: {exc}"

    @mcp.tool()
    def get_narrative_weaver_stats() -> str:
        """Return NarrativeWeaver counters (Phase 4c)."""
        try:
            worker = getattr(session, "_narrative_weaver", None)
            if worker is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **worker.stats()}, indent=2, default=str,
            )
        except Exception as exc:
            return f"get_narrative_weaver_stats failed: {exc}"

    @mcp.tool()
    def get_cadence_stats() -> str:
        """Return ProsodyDispatcher counters (Phase 5b)."""
        try:
            dispatcher = getattr(session, "_prosody", None)
            if dispatcher is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **dispatcher.stats()}, indent=2, default=str,
            )
        except Exception as exc:
            return f"get_cadence_stats failed: {exc}"

    @mcp.tool()
    def analyze_cadence(text: str, reaction: str = "neutral") -> str:
        """Show how Aiko would prosody-analyze a sentence right now."""
        try:
            dispatcher = getattr(session, "_prosody", None)
            if dispatcher is None:
                return json.dumps({"enabled": False}, indent=2)
            params = dispatcher.analyze(text, reaction=reaction)
            return json.dumps(
                {
                    "reaction": params.reaction,
                    "pause_before_ms": params.pause_before_ms,
                    "pause_after_ms": params.pause_after_ms,
                    "prefix_text": params.prefix_text,
                    "prefix_reaction": params.prefix_reaction,
                    "speed_hint": params.speed_hint,
                    "rationale": params.rationale,
                },
                indent=2,
                default=str,
            )
        except Exception as exc:
            return f"analyze_cadence failed: {exc}"

    @mcp.tool()
    def get_proactive_stats() -> str:
        """Return ProactiveDirector counters (prepared vs LLM path)."""
        try:
            director = getattr(session, "_proactive", None)
            if director is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **director.stats()}, indent=2, default=str,
            )
        except Exception as exc:
            return f"get_proactive_stats failed: {exc}"

    @mcp.tool()
    def get_relationship_pulse_stats() -> str:
        """Return RelationshipPulseWorker counters (Phase 4b)."""
        try:
            worker = getattr(session, "_relationship_pulse", None)
            if worker is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **worker.stats()}, indent=2, default=str,
            )
        except Exception as exc:
            return f"get_relationship_pulse_stats failed: {exc}"

    @mcp.tool()
    def get_promise_stats() -> str:
        """Return PromiseExtractionWorker config + rate-limiter state.

        The promise extractor was reworked into a context-aware idle
        worker (``promise_worker``); this surfaces its master switch,
        cadence, context budgets, and the current hour/day rate-limit
        spend. Use ``force_run("promise_worker")`` to trigger a pass.
        """
        try:
            worker = getattr(session, "_promise_worker", None)
            if worker is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                worker.debug_state(), indent=2, default=str,
            )
        except Exception as exc:
            return f"get_promise_stats failed: {exc}"

    @mcp.tool()
    def list_promises(limit: int = 10) -> str:
        """List recent promise memories (Phase 3c)."""
        try:
            store = getattr(session, "_memory_store", None)
            if store is None:
                return json.dumps([], indent=2)
            top = store.list_recent(limit=max(1, int(limit) * 4))
            promises = [
                {
                    "id": m.id,
                    "content": m.content,
                    "salience": float(m.salience),
                    "created_at": m.created_at,
                }
                for m in top
                if (m.kind or "").lower() == "promise"
            ][: max(1, int(limit))]
            return json.dumps(promises, indent=2, default=str)
        except Exception as exc:
            return f"list_promises failed: {exc}"

    @mcp.tool()
    def get_goals_state() -> str:
        """Return Aiko's long-term goal store snapshot (K1).

        Surfaces every active and archived goal with its summary,
        ``reflection_count``, ``last_reflected_at``, ``last_progress_note``,
        and source. Includes a ``next_reflection_candidate`` slot
        showing which goal the worker would pick on the next
        reflection tick (oldest-touched active goal). Useful for
        verifying the bootstrap pass landed, watching the per-goal
        reflection history grow without paging through the Memory tab,
        and confirming the worker's pick order.
        """
        try:
            store = getattr(session, "_goal_store", None)
            if store is None:
                return json.dumps(
                    {"enabled": False, "reason": "goal store unavailable"},
                    indent=2,
                )
            active = store.list_active()
            agent_cfg = getattr(session._settings, "agent", None)
            memory_cfg = getattr(session._settings, "memory", None)
            payload: dict[str, Any] = {
                "enabled": True,
                "settings": {
                    "goals_enabled": bool(
                        getattr(agent_cfg, "goals_enabled", True),
                    ),
                    "bootstrap_enabled": bool(
                        getattr(
                            agent_cfg, "goal_worker_bootstrap_enabled", True,
                        ),
                    ),
                    "per_hour_cap": int(
                        getattr(agent_cfg, "goal_worker_per_hour_cap", 3),
                    ),
                    "per_day_cap": int(
                        getattr(agent_cfg, "goal_worker_per_day_cap", 12),
                    ),
                    "max_active": int(
                        getattr(memory_cfg, "goal_max_active", 5),
                    ),
                    "max_progress_per_goal": int(
                        getattr(
                            memory_cfg, "goal_max_progress_per_goal", 12,
                        ),
                    ),
                    "reflection_interval_seconds": int(
                        getattr(
                            memory_cfg,
                            "goal_reflection_interval_seconds",
                            3600,
                        ),
                    ),
                },
                "active_count": len(active),
                "goals": [],
            }
            for goal in active:
                meta = goal.metadata or {}
                progress = store.list_progress(int(goal.id))
                payload["goals"].append({
                    "id": int(goal.id),
                    "summary": meta.get("summary") or goal.content,
                    "source": meta.get("source"),
                    "created_at": goal.created_at,
                    "last_reflected_at": meta.get("last_reflected_at"),
                    "reflection_count": int(
                        meta.get("reflection_count", 0) or 0,
                    ),
                    "last_progress_note": meta.get("last_progress_note"),
                    "pinned": bool(getattr(goal, "pinned", False)),
                    "tier": getattr(goal, "tier", "long_term"),
                    "progress_rows": len(progress),
                })
            try:
                candidate = store.pick_for_reflection()
                if candidate is not None:
                    cmeta = candidate.metadata or {}
                    payload["next_reflection_candidate"] = {
                        "id": int(candidate.id),
                        "summary": cmeta.get("summary") or candidate.content,
                        "last_reflected_at": cmeta.get("last_reflected_at"),
                    }
                else:
                    payload["next_reflection_candidate"] = None
            except Exception:
                payload["next_reflection_candidate"] = None
            worker = getattr(session, "_goal_worker", None)
            payload["worker_registered"] = worker is not None
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"get_goals_state failed: {exc}"

    @mcp.tool()
    def force_goal_worker() -> str:
        """Run :class:`GoalWorker` once, bypassing the idle/interval gate.

        Returns the worker's result dict (bootstrap branch keys
        ``checked`` / ``wrote`` / ``memory_ids`` when the ring is
        cold, or reflection branch ``goal_id`` / ``progress_id`` /
        ``note`` once at least one goal exists). The rate limiter is
        still consulted, so calling this repeatedly will start
        returning ``{"skipped": true, "reason": "rate_limited"}``
        once the per-hour cap is reached.
        """
        try:
            worker = getattr(session, "_goal_worker", None)
            if worker is None:
                return json.dumps(
                    {"enabled": False, "reason": "goal worker unavailable"},
                    indent=2,
                )
            result = worker.run()
            return json.dumps(result or {}, indent=2, default=str)
        except Exception as exc:
            return f"force_goal_worker failed: {exc}"

    @mcp.tool()
    def get_relationship_state() -> str:
        """Return relationship phase + counters (Phase 3b)."""
        try:
            tracker = getattr(session, "_relationship_tracker", None)
            if tracker is None:
                return json.dumps({"enabled": False}, indent=2)
            state = tracker.get(session._user_id)
            payload = {
                "enabled": True,
                "phase": tracker.current_phase(session._user_id),
                "ambient_line": tracker.ambient_line(session._user_id),
                **state.to_payload(),
            }
            return json.dumps(payload, indent=2, default=str)
        except Exception as exc:
            return f"get_relationship_state failed: {exc}"

    @mcp.tool()
    def get_user_state() -> str:
        """Return the per-turn user-state snapshot (Phase 3a)."""
        try:
            store = getattr(session, "_user_state_store", None)
            if store is None:
                return json.dumps({"enabled": False}, indent=2)
            state = store.get(session._user_id)
            return json.dumps(
                {"enabled": True, **state.to_payload()},
                indent=2, default=str,
            )
        except Exception as exc:
            return f"get_user_state failed: {exc}"

    @mcp.tool()
    def trigger_self_image_pulse() -> str:
        """Force a self-image pulse now (Phase 2d). Bypasses the daily gate."""
        try:
            worker = getattr(session, "_self_image_worker", None)
            if worker is None:
                return "self-image worker not enabled"
            target = worker._target_path  # type: ignore[attr-defined]
            try:
                if target.exists():
                    target.unlink()
            except Exception:
                pass
            text = worker.pulse()
            return text or "(no input — nothing written)"
        except Exception as exc:
            return f"trigger_self_image_pulse failed: {exc}"

    # ── Logs / debug introspection ────────────────────────────────────

    @mcp.tool()
    def tail_logs(
        n: int = 200,
        level: str = "INFO",
        module_contains: str | None = None,
    ) -> str:
        """Return the most recent log lines from the in-process ring buffer.

        ``level`` is the minimum severity (DEBUG/INFO/WARNING/ERROR).
        ``module_contains`` filters by logger name substring (e.g.
        ``"prompt"`` matches ``app.core.session.prompt_assembler``).
        """
        from app.core.infra.crash_logging import tail
        try:
            lines = tail(n=int(n), level=str(level), module_contains=module_contains)
        except Exception as exc:
            return f"tail_logs failed: {exc}"
        if not lines:
            return "(no log lines matched)"
        return "\n".join(lines)

    @mcp.tool()
    def read_log_file(
        lines: int = 500,
        level: str = "INFO",
        grep: str | None = None,
    ) -> str:
        """Tail the rotating ``data/app.log`` (and rolled siblings if needed).

        For cross-session investigations beyond the in-process ring's
        ~1000-line window. ``grep`` is a case-insensitive substring.
        """
        from app.core.infra.crash_logging import read_log_file as _read
        try:
            collected = _read(lines=int(lines), level=str(level), grep=grep)
        except Exception as exc:
            return f"read_log_file failed: {exc}"
        if not collected:
            return "(no log lines matched)"
        return "\n".join(collected)

    @mcp.tool()
    def set_log_level(module: str, level: str) -> str:
        """Bump a single logger to ``level`` at runtime (until app restart).

        Example: ``set_log_level("app.core.session.prompt_assembler", "DEBUG")``.
        Returns the resulting effective level.
        """
        from app.core.infra.crash_logging import set_module_level
        try:
            resolved = set_module_level(str(module), str(level))
        except Exception as exc:
            return f"set_log_level failed: {exc}"
        return f"{module} -> {resolved}"

    @mcp.tool()
    def get_log_config() -> str:
        """Return the active logging configuration: file path, levels, ring size."""
        try:
            from app.core.infra.crash_logging import (
                RING_BUFFER_CAPACITY,
                get_log_file_path,
                _RING_HANDLER,
            )
            settings_logging = getattr(session._settings, "logging", None)
            payload = {
                "level": getattr(settings_logging, "level", "INFO"),
                "file_enabled": bool(getattr(settings_logging, "file_enabled", True)),
                "file_path": str(get_log_file_path() or ""),
                "module_levels": dict(getattr(settings_logging, "module_levels", {}) or {}),
                "ring_capacity": RING_BUFFER_CAPACITY,
                "ring_used": len(_RING_HANDLER.snapshot()) if _RING_HANDLER else 0,
            }
            return json.dumps(payload, indent=2)
        except Exception as exc:
            return f"get_log_config failed: {exc}"

    # ── Schema v8: memory tiers + idle workers (E1/E2/G1) ───────────


