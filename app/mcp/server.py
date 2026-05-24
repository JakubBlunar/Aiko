"""FastMCP debug server for the lean Aiko app.

Exposes a small surface for Cursor / VSCode MCP clients to drive the running
session: send messages, inspect status, clear history, peek at the latest
metrics. Browser-snapshot and agent-tool tools from the legacy build are
gone -- v1 has no agent tools yet.
"""
from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP


if TYPE_CHECKING:
    from app.core.session_controller import SessionController


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
        """Return JSON list of agent tools (empty in v1, hooked in via TurnRunner later)."""
        return json.dumps([], indent=2)

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
            from app.core import circadian as _circ
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
