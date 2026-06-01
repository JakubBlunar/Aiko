"""FastMCP debug server for the lean Aiko app.

Exposes a small surface for Cursor / VSCode MCP clients to drive the running
session: send messages, inspect status, clear history, peek at the latest
metrics. Browser-snapshot and agent-tool tools from the legacy build are
gone -- v1 has no agent tools yet.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP


if TYPE_CHECKING:
    from app.core.session.session_controller import SessionController


log = logging.getLogger("app.mcp.server")
_session_ref: "SessionController | None" = None


def create_mcp_server(session: "SessionController", port: int = 6274) -> FastMCP:
    """Build a FastMCP server wired to the live ``session``."""
    global _session_ref
    _session_ref = session
    mcp = FastMCP("assistant", host="127.0.0.1", port=port)

    # ── Tools ────────────────────────────────────────────────────────

    @mcp.tool()
    def send_message(message: str, skip_tts: bool = False) -> str:
        """Send a message to Aiko and return her full response.

        The UI updates live (chat bubble, etc.). Set ``skip_tts=True`` to
        suppress audio playback during automated testing.
        """
        session._notify_message("You (MCP)", message)
        original = session._settings.tts.enabled
        if skip_tts:
            session._settings.tts.enabled = False
        try:
            response = session.chat_once(message)
        finally:
            session._settings.tts.enabled = original
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
        """Return PromiseExtractor counters (Phase 3c)."""
        try:
            extractor = getattr(session, "_promise_extractor", None)
            if extractor is None:
                return json.dumps({"enabled": False}, indent=2)
            return json.dumps(
                {"enabled": True, **extractor.stats()},
                indent=2, default=str,
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
    def get_self_noticing_state() -> str:
        """K30 — dump the live self-noticing detector state.

        Returns a JSON dict with the master switch, the three
        sub-switches, the most recent verdict from each sub-detector
        (agreement-streak share + sample size, flat-affect ranges +
        notable reaction count, repeated-thought cosine + matched
        index), the live cooldown remainders, the one-shot
        ``force_next`` flags, the size of the in-memory affect ring
        and assistant-vector ring, and a settings snapshot so you
        can see what thresholds are actually in force.

        The two cooldown counters (agreement, flat-affect) decrement
        by one each provider call regardless of trigger state -- a
        value of 0 means the next eligible turn can fire. Repeated-
        thought has no multi-turn cooldown; the one-shot
        carry-forward flag is set in ``post_turn`` and consumed by
        the next provider call.
        """
        try:
            agent = session._settings.agent

            def _verdict_payload(verdict: object | None) -> dict | None:
                if verdict is None:
                    return None
                payload = {}
                for field in (
                    "fired",
                    "agreement_share",
                    "pushback_share",
                    "valence_range",
                    "arousal_range",
                    "notable_reaction_count",
                    "max_cosine",
                    "matched_index",
                    "sample_size",
                ):
                    if hasattr(verdict, field):
                        value = getattr(verdict, field)
                        if isinstance(value, bool):
                            payload[field] = bool(value)
                        elif isinstance(value, int):
                            payload[field] = int(value)
                        elif isinstance(value, float):
                            payload[field] = round(float(value), 4)
                return payload

            affect_ring = getattr(session, "_self_noticing_affect_samples", None)
            vec_ring = getattr(session, "_self_noticing_aiko_vecs", None)
            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "self_noticing_enabled", True)
                    ),
                    "sub_switches": {
                        "agreement_streak": bool(
                            getattr(
                                agent,
                                "self_noticing_agreement_streak_enabled",
                                True,
                            )
                        ),
                        "flat_affect": bool(
                            getattr(
                                agent,
                                "self_noticing_flat_affect_enabled",
                                True,
                            )
                        ),
                        "repeated_thought": bool(
                            getattr(
                                agent,
                                "self_noticing_repeated_thought_enabled",
                                True,
                            )
                        ),
                    },
                    "agreement_streak": {
                        "cooldown_remaining": int(
                            getattr(
                                session,
                                "_self_noticing_agreement_cooldown",
                                0,
                            ) or 0
                        ),
                        "force_next": bool(
                            getattr(
                                session,
                                "_self_noticing_force_agreement",
                                False,
                            )
                        ),
                        "last_verdict": _verdict_payload(
                            getattr(
                                session,
                                "_last_self_noticing_agreement",
                                None,
                            )
                        ),
                    },
                    "flat_affect": {
                        "cooldown_remaining": int(
                            getattr(
                                session,
                                "_self_noticing_flat_affect_cooldown",
                                0,
                            ) or 0
                        ),
                        "force_next": bool(
                            getattr(
                                session,
                                "_self_noticing_force_flat_affect",
                                False,
                            )
                        ),
                        "affect_ring_size": (
                            len(affect_ring) if affect_ring is not None else 0
                        ),
                        "last_verdict": _verdict_payload(
                            getattr(
                                session,
                                "_last_self_noticing_flat_affect",
                                None,
                            )
                        ),
                    },
                    "repeated_thought": {
                        "flagged_for_next_turn": bool(
                            getattr(
                                session,
                                "_repeated_thought_fired_last_turn",
                                False,
                            )
                        ),
                        "force_next": bool(
                            getattr(
                                session,
                                "_self_noticing_force_repeated_thought",
                                False,
                            )
                        ),
                        "last_cosine": round(
                            float(
                                getattr(
                                    session,
                                    "_repeated_thought_last_cosine",
                                    0.0,
                                )
                            ),
                            4,
                        ),
                        "last_matched_index": int(
                            getattr(
                                session,
                                "_repeated_thought_last_matched_index",
                                -1,
                            )
                        ),
                        "vec_ring_size": (
                            len(vec_ring) if vec_ring is not None else 0
                        ),
                    },
                    "settings": {
                        "window": int(
                            getattr(agent, "self_noticing_window", 6)
                        ),
                        "warmup": int(
                            getattr(agent, "self_noticing_warmup", 4)
                        ),
                        "agreement_threshold": float(
                            getattr(
                                agent,
                                "self_noticing_agreement_threshold",
                                0.80,
                            )
                        ),
                        "max_pushback": int(
                            getattr(
                                agent, "self_noticing_max_pushback", 0,
                            )
                        ),
                        "flat_valence_range": float(
                            getattr(
                                agent,
                                "self_noticing_flat_valence_range",
                                0.10,
                            )
                        ),
                        "flat_arousal_range": float(
                            getattr(
                                agent,
                                "self_noticing_flat_arousal_range",
                                0.10,
                            )
                        ),
                        "repeated_cosine_threshold": float(
                            getattr(
                                agent,
                                "self_noticing_repeated_cosine_threshold",
                                0.85,
                            )
                        ),
                        "cooldown_turns": int(
                            getattr(
                                agent,
                                "self_noticing_cooldown_turns",
                                5,
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_self_noticing_state raised: {exc}"

    @mcp.tool()
    def force_agreement_streak() -> str:
        """K30 — arm a one-shot bypass on the agreement-streak cooldown.

        Sets ``_self_noticing_force_agreement`` so the next provider
        call ignores the cooldown counter AND fires the cue
        regardless of whether the streak actually crosses the
        threshold. Useful for verifying the Heads-up line lands in
        the rendered prompt (call this tool, then ``send_message``,
        then inspect the next ``get_last_response_detail`` output
        for the cue in ``system_prompt``).
        """
        try:
            session._self_noticing_force_agreement = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will surface the agreement-"
                        "streak Heads-up unconditionally; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_agreement_streak raised: {exc}"

    @mcp.tool()
    def force_flat_affect() -> str:
        """K30 — arm a one-shot bypass on the flat-affect cooldown.

        Sets ``_self_noticing_force_flat_affect`` so the next
        provider call ignores the cooldown counter AND fires the
        cue regardless of whether the in-memory affect ring actually
        sits below the configured thresholds. The cue surfaces the
        normal "Heads-up: your read has been pretty even-keel..."
        line; consume by sending one user message.
        """
        try:
            session._self_noticing_force_flat_affect = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will surface the flat-affect "
                        "Heads-up unconditionally; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_flat_affect raised: {exc}"

    @mcp.tool()
    def force_repeated_thought() -> str:
        """K30 — arm a one-shot bypass on the repeated-thought flag.

        Sets ``_self_noticing_force_repeated_thought`` so the next
        provider call surfaces the "your last reply was very close
        to something you already said" Heads-up regardless of the
        actual cosine measurement. The flag is consumed whether the
        cue fires or not -- strictly one-turn.
        """
        try:
            session._self_noticing_force_repeated_thought = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will surface the repeated-"
                        "thought Heads-up unconditionally; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_repeated_thought raised: {exc}"

    @mcp.tool()
    def get_day_color_state() -> str:
        """K27 — dump the live daily personality colour state.

        Returns a JSON dict with the master switch, the current
        stored colour name + ``set_at`` ISO timestamp, the age in
        hours, an ``is_stale`` boolean (true means the next
        provider call will lazy-roll a fresh colour), the worker
        cadence in seconds, both one-shot force flags
        (``force_next`` / ``force_reroll``), and the full palette
        names so a follow-up ``force_day_color`` call doesn't need
        clairvoyance.

        Pairs with ``force_day_color`` / ``reroll_day_color`` for
        end-to-end repro without shifting the OS clock:

        1. Call ``get_day_color_state`` -- read the current name +
           palette.
        2. Call ``force_day_color(color="pensive")`` -- arm the
           one-shot override.
        3. Send a message and read ``get_last_response_detail`` --
           the rendered system prompt should contain the
           "Your day's colour today: pensive --" line.
        4. ``reroll_day_color`` -- the next ``get_day_color_state``
           shows a new name + fresh ``set_at``.
        """
        try:
            from datetime import datetime

            from app.core.affect import day_color
            from app.core.affect.day_color_worker import (
                KV_DAY_COLOR,
                KV_DAY_COLOR_SET_AT,
            )

            agent = session._settings.agent
            chat_db = getattr(session, "_chat_db", None)
            stored_name: str | None = None
            stored_at: str | None = None
            if chat_db is not None:
                try:
                    stored_name = chat_db.kv_get(KV_DAY_COLOR)
                except Exception:
                    stored_name = None
                try:
                    stored_at = chat_db.kv_get(KV_DAY_COLOR_SET_AT)
                except Exception:
                    stored_at = None

            now = datetime.now().astimezone()
            stale = day_color.is_stale(stored_at, now)

            age_hours: float | None = None
            if stored_at:
                try:
                    text = str(stored_at).strip()
                    if text.endswith("Z"):
                        text = text[:-1] + "+00:00"
                    stored_dt = datetime.fromisoformat(text)
                    if stored_dt.tzinfo is None:
                        stored_dt = stored_dt.astimezone()
                    age_hours = round(
                        (now - stored_dt).total_seconds() / 3600.0, 3,
                    )
                except Exception:
                    age_hours = None

            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "day_color_enabled", True)
                    ),
                    "interval_seconds": int(
                        getattr(
                            agent,
                            "day_color_check_interval_seconds",
                            3600,
                        )
                    ),
                    "current": {
                        "name": stored_name,
                        "set_at": stored_at,
                        "age_hours": age_hours,
                        "is_stale": stale,
                    },
                    "force_flags": {
                        "force_next": getattr(
                            session, "_day_color_force_next", None,
                        ),
                        "force_reroll": bool(
                            getattr(
                                session, "_day_color_force_reroll", False,
                            )
                        ),
                    },
                    "palette": [c.name for c in day_color.PALETTE],
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_day_color_state raised: {exc}"

    @mcp.tool()
    def force_day_color(color: str) -> str:
        """K27 — arm a one-shot palette override.

        Sets ``_day_color_force_next`` so the *next* provider call
        renders the requested colour without touching ``kv_meta``
        (the persisted daily roll survives). Validates ``color``
        against the palette and returns ``{"error": "unknown
        color", ...}`` with the palette list when the name isn't
        recognised.

        One-shot: the flag is consumed by the next provider call
        whether or not the cue fires (in practice it always does
        because the validation has already passed).
        """
        try:
            from app.core.affect import day_color

            chosen = day_color.get_color_by_name(color)
            if chosen is None:
                return json.dumps(
                    {
                        "error": "unknown color",
                        "palette": [c.name for c in day_color.PALETTE],
                    },
                    indent=2,
                )
            session._day_color_force_next = chosen.name
            return json.dumps(
                {
                    "armed": True,
                    "color": chosen.name,
                    "tagline": chosen.tagline,
                    "note": (
                        "next provider call will render this colour; "
                        "kv_meta NOT modified; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_day_color raised: {exc}"

    @mcp.tool()
    def reroll_day_color() -> str:
        """K27 — arm a one-shot reroll of today's colour.

        Sets ``_day_color_force_reroll`` so the next provider call
        rolls a fresh palette entry via
        :func:`day_color.roll_for_today` and writes it to
        ``kv_meta`` (overwriting today's stored colour). Useful
        for end-to-end repro without waiting for midnight or
        shifting the OS clock.

        One-shot: the flag is consumed by the next provider call.
        The result lands in ``kv_meta`` so the new colour persists
        for the rest of the local day; subsequent calls hit the
        normal stable-read path until midnight.
        """
        try:
            session._day_color_force_reroll = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will roll a fresh colour "
                        "and write it to kv_meta; one-shot"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"reroll_day_color raised: {exc}"

    @mcp.tool()
    def get_turning_over_state() -> str:
        """K28 — dump the in-memory turning-over picker state.

        Returns a JSON dict with the master switch, the current
        pending-seconds slot (set by the post-turn engagement
        tracker when a long enough typed gap was observed), the
        ``force_next`` flag (armed by ``force_turning_over``),
        the most recent fire (``memory_id`` / ``age_hours`` /
        ``topical_score`` / ``topical_source`` / ``dream`` /
        truncated ``content``), the settings snapshot (5 knobs),
        plus a **dry-run picker result** that calls the picker
        against the *current* memory state without arming the
        cue -- so you can see what *would* surface on the next
        qualifying turn even when the slot isn't currently armed.

        The dry-run respects the configured age window and the
        topical-similarity threshold, so a ``would_surface: null``
        with ``reflections_in_window: N > 0`` means the threshold
        gate is rejecting every candidate.

        Pairs with ``force_turning_over`` for the end-to-end repro:

        1. Call ``get_turning_over_state`` first -- read
           ``would_surface`` to confirm there's a candidate that
           clears the gates.
        2. Call ``force_turning_over`` to arm the one-shot bypass.
        3. Send a message; verify ``tail_logs(module_contains=
           "turning_over")`` shows ``turning-over fire: ...``.
        4. Call ``get_turning_over_state`` again -- ``force_next``
           should be ``false`` (consumed), ``last_fire`` populated.
        """
        try:
            agent = session._settings.agent
            memory = session._memory_settings
            pending_s = getattr(
                session, "_pending_turning_over_seconds", None,
            )
            force_next = bool(
                getattr(session, "_turning_over_force_next", False),
            )
            last = getattr(session, "_last_turning_over", None)
            last_payload = None
            if last is not None:
                last_payload = {
                    "memory_id": int(getattr(last, "memory_id", 0) or 0),
                    "age_hours": float(getattr(last, "age_hours", 0.0)),
                    "topical_score": float(
                        getattr(last, "topical_score", 0.0)
                    ),
                    "topical_source": str(
                        getattr(last, "topical_source", "") or ""
                    ),
                    "dream": bool(getattr(last, "dream", False)),
                    "content": (
                        (getattr(last, "content", "") or "")[:200]
                    ),
                }

            # Dry-run: pick a candidate against the current memory
            # state without arming the cue. Mirrors the live provider's
            # picker call so what we show here is what would land.
            dry_run = None
            reflections_in_window = 0
            try:
                from datetime import datetime, timezone
                from app.core.session.inner_life import turning_over as _to

                memory_store = getattr(session, "_memory_store", None)
                if memory_store is not None:
                    reflections = list(memory_store.iter_by_kind("reflection"))
                    # Count rows in the age window for diagnostic.
                    now = datetime.now(timezone.utc)
                    min_age = float(
                        getattr(
                            memory,
                            "turning_over_min_age_hours",
                            _to.DEFAULT_MIN_AGE_HOURS,
                        )
                    )
                    max_age = float(
                        getattr(
                            memory,
                            "turning_over_max_age_hours",
                            _to.DEFAULT_MAX_AGE_HOURS,
                        )
                    )
                    for mem in reflections:
                        age = _to._parse_age_hours(
                            getattr(mem, "created_at", None), now=now,
                        )
                        if age is None:
                            continue
                        if min_age <= age <= max_age:
                            reflections_in_window += 1
                    goal_store = getattr(session, "_goal_store", None)
                    goal_vecs = []
                    if goal_store is not None:
                        try:
                            goal_vecs = list(goal_store.active_goal_vectors())
                        except Exception:
                            goal_vecs = []
                    msg_vecs = []
                    rag_store = getattr(session, "_rag_store", None)
                    msgs_window = int(
                        getattr(
                            memory,
                            "turning_over_recent_msgs_window",
                            12,
                        )
                    )
                    if rag_store is not None and msgs_window > 0:
                        try:
                            msg_vecs = list(
                                rag_store.list_recent_user_vectors(
                                    user_id_prefix=(
                                        getattr(session, "_user_id", "") or ""
                                    ),
                                    limit=msgs_window,
                                )
                            )
                        except Exception:
                            msg_vecs = []
                    picked = _to.pick_turning_over(
                        reflections=reflections,
                        active_goal_vecs=goal_vecs,
                        recent_user_vecs=msg_vecs,
                        now=now,
                        min_age_hours=min_age,
                        max_age_hours=max_age,
                        min_topical_similarity=float(
                            getattr(
                                memory,
                                "turning_over_min_topical_similarity",
                                _to.DEFAULT_MIN_TOPICAL_SIMILARITY,
                            )
                        ),
                    )
                    if picked is not None:
                        dry_run = {
                            "memory_id": int(picked.memory_id),
                            "age_hours": float(picked.age_hours),
                            "topical_score": float(picked.topical_score),
                            "topical_source": picked.topical_source,
                            "dream": bool(picked.dream),
                            "content": (picked.content or "")[:200],
                        }
            except Exception as dry_exc:
                dry_run = {"error": str(dry_exc)}

            return json.dumps(
                {
                    "enabled": bool(
                        getattr(agent, "turning_over_enabled", True)
                    ),
                    "pending_seconds": (
                        float(pending_s) if pending_s is not None else None
                    ),
                    "force_next": force_next,
                    "last_fire": last_payload,
                    "would_surface": dry_run,
                    "reflections_in_window": reflections_in_window,
                    "settings": {
                        "min_gap_minutes": float(
                            getattr(
                                memory,
                                "turning_over_min_gap_minutes",
                                90.0,
                            )
                        ),
                        "min_age_hours": float(
                            getattr(
                                memory,
                                "turning_over_min_age_hours",
                                24.0,
                            )
                        ),
                        "max_age_hours": float(
                            getattr(
                                memory,
                                "turning_over_max_age_hours",
                                72.0,
                            )
                        ),
                        "min_topical_similarity": float(
                            getattr(
                                memory,
                                "turning_over_min_topical_similarity",
                                0.30,
                            )
                        ),
                        "recent_msgs_window": int(
                            getattr(
                                memory,
                                "turning_over_recent_msgs_window",
                                12,
                            )
                        ),
                    },
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_turning_over_state raised: {exc}"

    @mcp.tool()
    def force_turning_over() -> str:
        """K28 — arm a one-shot bypass on the turning-over gap gate.

        Sets ``_turning_over_force_next`` so the next call to the
        provider treats the pending-slot gate AND the threshold
        double-check as bypassed. The picker still runs, so a
        forced bypass on an empty reflection corpus (or one where
        nothing clears the topical-similarity gate) silently
        expires with no cue. Bypass is consumed regardless --
        strictly one-turn.

        Repro recipe:

        1. Make sure Aiko has at least one ``kind="reflection"``
           memory row written between 24h and 72h ago. Real
           reflections come from the post-turn ``ReflectionWorker``
           or ``DreamWorker``; for testing, you can insert one via
           ``POST /api/memories`` with ``kind=reflection`` and a
           ``created_at`` 30h in the past.
        2. Call ``get_turning_over_state`` -- confirm
           ``would_surface`` is non-null (i.e. there's a candidate
           that clears the gates).
        3. Call this tool.
        4. Send a message; check ``tail_logs(module_contains=
           "turning_over")`` for ``turning-over fire: memory_id=...``.
        5. Aiko's reply should fold in the reflection as a casual
           aside, not as an announcement.
        """
        try:
            session._turning_over_force_next = True
            return json.dumps(
                {
                    "armed": True,
                    "note": (
                        "next provider call will ignore the pending-slot "
                        "gate AND the threshold double-check; picker still "
                        "runs, so an empty reflection corpus or a "
                        "below-threshold candidate silently expires"
                    ),
                },
                indent=2,
            )
        except Exception as exc:
            return f"force_turning_over raised: {exc}"

    @mcp.tool()
    def get_confidence_decay_state(limit: int = 20) -> str:
        """K25 — preview which memory rows would currently render
        with the ``(distant)`` suffix.

        Returns a JSON dict with:

        - ``enabled``: master switch state from :class:`AgentSettings`.
        - ``settings``: the three numeric knobs (``horizon_days``,
          ``floor``, ``distant_threshold``) so user.json overrides
          are visible immediately.
        - ``rows``: top-``limit`` memory rows (most recently used
          first) with ``id``, ``kind``, ``stored_confidence``,
          ``age_days``, ``effective_confidence``, ``pinned``, and
          predicate flags ``distant`` / ``uncertain`` so you can
          eyeball which rows would gain which suffix.

        Pinned rows are included with ``distant=False`` (bypassed)
        so you can confirm pinning is working as intended. This tool
        is the tuning loop for K25: tweak ``user.json``, restart,
        call this, see what would surface differently.
        """
        store = getattr(session, "_memory_store", None)
        if store is None:
            return json.dumps({"enabled": False, "error": "no memory_store"})
        try:
            from datetime import datetime, timezone

            from app.core.rag.rag_retriever import (
                _compute_effective_confidence,
                _is_distant_memory,
            )
        except Exception as exc:
            return f"get_confidence_decay_state import failed: {exc}"
        try:
            agent = session._settings.agent
            mem_settings = session._settings.memory
            enabled = bool(
                getattr(agent, "confidence_time_decay_enabled", True),
            )
            horizon_days = max(
                1,
                int(
                    getattr(
                        mem_settings, "confidence_decay_horizon_days", 365,
                    )
                ),
            )
            floor = max(
                0.0,
                min(
                    1.0,
                    float(
                        getattr(
                            mem_settings, "confidence_decay_floor", 0.3,
                        )
                    ),
                ),
            )
            threshold = max(
                0.0,
                min(
                    1.0,
                    float(
                        getattr(
                            mem_settings,
                            "confidence_decay_distant_threshold",
                            0.5,
                        )
                    ),
                ),
            )
            mirror = getattr(store, "_mirror", None)
            rows_iter = list(mirror.values()) if mirror is not None else []
            # Sort most-recently-used first so the preview shows
            # actively-retrieved rows -- the ones that actually
            # surface in real turns.
            rows_iter.sort(
                key=lambda m: (m.last_used_at or m.created_at or ""),
                reverse=True,
            )
            now = datetime.now(timezone.utc)
            rows: list[dict[str, Any]] = []
            cap = max(1, int(limit))
            for mem in rows_iter[:cap]:
                stored = float(getattr(mem, "confidence", 0.0) or 0.0)
                pinned = bool(getattr(mem, "pinned", False))
                created_at = getattr(mem, "created_at", None)
                age_days: float | None = None
                if created_at:
                    try:
                        created = datetime.fromisoformat(
                            str(created_at).replace("Z", "+00:00")
                        )
                        age_days = max(
                            0.0,
                            (now - created).total_seconds() / 86400.0,
                        )
                    except Exception:
                        age_days = None
                effective = (
                    _compute_effective_confidence(
                        stored,
                        age_days=age_days,
                        horizon_days=horizon_days,
                        floor=floor,
                    )
                    if age_days is not None
                    else stored
                )
                distant = _is_distant_memory(
                    stored_confidence=stored,
                    created_at=created_at,
                    now=now,
                    horizon_days=horizon_days,
                    floor=floor,
                    threshold=threshold,
                    pinned=pinned,
                )
                rows.append(
                    {
                        "id": int(mem.id),
                        "kind": mem.kind,
                        "tier": getattr(mem, "tier", "long_term"),
                        "pinned": pinned,
                        "stored_confidence": round(stored, 4),
                        "age_days": (
                            round(age_days, 2) if age_days is not None else None
                        ),
                        "effective_confidence": round(float(effective), 4),
                        "distant": bool(distant and enabled),
                        "uncertain": stored < 0.5,
                        "content_preview": (mem.content or "")[:80],
                    }
                )
            return json.dumps(
                {
                    "enabled": enabled,
                    "settings": {
                        "horizon_days": horizon_days,
                        "floor": floor,
                        "distant_threshold": threshold,
                    },
                    "rows": rows,
                    "total_rows": len(rows_iter),
                    "shown": len(rows),
                },
                indent=2,
            )
        except Exception as exc:
            return f"get_confidence_decay_state raised: {exc}"

    @mcp.tool()
    def force_seed_onboarding_goal() -> str:
        """K1 follow-up — re-seed the curated "get to know" goal.

        Bypasses the ``goals.onboarding_goal_seeded`` kv_meta gate
        and re-runs the seed. Useful for end-to-end testing the
        prompt placement + reflection cadence without nuking
        ``data/chat_sessions.db``. Cosine dedupe in
        :class:`MemoryStore` may collapse the second insert into
        the existing row (returns ``None``); the kv_meta flag
        stays set in that case.

        Returns JSON with the seeded memory id + summary preview,
        or an explanatory message if the seed was a no-op.
        """
        try:
            mem = session._seed_onboarding_goal_if_first_time(force=True)
        except Exception as exc:
            return f"force_seed_onboarding_goal raised: {exc}"
        if mem is None:
            return json.dumps(
                {
                    "fired": False,
                    "reason": (
                        "add_goal returned None — likely cosine dedupe "
                        "against an existing goal, or no_embedder. The "
                        "kv_meta flag is set anyway to prevent retries."
                    ),
                },
            )
        return json.dumps(
            {
                "fired": True,
                "memory_id": int(getattr(mem, "id", -1) or -1),
                "pinned": bool(getattr(mem, "pinned", False)),
                "source": (getattr(mem, "metadata", {}) or {}).get(
                    "source",
                ),
                "summary_preview": str(
                    getattr(mem, "content", "")
                )[:200],
            },
            indent=2,
        )

    # ── Resources ────────────────────────────────────────────────────

    @mcp.resource("assistant://history")
    def get_history() -> str:
        """Recent conversation messages (most recent 40)."""
        try:
            rows = session._chat_db.get_messages(session.session_key, limit=40)
            entries = [
                {"role": r.role, "content": r.content[:500], "created_at": str(r.created_at)}
                for r in rows
            ]
            return json.dumps(entries, indent=2, default=str)
        except Exception as exc:
            return f"Error reading history: {exc}"

    @mcp.resource("assistant://config")
    def get_config() -> str:
        """Current assistant configuration snapshot."""
        s = session._settings
        info = {
            "model": session.effective_chat_model,
            "base_url": s.ollama.base_url,
            "temperature": s.ollama.temperature,
            "context_window": session.context_window_size,
            "tts_provider": s.tts.provider,
            "tts_voice": s.tts.voice,
            "tts_enabled": s.tts.enabled,
            "stt_model": s.stt.model,
            "stt_language": s.stt.language,
            "mcp_server_port": s.mcp_server.port,
        }
        return json.dumps(info, indent=2, default=str)

    log.info("MCP server created (lean v1)")
    return mcp
