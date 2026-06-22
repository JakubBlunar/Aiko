from __future__ import annotations

import json
import logging
import time
from typing import Any
from fastapi import HTTPException
from fastapi.responses import JSONResponse
from app.core.infra import crash_logging
from app.core.infra.settings import _parse_grounding_line_mode, persist_user_overrides
from app.web.server import _classify_test_error, _search_public_snapshot


log = logging.getLogger("app.web.server")


def register(app, session, hub, _broadcast_context_window, live_session) -> None:
    """REST routes: sessions settings routes."""
    @app.get("/api/sessions")
    def list_sessions() -> JSONResponse:
        rows = session._chat_db.list_sessions()
        active = session.session_key
        return JSONResponse({
            "active": active,
            "sessions": rows,
        })

    @app.post("/api/sessions/new")
    def new_session() -> JSONResponse:
        new_id = session.new_session()
        hub.broadcast({"type": "session_changed", "session": session.session_key})
        _broadcast_context_window()
        return JSONResponse({"session_id": new_id, "session_key": session.session_key})

    @app.post("/api/sessions/switch")
    async def switch_session(payload: dict[str, str]) -> JSONResponse:
        session_id = (payload.get("session_id") or "").strip()
        if not session_id:
            raise HTTPException(400, "missing session_id")
        # ``session_id`` from list_sessions is the full key (``user:id``).
        # Strip the user prefix if present so switch_session stores just the id.
        if ":" in session_id:
            session_id = session_id.split(":", 1)[1]
        session.switch_session(session_id)
        hub.broadcast({"type": "session_changed", "session": session.session_key})
        _broadcast_context_window()
        return JSONResponse({"session_key": session.session_key})

    @app.delete("/api/sessions/{session_id}")
    def delete_session(session_id: str) -> JSONResponse:
        session._chat_db.delete_session(session_id)
        return JSONResponse({"deleted": session_id})

    @app.post("/api/sessions/clear")
    def clear_active() -> JSONResponse:
        session.clear_conversation_memory()
        hub.broadcast({"type": "history_cleared", "session": session.session_key})
        return JSONResponse({"cleared": session.session_key})

    @app.get("/api/sessions/{session_id}/messages")
    def session_messages(session_id: str, limit: int = 200) -> JSONResponse:
        rows = session._chat_db.get_messages(session_id, limit=max(1, min(limit, 1000)))

        def _json_or_none(raw: str | None) -> Any:
            # Reactions / gestures persist as JSON strings; decode so the
            # client can restore the reaction counters + gesture badges on
            # a history reload. Bad/empty JSON degrades to None silently.
            if not raw:
                return None
            try:
                return json.loads(raw)
            except (ValueError, TypeError):
                return None

        # ``id`` is included for the schema-v7 "mark as moment" action;
        # ``reactions`` / ``gestures`` (schema v15, K31/K32) are included so
        # the counters + badges survive a reload. Callers that don't need
        # them can ignore the fields.
        return JSONResponse([
            {
                "id": int(r.id),
                "role": r.role,
                "content": r.content,
                "created_at": r.created_at,
                "reactions": _json_or_none(r.reactions),
                "gestures": _json_or_none(r.gestures),
                "attachments": _json_or_none(r.attachments),
            }
            for r in rows
        ])

    # ── REST: health (Tauri sidecar bootstrap probe) ────────────────

    @app.get("/api/health")
    def get_health() -> JSONResponse:
        """Cheap liveness probe used by the Tauri shell.

        Polled by the macOS Tauri sidecar before opening the webview to
        know when the spawned Python backend has finished booting. Kept
        intentionally trivial so it can answer before the heavier
        services finish warming up.
        """
        return JSONResponse({"ok": True, "session_key": session.session_key})

    # ── REST: identity (first-run onboarding) ───────────────────────

    @app.get("/api/settings/identity")
    def get_identity() -> JSONResponse:
        """Return the configured display name + onboarding flag.

        ``needs_onboarding`` is true exactly when ``user_display_name``
        is empty/unset -- the React shell uses it to decide whether to
        show the first-run name modal.
        """
        return JSONResponse({
            "user_display_name": (
                session._settings.assistant.user_display_name or ""
            ),
            "needs_onboarding": bool(session.needs_onboarding),
        })

    @app.put("/api/settings/identity")
    def put_identity(payload: dict[str, Any]) -> JSONResponse:
        """Persist a new user display name.

        Validates 1-32 chars after strip. Broadcasts ``identity_changed``
        so workers and other browser windows reconcile their cached
        prompt strings.
        """
        raw_name = payload.get("user_display_name", "")
        try:
            stored = session.update_user_display_name(str(raw_name))
        except ValueError as exc:
            return JSONResponse(
                {"error": str(exc)}, status_code=400,
            )
        hub.broadcast({
            "type": "identity_changed",
            "user_display_name": stored,
            "needs_onboarding": False,
        })
        return JSONResponse({
            "user_display_name": stored,
            "needs_onboarding": False,
        })

    # ── REST: settings / models / voices / devices ──────────────────

    @app.get("/api/settings")
    def get_settings() -> JSONResponse:
        s = session._settings
        return JSONResponse({
            "chat": {
                "model": session.effective_chat_model,
                "context_window": session.context_window_size,
                "temperature": float(s.ollama.temperature),
                "max_tokens": int(s.chat_llm.max_tokens),
            },
            # Provider routing snapshot. The raw API key is intentionally
            # NOT echoed back — only ``has_api_key`` so the UI knows
            # whether to prefill the password input with a •••• placeholder
            # or leave it empty.
            "chat_llm": session._chat_llm_public_snapshot(),
            "tts": {
                "provider": session.tts_provider,
                "voice": session.tts_voice,
                "enabled": bool(s.tts.enabled),
            },
            "stt": {
                "model": session.stt_model,
                "language": s.stt.language,
            },
            "audio": {
                "vad_level_threshold": session.vad_level_threshold,
                "vad_silence_seconds": session.vad_silence_seconds,
                "barge_in_enabled": session.barge_in_enabled(),
                "earcons_enabled": bool(
                    getattr(s.audio, "earcons_enabled", True),
                ),
            },
            "proactive": {
                "silence_seconds": float(getattr(s.agent, "proactive_silence_seconds", 45.0)),
                "cooldown_seconds": float(getattr(s.agent, "proactive_cooldown_seconds", 120.0)),
                "typed_enabled": bool(getattr(s.agent, "proactive_typed_enabled", True)),
                "silence_seconds_typed": float(
                    getattr(s.agent, "proactive_silence_seconds_typed", 240.0),
                ),
                "cooldown_seconds_typed": float(
                    getattr(s.agent, "proactive_cooldown_seconds_typed", 600.0),
                ),
                "typed_when_away": bool(
                    getattr(s.agent, "proactive_typed_when_away", False),
                ),
            },
            "activity": {
                # Surfaced as a top-level block (not under ``proactive``)
                # because it's a distinct privacy-critical opt-in. The
                # frontend watches this flag to start/stop the activity
                # reporter polling loop.
                "awareness_enabled": bool(
                    getattr(s.agent, "activity_awareness_enabled", False),
                ),
            },
            "shared_moments": {
                "enabled": bool(getattr(s.agent, "shared_moments_enabled", True)),
                "llm_enabled": bool(
                    getattr(s.agent, "shared_moments_llm_enabled", True),
                ),
                "min_turn_gap": int(
                    getattr(s.agent, "shared_moments_min_turn_gap", 5),
                ),
                "cooldown_seconds": float(
                    getattr(s.agent, "shared_moments_cooldown_seconds", 300.0),
                ),
            },
            "anniversary": {
                "surfacing_enabled": bool(
                    getattr(s.agent, "anniversary_surfacing_enabled", True),
                ),
            },
            "relationship_axes": {
                "enabled": bool(
                    getattr(s.agent, "relationship_axes_enabled", True),
                ),
            },
            # Companion-feel knobs that previously lived only in
            # config.json: proactive room/gift nudges (world_notice_*),
            # the K16 ambient grounding-line mode, and the K31/K32
            # soft-physicality switches (touch / user reactions / persona
            # banner). Grouped under one block the Settings drawer + the
            # persona window both read from.
            "companion": {
                "world_notice_enabled": bool(
                    getattr(s.agent, "world_notice_enabled", True),
                ),
                "world_notice_interval_seconds": int(
                    getattr(
                        getattr(s, "memory", None),
                        "world_notice_interval_seconds",
                        300,
                    ),
                ),
                "world_notice_cooldown_seconds": int(
                    getattr(
                        getattr(s, "memory", None),
                        "world_notice_cooldown_seconds",
                        3600,
                    ),
                ),
                "world_notice_daily_cap": int(
                    getattr(
                        getattr(s, "memory", None),
                        "world_notice_daily_cap",
                        4,
                    ),
                ),
                "world_notice_ttl_seconds": int(
                    getattr(
                        getattr(s, "memory", None),
                        "world_notice_ttl_seconds",
                        1800,
                    ),
                ),
                "grounding_line_mode": str(
                    getattr(s.agent, "grounding_line_mode", "off"),
                ),
                "touch_enabled": bool(
                    getattr(s.agent, "touch_enabled", True),
                ),
                "user_reactions_enabled": bool(
                    getattr(s.agent, "user_reactions_enabled", True),
                ),
                "persona_touch_banner_enabled": bool(
                    getattr(s.agent, "persona_touch_banner_enabled", True),
                ),
                "persona_touch_banner_duration_seconds": int(
                    getattr(
                        s.agent, "persona_touch_banner_duration_seconds", 20,
                    ),
                ),
                # K60 tsundere expression mask dial.
                "expression_mask": str(
                    getattr(s.agent, "expression_mask", "off"),
                ),
            },
            "endpointing": {
                "enabled": bool(getattr(s.endpointing, "enabled", True)),
                "use_partial_transcript": bool(
                    getattr(s.endpointing, "use_partial_transcript", True)
                ),
                "phrase_silence_seconds": float(
                    getattr(s.endpointing, "phrase_silence_seconds", 1.0)
                ),
                "turn_silence_seconds": float(
                    getattr(s.endpointing, "turn_silence_seconds", 3.0)
                ),
                "fast_close_silence_seconds": float(
                    getattr(s.endpointing, "fast_close_silence_seconds", 0.6)
                ),
                "hesitation_extend_to_turn": bool(
                    getattr(s.endpointing, "hesitation_extend_to_turn", True)
                ),
                "barge_in_min_speech_seconds": float(
                    getattr(s.endpointing, "barge_in_min_speech_seconds", 0.7)
                ),
            },
            "tools": {
                "enabled": bool(getattr(s.tools, "enabled", True)),
                "get_time": bool(getattr(s.tools, "get_time", True)),
                "recall": bool(getattr(s.tools, "recall", True)),
                "web_search": bool(getattr(s.tools, "web_search", True)),
                "world": bool(getattr(s.tools, "world", True)),
                "available": list(session.available_tool_names()),
            },
            # Web-search backend. The raw ``api_key`` is never echoed —
            # only ``has_api_key`` so the drawer can show a •••• filled
            # state. Writes to the key go through PUT
            # ``/api/settings/search-credentials``.
            "search": _search_public_snapshot(getattr(s, "search", None)),
            "logging": {
                # Mirror of LoggingSettings.ui_log_enabled and friends so
                # the Settings drawer's Debug-logging toggle has a single
                # source of truth. Only the UI-bridge knobs are exposed —
                # the file/level knobs stay backend-only because flipping
                # them mid-session would require re-initialising handlers.
                "ui_log_enabled": bool(getattr(s.logging, "ui_log_enabled", False)),
                "ui_log_categories": list(getattr(s.logging, "ui_log_categories", [])),
                "ui_log_max_batch": int(getattr(s.logging, "ui_log_max_batch", 50)),
                "ui_log_max_payload_bytes": int(
                    getattr(s.logging, "ui_log_max_payload_bytes", 2048),
                ),
            },
            "voice_active": bool(live_session.is_active),
            "session_key": session.session_key,
        })

    @app.patch("/api/settings")
    async def patch_settings(payload: dict[str, Any]) -> JSONResponse:
        # Accepts a partial settings doc and applies only the keys present.
        chat = payload.get("chat") or {}
        if "model" in chat:
            session.set_chat_model(str(chat["model"]))
            hub.broadcast({"type": "model_changed", "model": session.effective_chat_model})
            _broadcast_context_window()
        chat_llm_patch = payload.get("chat_llm") or {}
        if chat_llm_patch:
            # Safety net: never accept an API key through the generic
            # PATCH endpoint. ``PUT /api/settings/llm-credentials`` is
            # the dedicated write-only path so a misclick in another
            # form field can't leak credentials in browser tooling.
            chat_llm_patch.pop("api_key", None)
            try:
                snapshot = session.reconfigure_chat_llm(chat_llm_patch)
                hub.broadcast({
                    "type": "llm_settings_changed",
                    "chat_llm": snapshot,
                })
                hub.broadcast({
                    "type": "model_changed",
                    "model": session.effective_chat_model,
                })
                _broadcast_context_window()
            except Exception as exc:
                log.warning("reconfigure_chat_llm failed: %s", exc, exc_info=True)
                raise HTTPException(
                    400, f"chat_llm reconfigure failed: {exc}",
                )
        search_patch = payload.get("search") or {}
        if search_patch:
            # Safety net: never accept the search API key through the
            # generic PATCH. ``PUT /api/settings/search-credentials`` is
            # the dedicated write-only path.
            search_patch.pop("api_key", None)
            try:
                snapshot = session.reconfigure_search(search_patch)
                hub.broadcast({
                    "type": "search_settings_changed",
                    "search": snapshot,
                })
            except Exception as exc:
                log.warning("reconfigure_search failed: %s", exc, exc_info=True)
                raise HTTPException(
                    400, f"search reconfigure failed: {exc}",
                )
        tts = payload.get("tts") or {}
        if "voice" in tts:
            session.set_tts_voice(str(tts["voice"]))
        if "enabled" in tts:
            session._settings.tts.enabled = bool(tts["enabled"])
            session._tts.set_enabled(bool(tts["enabled"]))
        audio = payload.get("audio") or {}
        if "vad_level_threshold" in audio:
            session.set_vad_level_threshold(float(audio["vad_level_threshold"]))
        if "vad_silence_seconds" in audio:
            session.set_vad_silence_seconds(float(audio["vad_silence_seconds"]))
        if "barge_in_enabled" in audio:
            session.set_barge_in_enabled(bool(audio["barge_in_enabled"]))
        if "earcons_enabled" in audio:
            earcons_on = bool(audio["earcons_enabled"])
            session._settings.audio.earcons_enabled = earcons_on
            try:
                session._earcons.enabled = earcons_on
            except Exception:
                log.debug("earcons enable toggle failed", exc_info=True)
            try:
                persist_user_overrides({"audio": {"earcons_enabled": earcons_on}})
            except Exception:
                log.debug("persist earcons override failed", exc_info=True)
        proactive = payload.get("proactive") or {}
        if "silence_seconds" in proactive:
            try:
                value = max(10.0, float(proactive["silence_seconds"]))
            except (TypeError, ValueError):
                value = 45.0
            session._settings.agent.proactive_silence_seconds = value
        if "cooldown_seconds" in proactive:
            try:
                value = max(30.0, float(proactive["cooldown_seconds"]))
            except (TypeError, ValueError):
                value = 120.0
            session._settings.agent.proactive_cooldown_seconds = value
            try:
                session._proactive.update_runtime(cooldown_seconds=value)
            except Exception:
                log.debug("proactive update_runtime failed", exc_info=True)
        if "typed_enabled" in proactive:
            session._settings.agent.proactive_typed_enabled = bool(
                proactive["typed_enabled"]
            )
        if "silence_seconds_typed" in proactive:
            try:
                value = max(60.0, float(proactive["silence_seconds_typed"]))
            except (TypeError, ValueError):
                value = 240.0
            session._settings.agent.proactive_silence_seconds_typed = value
        if "cooldown_seconds_typed" in proactive:
            try:
                value = max(120.0, float(proactive["cooldown_seconds_typed"]))
            except (TypeError, ValueError):
                value = 600.0
            session._settings.agent.proactive_cooldown_seconds_typed = value
            try:
                session._proactive.update_runtime(cooldown_seconds_typed=value)
            except Exception:
                log.debug(
                    "proactive update_runtime (typed) failed", exc_info=True,
                )
        if "typed_when_away" in proactive:
            session._settings.agent.proactive_typed_when_away = bool(
                proactive["typed_when_away"]
            )
        activity = payload.get("activity") or {}
        if "awareness_enabled" in activity:
            new_value = bool(activity["awareness_enabled"])
            session._settings.agent.activity_awareness_enabled = new_value
            # Privacy hygiene: when the user disables the toggle, drop
            # any cached active-app string so a next-prompt build won't
            # surface a stale "<user> is in <App>" line. ``set_user_active_app``
            # already short-circuits on the disabled gate, but we also
            # null the cached field directly for completeness.
            if not new_value:
                try:
                    session.set_user_active_app(None)
                except Exception:
                    log.debug(
                        "clearing user_active_app failed", exc_info=True,
                    )
        shared_moments_cfg = payload.get("shared_moments") or {}
        if "enabled" in shared_moments_cfg:
            session._settings.agent.shared_moments_enabled = bool(
                shared_moments_cfg["enabled"]
            )
        if "llm_enabled" in shared_moments_cfg:
            session._settings.agent.shared_moments_llm_enabled = bool(
                shared_moments_cfg["llm_enabled"]
            )
        if "min_turn_gap" in shared_moments_cfg:
            try:
                value = max(1, int(shared_moments_cfg["min_turn_gap"]))
            except (TypeError, ValueError):
                value = 5
            session._settings.agent.shared_moments_min_turn_gap = value
            try:
                if session._moment_detector is not None:
                    session._moment_detector.update_runtime(min_turn_gap=value)
            except Exception:
                log.debug("moment detector update_runtime failed", exc_info=True)
        if "cooldown_seconds" in shared_moments_cfg:
            try:
                value = max(
                    30.0, float(shared_moments_cfg["cooldown_seconds"]),
                )
            except (TypeError, ValueError):
                value = 300.0
            session._settings.agent.shared_moments_cooldown_seconds = value
            try:
                if session._moment_detector is not None:
                    session._moment_detector.update_runtime(
                        cooldown_seconds=value,
                    )
            except Exception:
                log.debug("moment detector cooldown update failed", exc_info=True)
        anniversary_cfg = payload.get("anniversary") or {}
        if "surfacing_enabled" in anniversary_cfg:
            session._settings.agent.anniversary_surfacing_enabled = bool(
                anniversary_cfg["surfacing_enabled"]
            )
        axes_cfg = payload.get("relationship_axes") or {}
        if "enabled" in axes_cfg:
            session._settings.agent.relationship_axes_enabled = bool(
                axes_cfg["enabled"]
            )
        tools = payload.get("tools") or {}
        if tools:
            tcfg = session._settings.tools
            for key in ("enabled", "get_time", "recall", "web_search", "world"):
                if key in tools:
                    setattr(tcfg, key, bool(tools[key]))
            try:
                session.rebuild_tool_registry()
            except Exception:
                log.debug("rebuild_tool_registry failed", exc_info=True)
        endpointing_cfg = payload.get("endpointing") or {}
        if endpointing_cfg:
            ecfg = session._settings.endpointing
            if "enabled" in endpointing_cfg:
                ecfg.enabled = bool(endpointing_cfg["enabled"])
            if "use_partial_transcript" in endpointing_cfg:
                ecfg.use_partial_transcript = bool(
                    endpointing_cfg["use_partial_transcript"]
                )
            if "hesitation_extend_to_turn" in endpointing_cfg:
                ecfg.hesitation_extend_to_turn = bool(
                    endpointing_cfg["hesitation_extend_to_turn"]
                )
            if "phrase_silence_seconds" in endpointing_cfg:
                try:
                    ecfg.phrase_silence_seconds = max(
                        0.2, float(endpointing_cfg["phrase_silence_seconds"])
                    )
                except (TypeError, ValueError):
                    pass
            if "turn_silence_seconds" in endpointing_cfg:
                try:
                    ecfg.turn_silence_seconds = max(
                        0.4, float(endpointing_cfg["turn_silence_seconds"])
                    )
                except (TypeError, ValueError):
                    pass
            if "fast_close_silence_seconds" in endpointing_cfg:
                try:
                    ecfg.fast_close_silence_seconds = max(
                        0.1, float(endpointing_cfg["fast_close_silence_seconds"])
                    )
                except (TypeError, ValueError):
                    pass
            if "barge_in_min_speech_seconds" in endpointing_cfg:
                try:
                    ecfg.barge_in_min_speech_seconds = max(
                        0.0, float(endpointing_cfg["barge_in_min_speech_seconds"])
                    )
                except (TypeError, ValueError):
                    pass
        logging_cfg = payload.get("logging") or {}
        if logging_cfg:
            # Only the UI-bridge knobs are mutable at runtime; the file
            # path / level switches stay frozen because re-initialising
            # the rotating handler mid-session is messy. Broadcast the
            # change so any other connected tab flips its toggle too.
            lcfg = session._settings.logging
            changed = False
            if "ui_log_enabled" in logging_cfg:
                lcfg.ui_log_enabled = bool(logging_cfg["ui_log_enabled"])
                changed = True
            if "ui_log_categories" in logging_cfg:
                raw_cats = logging_cfg.get("ui_log_categories") or []
                if isinstance(raw_cats, (list, tuple)):
                    lcfg.ui_log_categories = [
                        str(token).strip().lower()
                        for token in raw_cats
                        if str(token).strip()
                    ]
                    changed = True
            if "ui_log_max_batch" in logging_cfg:
                try:
                    lcfg.ui_log_max_batch = max(
                        1, min(500, int(logging_cfg["ui_log_max_batch"])),
                    )
                    changed = True
                except (TypeError, ValueError):
                    pass
            if "ui_log_max_payload_bytes" in logging_cfg:
                try:
                    lcfg.ui_log_max_payload_bytes = max(
                        256,
                        min(64 * 1024, int(logging_cfg["ui_log_max_payload_bytes"])),
                    )
                    changed = True
                except (TypeError, ValueError):
                    pass
            if changed:
                hub.broadcast({
                    "type": "logging_settings_changed",
                    "logging": {
                        "ui_log_enabled": bool(lcfg.ui_log_enabled),
                        "ui_log_categories": list(lcfg.ui_log_categories),
                        "ui_log_max_batch": int(lcfg.ui_log_max_batch),
                        "ui_log_max_payload_bytes": int(lcfg.ui_log_max_payload_bytes),
                    },
                })
        companion = payload.get("companion") or {}
        if companion:
            agent = session._settings.agent
            mem = session._settings.memory
            # Build the persistence patch as we go so each knob survives
            # a restart (matching avatar/identity durability). Empty
            # sub-dicts are pruned before writing.
            persist_patch: dict[str, Any] = {"agent": {}, "memory": {}}
            if "world_notice_enabled" in companion:
                v = bool(companion["world_notice_enabled"])
                agent.world_notice_enabled = v
                persist_patch["agent"]["world_notice_enabled"] = v
            # world_notice_* cadence lives on MemorySettings; clamp to the
            # same floors load_settings applies.
            for key, floor, default in (
                ("world_notice_interval_seconds", 30, 300),
                ("world_notice_cooldown_seconds", 0, 3600),
                ("world_notice_daily_cap", 0, 4),
                ("world_notice_ttl_seconds", 60, 1800),
            ):
                if key in companion:
                    try:
                        v = max(floor, int(companion[key]))
                    except (TypeError, ValueError):
                        v = default
                    setattr(mem, key, v)
                    persist_patch["memory"][key] = v
            if "grounding_line_mode" in companion:
                mode = _parse_grounding_line_mode(companion["grounding_line_mode"])
                agent.grounding_line_mode = mode
                try:
                    session._prompt_assembler.set_grounding_line_mode(mode)
                except Exception:
                    log.debug("set_grounding_line_mode failed", exc_info=True)
                persist_patch["agent"]["grounding_line_mode"] = mode
            for flag in (
                "touch_enabled",
                "user_reactions_enabled",
                "persona_touch_banner_enabled",
            ):
                if flag in companion:
                    v = bool(companion[flag])
                    setattr(agent, flag, v)
                    persist_patch["agent"][flag] = v
            if "persona_touch_banner_duration_seconds" in companion:
                try:
                    v = max(
                        1,
                        min(
                            120,
                            int(companion["persona_touch_banner_duration_seconds"]),
                        ),
                    )
                except (TypeError, ValueError):
                    v = 20
                agent.persona_touch_banner_duration_seconds = v
                persist_patch["agent"]["persona_touch_banner_duration_seconds"] = v
            if "expression_mask" in companion:
                from app.core.affect.expression_mask import normalize_mode

                mode = normalize_mode(companion["expression_mask"])
                agent.expression_mask = mode
                persist_patch["agent"]["expression_mask"] = mode
            persist_patch = {k: v for k, v in persist_patch.items() if v}
            if persist_patch:
                try:
                    persist_user_overrides(persist_patch)
                except Exception:
                    log.debug("persist companion overrides failed", exc_info=True)
                # Broadcast so other windows (notably the persona overlay,
                # which reads the touch-banner flags) reconcile live.
                hub.broadcast({
                    "type": "companion_settings_changed",
                    "companion": {
                        "world_notice_enabled": bool(
                            getattr(agent, "world_notice_enabled", True),
                        ),
                        "world_notice_interval_seconds": int(
                            getattr(mem, "world_notice_interval_seconds", 300),
                        ),
                        "world_notice_cooldown_seconds": int(
                            getattr(mem, "world_notice_cooldown_seconds", 3600),
                        ),
                        "world_notice_daily_cap": int(
                            getattr(mem, "world_notice_daily_cap", 4),
                        ),
                        "world_notice_ttl_seconds": int(
                            getattr(mem, "world_notice_ttl_seconds", 1800),
                        ),
                        "grounding_line_mode": str(
                            getattr(agent, "grounding_line_mode", "off"),
                        ),
                        "touch_enabled": bool(
                            getattr(agent, "touch_enabled", True),
                        ),
                        "user_reactions_enabled": bool(
                            getattr(agent, "user_reactions_enabled", True),
                        ),
                        "persona_touch_banner_enabled": bool(
                            getattr(agent, "persona_touch_banner_enabled", True),
                        ),
                        "persona_touch_banner_duration_seconds": int(
                            getattr(
                                agent,
                                "persona_touch_banner_duration_seconds",
                                20,
                            ),
                        ),
                        "expression_mask": str(
                            getattr(agent, "expression_mask", "off"),
                        ),
                    },
                })
        return get_settings()

    @app.get("/api/models")
    def list_models(
        refresh: bool = False, provider: str | None = None,
    ) -> JSONResponse:
        # ``provider`` (optional) lets the React drawer preview the
        # model list of a non-active provider before the user commits
        # to it. Empty / missing -> active provider, cached.
        if provider:
            return JSONResponse(
                session.list_chat_models(provider=provider),
            )
        return JSONResponse(session.list_chat_models(refresh=refresh))

    # ── REST: LLM provider config (chat_llm) ────────────────────────

    @app.get("/api/llm/presets")
    def get_llm_presets() -> JSONResponse:
        """Return the curated provider preset catalogue.

        Read-only. Includes ``base_url`` / recommended models / free-tier
        labels per preset so the UI can render self-documenting cards
        without re-encoding the same strings on the client.
        """
        return JSONResponse({"presets": session.provider_presets()})

    @app.put("/api/settings/llm-credentials")
    async def put_llm_credentials(payload: dict[str, Any]) -> JSONResponse:
        """Persist provider credentials + URL in one write-only call.

        Body accepts ``{api_key, api_key_env, base_url, extra_headers}``.
        Mirrors :func:`put_identity`'s shape. Validates that ``base_url``
        (if present) starts with ``http://`` or ``https://`` and that
        the API key is whitespace-free (so a stray copy-paste newline
        can't trip later requests). Returns the masked snapshot.
        """
        patch: dict[str, Any] = {}
        if "api_key" in payload:
            raw_key = str(payload.get("api_key", "") or "")
            if raw_key and any(c.isspace() for c in raw_key):
                raise HTTPException(
                    400,
                    "api_key must not contain whitespace",
                )
            patch["api_key"] = raw_key.strip()
        if "api_key_env" in payload:
            patch["api_key_env"] = str(
                payload.get("api_key_env", "") or "",
            ).strip()
        if "base_url" in payload:
            raw_url = str(payload.get("base_url", "") or "").strip()
            if raw_url and not (
                raw_url.startswith("http://")
                or raw_url.startswith("https://")
            ):
                raise HTTPException(
                    400,
                    "base_url must start with http:// or https://",
                )
            patch["base_url"] = raw_url
        if "extra_headers" in payload:
            raw_headers = payload.get("extra_headers") or {}
            if not isinstance(raw_headers, dict):
                raise HTTPException(
                    400, "extra_headers must be an object",
                )
            patch["extra_headers"] = raw_headers
        if not patch:
            return JSONResponse(session._chat_llm_public_snapshot())
        try:
            snapshot = session.reconfigure_chat_llm(patch)
        except Exception as exc:
            log.warning(
                "llm-credentials write failed: %s", exc, exc_info=True,
            )
            raise HTTPException(400, f"credentials write failed: {exc}")
        hub.broadcast({
            "type": "llm_settings_changed",
            "chat_llm": snapshot,
        })
        return JSONResponse(snapshot)

    @app.put("/api/settings/search-credentials")
    async def put_search_credentials(payload: dict[str, Any]) -> JSONResponse:
        """Persist the web-search (LangSearch) API key, write-only.

        Body accepts ``{api_key, api_key_env}``. The raw key is routed
        into the OS keychain by ``reconfigure_search``; only the masked
        snapshot (``has_api_key``) comes back.
        """
        patch: dict[str, Any] = {}
        if "api_key" in payload:
            raw_key = str(payload.get("api_key", "") or "")
            if raw_key and any(c.isspace() for c in raw_key):
                raise HTTPException(
                    400, "api_key must not contain whitespace",
                )
            patch["api_key"] = raw_key.strip()
        if "api_key_env" in payload:
            patch["api_key_env"] = str(
                payload.get("api_key_env", "") or "",
            ).strip()
        if not patch:
            return JSONResponse(
                _search_public_snapshot(getattr(session._settings, "search", None))
            )
        try:
            snapshot = session.reconfigure_search(patch)
        except Exception as exc:
            log.warning(
                "search-credentials write failed: %s", exc, exc_info=True,
            )
            raise HTTPException(400, f"credentials write failed: {exc}")
        hub.broadcast({
            "type": "search_settings_changed",
            "search": snapshot,
        })
        return JSONResponse(snapshot)

    @app.post("/api/llm/test-connection")
    async def post_test_llm_connection(
        payload: dict[str, Any],
    ) -> JSONResponse:
        """Verify a candidate provider config without persisting it.

        Issues a real one-token chat completion against the supplied
        ``{provider, base_url, api_key, model, extra_headers}`` using a
        throwaway :class:`app.llm.chat_client.ChatClient`. The
        controller's saved ``chat_llm`` is **never** touched — this is
        explicitly a dry run so the user can pre-flight Gemini before
        committing the key to disk.

        Returns 200 with a structured ``{success, ...}`` payload on
        both pass and fail so the UI can show a green check or a red
        banner with the provider's error message verbatim. The endpoint
        only returns 4xx when the request body itself is malformed.
        """
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        provider = str(payload.get("provider", "") or "").strip().lower()
        if provider not in {"ollama", "openai_compatible"}:
            raise HTTPException(
                400, "provider must be 'ollama' or 'openai_compatible'",
            )
        model = str(payload.get("model", "") or "").strip()
        if not model and provider == "openai_compatible":
            raise HTTPException(
                400, "model is required for openai_compatible",
            )
        base_url = str(payload.get("base_url", "") or "").strip()
        api_key = str(payload.get("api_key", "") or "").strip()
        raw_headers = payload.get("extra_headers") or {}
        if not isinstance(raw_headers, dict):
            raise HTTPException(400, "extra_headers must be an object")

        # Build a throwaway ChatLlmSettings + client. Reuses the
        # controller's existing factory so the test path can't drift
        # from the real path.
        from app.core.infra.settings import ChatLlmSettings
        from app.core.session.session_controller import (
            _build_chat_client,
        )

        probe_cfg = ChatLlmSettings(
            provider=provider,
            model=model,
            base_url=base_url,
            api_key=api_key,
            extra_headers={
                str(k).strip(): str(v).strip()
                for k, v in raw_headers.items()
                if str(k).strip() and v is not None
            },
            reasoning_effort=str(
                payload.get("reasoning_effort", "") or ""
            ).strip().lower(),
        )
        try:
            probe = _build_chat_client(
                chat_llm=probe_cfg,
                ollama_settings=session._settings.ollama,
                role="connection_test",
            )
        except Exception as exc:
            log.info("test-connection client build failed: %s", exc)
            return JSONResponse({
                "success": False,
                "latency_ms": 0,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model_resolved": model,
                "error_code": "bad_response",
                "error_message": str(exc)[:500],
            })

        # One-token chat ping. ``num_predict=1`` works on both clients;
        # ``surface="connection_test"`` shows up in the truncation
        # warning gate so a future grep over logs can find these calls.
        ping_messages = [{"role": "user", "content": "ping"}]
        ping_options: dict[str, object] = {"num_predict": 1}
        import time as _time

        t0 = _time.monotonic()
        try:
            response = probe.chat_with_tools(
                ping_messages,
                options=ping_options,
                model=model or None,
                surface="connection_test",
            )
            latency_ms = int((_time.monotonic() - t0) * 1000.0)
            usage = getattr(probe, "last_usage", None)
            return JSONResponse({
                "success": True,
                "latency_ms": latency_ms,
                "prompt_tokens": int(
                    getattr(usage, "prompt_tokens", 0) or 0,
                ),
                "completion_tokens": int(
                    getattr(usage, "completion_tokens", 0) or 0,
                ),
                "model_resolved": model,
                "error_code": None,
                "error_message": None,
                # Always include the ping content (trimmed) for the UI's
                # debug surface, even though success is determined by
                # the HTTP-level outcome rather than content shape.
                "content_preview": (response.content or "")[:80],
            })
        except Exception as exc:
            latency_ms = int((_time.monotonic() - t0) * 1000.0)
            error_code, error_message = _classify_test_error(exc)
            log.info(
                "test-connection failed: provider=%s model=%s code=%s "
                "elapsed_ms=%d msg=%s",
                provider, model, error_code, latency_ms, error_message,
            )
            return JSONResponse({
                "success": False,
                "latency_ms": latency_ms,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model_resolved": model,
                "error_code": error_code,
                "error_message": error_message,
            })

    # ── PR 2: provider catalogue + role-assignment REST surface ────
    #
    # New endpoints sit alongside the legacy /api/settings + /api/llm/presets +
    # /api/llm/test-connection ones (which keep working unchanged as back-compat
    # shims). The new catalogue is the eventual primary; the legacy block stays
    # readable / writable so downgrades and external scripts don't break.

    @app.get("/api/llm/providers")
    def get_llm_providers() -> JSONResponse:
        """List the saved provider catalogue with credentials masked.

        Each entry is a snapshot of :class:`LlmProvider` with the raw
        ``api_key`` replaced by ``has_api_key: bool``.
        """
        return JSONResponse({"providers": session.list_providers()})

    @app.post("/api/llm/providers")
    async def post_llm_provider(payload: dict[str, Any]) -> JSONResponse:
        """Create a new provider catalogue entry.

        Body: ``{template_id?: str, draft: {...}}``. ``template_id``
        seeds the entry from one of ``_PROVIDER_PRESETS``; ``draft``
        can override any field. Returns 409 when the id is taken.
        """
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        template_id = payload.get("template_id") or None
        draft = payload.get("draft") or {}
        if not isinstance(draft, dict):
            raise HTTPException(400, "draft must be an object")
        try:
            entry = session.add_provider(
                template_id=str(template_id) if template_id else None,
                draft=draft,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        hub.broadcast({
            "type": "llm_settings_changed",
            "providers": session.list_providers(),
            "routes": session.list_routes(),
        })
        return JSONResponse(entry)

    @app.patch("/api/llm/providers/{provider_id}")
    async def patch_llm_provider(
        provider_id: str, payload: dict[str, Any],
    ) -> JSONResponse:
        """Edit non-credential fields on a saved provider."""
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        # Safety net: credentials only flow through PUT
        # /api/llm/providers/{id}/credentials so an accidental PATCH
        # field can't leak through this surface.
        safe = {k: v for k, v in payload.items() if k not in ("api_key", "api_key_env")}
        try:
            entry = session.update_provider(provider_id, safe)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        hub.broadcast({
            "type": "llm_settings_changed",
            "providers": session.list_providers(),
            "routes": session.list_routes(),
        })
        return JSONResponse(entry)

    @app.put("/api/llm/providers/{provider_id}/credentials")
    async def put_llm_provider_credentials(
        provider_id: str, payload: dict[str, Any],
    ) -> JSONResponse:
        """Replace the api_key / api_key_env on a saved provider.

        Validates that the API key is whitespace-free (parallel to the
        legacy /api/settings/llm-credentials endpoint).
        """
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        creds: dict[str, Any] = {}
        if "api_key" in payload:
            raw_key = str(payload.get("api_key", "") or "")
            if raw_key and any(c.isspace() for c in raw_key):
                raise HTTPException(
                    400, "api_key must not contain whitespace",
                )
            creds["api_key"] = raw_key.strip()
        if "api_key_env" in payload:
            creds["api_key_env"] = str(
                payload.get("api_key_env", "") or "",
            ).strip()
        if not creds:
            raise HTTPException(400, "no credential fields supplied")
        try:
            entry = session.update_provider_credentials(provider_id, creds)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        hub.broadcast({
            "type": "llm_settings_changed",
            "providers": session.list_providers(),
            "routes": session.list_routes(),
        })
        return JSONResponse(entry)

    @app.delete("/api/llm/providers/{provider_id}")
    async def delete_llm_provider(provider_id: str) -> JSONResponse:
        """Delete a saved provider. 409 when still referenced by a route."""
        try:
            session.remove_provider(provider_id)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        hub.broadcast({
            "type": "llm_settings_changed",
            "providers": session.list_providers(),
            "routes": session.list_routes(),
        })
        return JSONResponse({"ok": True, "deleted": provider_id})

    @app.post("/api/llm/providers/{provider_id}/test")
    async def post_llm_provider_test(
        provider_id: str, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """Run a one-token probe against a saved provider.

        Body (all optional): ``{model?: str, context_window?: int}``.
        Returns the same shape as the legacy /api/llm/test-connection
        endpoint so the UI can reuse the green/red banner.
        """
        body = payload if isinstance(payload, dict) else {}
        override_model = body.get("model")
        override_ctx_raw = body.get("context_window")
        try:
            override_ctx = (
                int(override_ctx_raw)
                if override_ctx_raw not in (None, "", 0)
                else None
            )
        except (TypeError, ValueError):
            override_ctx = None
        try:
            result = session.test_provider(
                provider_id,
                override_model=(
                    str(override_model).strip()
                    if override_model is not None
                    else None
                ),
                override_context_window=override_ctx,
            )
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        return JSONResponse(result)

    @app.get("/api/llm/routes")
    def get_llm_routes() -> JSONResponse:
        """List all role assignments."""
        return JSONResponse({"routes": session.list_routes()})

    @app.patch("/api/llm/routes/{role}")
    async def patch_llm_route(
        role: str, payload: dict[str, Any],
    ) -> JSONResponse:
        """Set ``llm.routes[role]`` from a partial draft.

        For ``main_chat`` this cascades through the legacy
        :meth:`SessionController.reconfigure_chat_llm` path so the
        in-flight chat client + TurnRunner are rebuilt immediately.
        For other roles (currently only ``worker_default``) the route
        is recorded; a restart picks it up.
        """
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        try:
            updated = session.update_route(role, payload)
        except KeyError as exc:
            raise HTTPException(404, str(exc))
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        hub.broadcast({
            "type": "llm_settings_changed",
            "providers": session.list_providers(),
            "routes": session.list_routes(),
            # Echo the matching legacy snapshot so the existing UI
            # keeps working unchanged until the catalogue UI lands.
            "chat_llm": session._chat_llm_public_snapshot(),
        })
        return JSONResponse(updated)

    @app.get("/api/voices")
    def list_voices() -> JSONResponse:
        return JSONResponse(session.list_tts_voices())

    @app.post("/api/logs/ui")
    async def post_ui_logs(payload: dict[str, Any]) -> JSONResponse:
        """Receive batched UI debug events and merge them into ``app.log``.

        Body shape: ``{"entries": [{"ts": ..., "source": ..., "kind": ...,
        "payload": ...}, ...]}``. Returns ``403`` when the feature flag
        is off so a stale client can't keep writing without consent; the
        frontend treats 403 as "stop trying until the toggle flips back".
        Entries with a ``source`` outside ``ui_log_categories`` are
        silently dropped; the batch is capped at ``ui_log_max_batch``.
        """
        lcfg = session._settings.logging
        if not bool(getattr(lcfg, "ui_log_enabled", False)):
            raise HTTPException(403, "ui debug logging disabled")
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        raw_entries = payload.get("entries")
        if not isinstance(raw_entries, list):
            raise HTTPException(400, "entries must be a list")

        allowed_sources = {
            str(token).strip().lower()
            for token in getattr(lcfg, "ui_log_categories", []) or []
            if str(token).strip()
        }
        max_batch = max(1, int(getattr(lcfg, "ui_log_max_batch", 50)))
        max_payload = max(256, int(getattr(lcfg, "ui_log_max_payload_bytes", 2048)))

        accepted = 0
        dropped = 0
        for raw in raw_entries[:max_batch]:
            if not isinstance(raw, dict):
                dropped += 1
                continue
            source = str(raw.get("source") or "").strip().lower()
            if not source:
                dropped += 1
                continue
            # The allow-list matches by prefix (``channel.expression`` is
            # accepted when ``channel`` is on the list) so callers can
            # tag fine-grained sources without us maintaining the full
            # vocabulary here.
            if allowed_sources and not any(
                source == token or source.startswith(token + ".")
                for token in allowed_sources
            ):
                dropped += 1
                continue
            ok = crash_logging.log_ui_event(raw, max_payload_bytes=max_payload)
            if ok:
                accepted += 1
            else:
                dropped += 1
        overflow = max(0, len(raw_entries) - max_batch)
        return JSONResponse({
            "accepted": accepted,
            "dropped": dropped + overflow,
        })

    @app.get("/api/metrics")
    def metrics() -> JSONResponse:
        s = session._settings
        return JSONResponse({
            "last": session.get_last_metrics(),
            "average": session.get_average_metrics(),
            "config": {
                "model": session.effective_chat_model,
                "context_window": session.context_window_size,
                "context_source": session.context_window_source,
                "max_prompt_tokens_pct": float(getattr(s.agent, "max_prompt_tokens_pct", 0.8)),
                "summary_idle_seconds": float(getattr(s.agent, "summary_idle_seconds", 15.0)),
                "summary_min_unsummarized_messages": int(
                    getattr(s.agent, "summary_min_unsummarized_messages", 6),
                ),
                "summary_target_tokens": int(getattr(s.agent, "summary_target_tokens", 600)),
            },
        })

    # ── REST: long-term memories ────────────────────────────────────


