"""FastAPI app: REST + WebSocket bridge between the React UI and the SessionController.

Design notes
============
- The browser owns *no* audio. TTS plays through pocket-tts -> sounddevice in
  Python; STT captures from the system mic in Python. The websocket only
  carries text events (tokens, transcripts, state).
- One websocket = one user. We broadcast every assistant event to all
  connected sockets so multiple tabs stay in sync.
- The chat call is synchronous on a worker thread: the UI POSTs ``chat`` over
  the websocket and we run :meth:`SessionController.chat_once_streaming` in a
  thread, forwarding tokens as they arrive.
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, File, HTTPException, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles


if TYPE_CHECKING:
    from app.core.session_controller import SessionController

from app.core.live_session import LiveSession
from app.core.persona_manager import PersonaError


log = logging.getLogger("app.web.server")


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DIST_DIR = _PROJECT_ROOT / "web" / "dist"
_PERSONA_DIR = _PROJECT_ROOT / "data" / "persona"
_PERSONAS_ROOT = _PROJECT_ROOT / "data" / "personas"

# Maximum upload size for a Live2D model zip (matches the in-zip uncompressed
# cap inside PersonaManager).
_MAX_PERSONA_UPLOAD_BYTES = 200 * 1024 * 1024  # 200 MB compressed


# ── Connection registry ────────────────────────────────────────────────


class _Hub:
    """Thread-safe registry of active websockets with broadcast helpers."""

    def __init__(self) -> None:
        self._sockets: set[WebSocket] = set()
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, ws: WebSocket) -> None:
        with self._lock:
            self._sockets.add(ws)

    def discard(self, ws: WebSocket) -> None:
        with self._lock:
            self._sockets.discard(ws)

    def snapshot(self) -> list[WebSocket]:
        with self._lock:
            return list(self._sockets)

    def broadcast(self, message: dict[str, Any]) -> None:
        """Schedule a broadcast onto the asyncio loop from any thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            asyncio.run_coroutine_threadsafe(self._broadcast_async(message), loop)
        except Exception:
            log.debug("broadcast scheduling failed", exc_info=True)

    async def _broadcast_async(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, default=str)
        for ws in self.snapshot():
            try:
                await ws.send_text(payload)
            except Exception:
                self.discard(ws)


# ── App factory ────────────────────────────────────────────────────────


def create_web_app(session: "SessionController") -> FastAPI:
    """Build the FastAPI app wired to ``session``.

    Side effect: subscribes a message + TTS-state listener on the controller
    so any assistant event (UI- or MCP-triggered) reaches every connected
    websocket.
    """
    app = FastAPI(title="Aiko Web", version="0.2.0")

    # Vite dev server runs on :5173; production bundle is served by us.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
        allow_methods=["*"],
        allow_headers=["*"],
        allow_credentials=True,
    )

    hub = _Hub()

    # ── SessionController -> hub bridges ────────────────────────────

    def _on_message(speaker: str, text: str) -> None:
        # Map the legacy "You / Assistant / You (MCP)" speaker convention to
        # an explicit role so the React store doesn't have to guess.
        lowered = (speaker or "").strip().lower()
        kind: str | None = None
        if lowered.startswith("you"):
            role = "user"
        elif lowered.startswith("assistant"):
            role = "assistant"
            if "proactive" in lowered:
                kind = "proactive"
        else:
            role = "system"
        payload: dict[str, Any] = {
            "type": "message",
            "role": role,
            "speaker": speaker,
            "content": text,
        }
        if kind is not None:
            payload["kind"] = kind
        hub.broadcast(payload)

    def _on_tts_state(event: str, **payload: Any) -> None:
        hub.broadcast({"type": "tts_state", "event": event, **payload})

    def _on_metrics_updated(snapshot: dict[str, Any]) -> None:
        hub.broadcast({"type": "metrics_update", "metrics": snapshot})

    def _on_mood_state(payload: dict[str, Any]) -> None:
        hub.broadcast({"type": "mood_state", **payload})

    def _on_backchannel(hint: str, partial: str) -> None:
        hub.broadcast({
            "type": "backchannel",
            "hint": hint,
            "partial": partial,
        })

    def _broadcast_context_window() -> None:
        hub.broadcast({
            "type": "context_window",
            "context_window": session.context_window_size,
            "context_source": session.context_window_source,
            "model": session.effective_chat_model,
        })

    # Throttle amplitude broadcasts to <=30 Hz so we don't drown the WS.
    _amp_state: dict[str, float] = {"last_sent": 0.0, "last_level": 0.0}
    _AMP_INTERVAL = 1.0 / 30.0

    def _on_amplitude(level: float) -> None:
        import time as _time
        now = _time.monotonic()
        last_sent = _amp_state["last_sent"]
        last_level = _amp_state["last_level"]
        # Always emit zero immediately so the mouth closes when speech ends.
        if level == 0.0 and last_level == 0.0:
            return
        if level != 0.0 and (now - last_sent) < _AMP_INTERVAL:
            return
        _amp_state["last_sent"] = now
        _amp_state["last_level"] = level
        hub.broadcast({"type": "audio_amplitude", "level": float(level)})

    # Throttle partial broadcasts to <=5 Hz. Frontend renders a single
    # transient "Hearing: ..." line that updates in place — sub-200ms
    # updates would make it strobe.
    _partial_state: dict[str, Any] = {"last_sent": 0.0, "last_text": ""}
    _PARTIAL_INTERVAL = 1.0 / 5.0

    def _on_stt_partial(text: str) -> None:
        import time as _time
        text = (text or "").strip()
        if not text:
            return
        now = _time.monotonic()
        if text == _partial_state["last_text"]:
            return
        if (now - _partial_state["last_sent"]) < _PARTIAL_INTERVAL:
            return
        _partial_state["last_sent"] = now
        _partial_state["last_text"] = text
        hub.broadcast({"type": "stt_partial_live", "text": text})

    session.add_message_listener(_on_message)
    session.add_tts_state_listener(_on_tts_state)
    try:
        session.add_metrics_listener(_on_metrics_updated)
    except Exception:
        log.debug("metrics listener subscription failed", exc_info=True)
    try:
        session.add_mood_state_listener(_on_mood_state)
    except Exception:
        log.debug("mood state listener subscription failed", exc_info=True)
    try:
        session.add_backchannel_listener(_on_backchannel)
    except Exception:
        log.debug("backchannel listener subscription failed", exc_info=True)
    try:
        session.add_tts_amplitude_listener(_on_amplitude)
    except Exception:
        log.debug("amplitude listener subscription failed", exc_info=True)
    try:
        session.add_stt_partial_listener(_on_stt_partial)
    except Exception:
        log.debug("stt partial listener subscription failed", exc_info=True)

    def _on_memory_added(memory: Any) -> None:
        try:
            payload = memory.to_dict() if hasattr(memory, "to_dict") else dict(memory)
        except Exception:
            return
        hub.broadcast({"type": "memory_added", "memory": payload})

    try:
        session.add_memory_listener(_on_memory_added)
    except Exception:
        log.debug("memory listener subscription failed", exc_info=True)

    def _on_tool_event(event: str, payload: dict[str, Any]) -> None:
        # Tool calls and results stream out as a small event so the UI can
        # show "Aiko is checking the time / searching the web / recalling
        # your notebook..." indicators while the model decides.
        try:
            hub.broadcast({"type": "tool_event", "event": event, "payload": dict(payload)})
        except Exception:
            log.debug("tool event broadcast failed", exc_info=True)

    try:
        session.add_tool_event_listener(_on_tool_event)
    except Exception:
        log.debug("tool event listener subscription failed", exc_info=True)

    # ── Live (continuous voice) session ─────────────────────────────
    # One global instance per backend; SessionController already serializes
    # mic/STT access so a single loop is the right shape.

    def _on_live_event(name: str, payload: dict[str, Any]) -> None:
        # Translate LiveSession callback events into WS frames.
        if name == "voice_state":
            hub.broadcast({"type": "voice_state", "state": payload.get("state", "off")})
        elif name == "audio_level":
            hub.broadcast({"type": "audio_level", "level": payload.get("level", 0.0)})
        elif name == "stt_partial":
            text = str(payload.get("text") or "")
            if text:
                hub.broadcast({"type": "stt_partial", "text": text})
                # Route through the SessionController so the backchannel
                # classifier + scheduler urgent-cancel hooks both fire.
                try:
                    session.feed_stt_partial(text)
                except Exception:
                    log.debug("feed_stt_partial failed", exc_info=True)
        elif name == "stt_final":
            text = str(payload.get("text") or "").strip()
            if not text:
                return
            # Two parallel surfaces: the chat list (via _notify_message) and
            # the live "you said: ..." subtitle pill (via stt_final). The
            # store's lastTranscript drives the pill; the chat bubble comes
            # from the message event.
            hub.broadcast({"type": "stt_final", "text": text})
            session._notify_message("You (voice)", text)
        elif name == "token":
            chunk = payload.get("chunk", "")
            if chunk:
                hub.broadcast({"type": "token", "chunk": chunk})
        elif name == "turn_done":
            hub.broadcast({
                "type": "turn_done",
                "metrics": payload.get("metrics", {}),
            })
        elif name == "error":
            hub.broadcast({
                "type": "error",
                "message": str(payload.get("message", "voice error")),
            })

    live_session = LiveSession(session, _on_live_event)

    # ── REST: sessions ──────────────────────────────────────────────

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
        return JSONResponse([
            {"role": r.role, "content": r.content, "created_at": r.created_at}
            for r in rows
        ])

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
                "microphone_device": session.microphone_device,
                "output_device": session.output_device,
                "vad_level_threshold": session.vad_level_threshold,
                "vad_silence_seconds": session.vad_silence_seconds,
                "barge_in_enabled": session.barge_in_enabled(),
            },
            "proactive": {
                "silence_seconds": float(getattr(s.agent, "proactive_silence_seconds", 45.0)),
                "cooldown_seconds": float(getattr(s.agent, "proactive_cooldown_seconds", 120.0)),
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
                "available": list(session.available_tool_names()),
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
        tts = payload.get("tts") or {}
        if "voice" in tts:
            session.set_tts_voice(str(tts["voice"]))
        if "enabled" in tts:
            session._settings.tts.enabled = bool(tts["enabled"])
            session._tts.set_enabled(bool(tts["enabled"]))
        audio = payload.get("audio") or {}
        if "microphone_device" in audio:
            mic = audio["microphone_device"]
            session.set_microphone_device(int(mic) if mic is not None else None)
        if "output_device" in audio:
            out = audio["output_device"]
            session.set_output_device(int(out) if out is not None else None)
        if "vad_level_threshold" in audio:
            session.set_vad_level_threshold(float(audio["vad_level_threshold"]))
        if "vad_silence_seconds" in audio:
            session.set_vad_silence_seconds(float(audio["vad_silence_seconds"]))
        if "barge_in_enabled" in audio:
            session.set_barge_in_enabled(bool(audio["barge_in_enabled"]))
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
        tools = payload.get("tools") or {}
        if tools:
            tcfg = session._settings.tools
            for key in ("enabled", "get_time", "recall", "web_search"):
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
        return get_settings()

    @app.get("/api/models")
    def list_models(refresh: bool = False) -> JSONResponse:
        return JSONResponse(session.list_chat_models(refresh=refresh))

    @app.get("/api/voices")
    def list_voices() -> JSONResponse:
        return JSONResponse(session.list_tts_voices())

    @app.get("/api/audio/devices")
    def list_audio_devices() -> JSONResponse:
        return JSONResponse({
            "input": [{"index": i, "name": n} for i, n in session.list_microphone_devices()],
            "output": [{"index": i, "name": n} for i, n in session.list_output_devices()],
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

    @app.get("/api/memories")
    def list_memories(limit: int = 50, order: str = "recent") -> JSONResponse:
        clamped = max(1, min(int(limit), 200))
        order_norm = "top" if str(order).strip().lower() == "top" else "recent"
        items = session.list_memories(limit=clamped, order=order_norm)
        return JSONResponse({
            "memories": items,
            "count": len(items),
            "enabled": session.memory_store is not None,
        })

    @app.delete("/api/memories/{memory_id}")
    def delete_memory(memory_id: int) -> JSONResponse:
        ok = session.delete_memory(int(memory_id))
        if not ok:
            raise HTTPException(404, "memory not found")
        hub.broadcast({"type": "memory_deleted", "id": int(memory_id)})
        return JSONResponse({"deleted": int(memory_id)})

    # ── REST: Live2D persona (avatar) ───────────────────────────────

    def _persona_payload() -> dict[str, Any] | None:
        manifest = session.persona_manager.current()
        return manifest.to_dict() if manifest else None

    @app.get("/api/persona")
    def get_persona() -> JSONResponse:
        return JSONResponse({"persona": _persona_payload()})

    @app.post("/api/persona/upload")
    async def upload_persona(file: UploadFile = File(...)) -> JSONResponse:
        if not file.filename:
            raise HTTPException(400, "missing filename")
        lowered = file.filename.lower()
        if not lowered.endswith(".zip"):
            raise HTTPException(400, "expected a .zip file")
        body = await file.read()
        if len(body) == 0:
            raise HTTPException(400, "uploaded file is empty")
        if len(body) > _MAX_PERSONA_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"upload too large (limit {_MAX_PERSONA_UPLOAD_BYTES // (1024 * 1024)} MB)",
            )
        import io as _io
        buffer = _io.BytesIO(body)
        try:
            display = Path(file.filename).stem
            manifest = session.persona_manager.install_from_zip(
                buffer,
                display_name=display or "Persona",
            )
        except PersonaError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            log.exception("persona upload failed")
            raise HTTPException(500, f"upload failed: {exc}") from exc
        payload = manifest.to_dict()
        hub.broadcast({"type": "persona_changed", "persona": payload})
        return JSONResponse({"persona": payload})

    @app.delete("/api/persona")
    def delete_persona() -> JSONResponse:
        removed = session.persona_manager.delete()
        hub.broadcast({"type": "persona_changed", "persona": None})
        return JSONResponse({"removed": removed})

    @app.patch("/api/persona/mapping")
    async def patch_persona_mapping(payload: dict[str, Any]) -> JSONResponse:
        reaction_mapping = payload.get("reaction_mapping")
        if reaction_mapping is not None and not isinstance(reaction_mapping, dict):
            raise HTTPException(400, "reaction_mapping must be an object")
        idle = payload.get("idle_motion_group")
        talk = payload.get("talk_motion_group")
        scale = payload.get("scale_multiplier")
        if scale is not None:
            try:
                scale = float(scale)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    400, "scale_multiplier must be a number",
                ) from exc
        manifest = session.persona_manager.update_mapping(
            reaction_mapping=(
                {str(k): str(v) for k, v in reaction_mapping.items()}
                if reaction_mapping
                else None
            ),
            idle_motion_group=str(idle) if idle is not None else None,
            talk_motion_group=str(talk) if talk is not None else None,
            scale_multiplier=scale,
        )
        if manifest is None:
            raise HTTPException(404, "no active persona")
        out = manifest.to_dict()
        hub.broadcast({"type": "persona_changed", "persona": out})
        return JSONResponse({"persona": out})

    # ── REST: documents (RAG corpus) ────────────────────────────────

    _MAX_DOCUMENT_UPLOAD_BYTES = 16 * 1024 * 1024  # 16 MB

    @app.get("/api/documents")
    def list_documents() -> JSONResponse:
        ingestor = session.document_ingestor
        if ingestor is None:
            raise HTTPException(503, "RAG document store unavailable")
        return JSONResponse({"documents": ingestor.list_documents()})

    @app.post("/api/documents/upload")
    async def upload_document(file: UploadFile = File(...)) -> JSONResponse:
        ingestor = session.document_ingestor
        if ingestor is None:
            raise HTTPException(503, "RAG document store unavailable")
        if not file.filename:
            raise HTTPException(400, "missing filename")
        body = await file.read()
        if len(body) == 0:
            raise HTTPException(400, "uploaded file is empty")
        if len(body) > _MAX_DOCUMENT_UPLOAD_BYTES:
            raise HTTPException(
                413,
                f"upload too large (limit {_MAX_DOCUMENT_UPLOAD_BYTES // (1024 * 1024)} MB)",
            )
        try:
            result = ingestor.ingest(filename=file.filename, data=body)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            log.exception("document ingestion failed")
            raise HTTPException(500, f"ingestion failed: {exc}") from exc
        return JSONResponse({
            "document": {
                "document_id": result.document_id,
                "title": result.title,
                "chunk_count": result.chunk_count,
                "bytes_indexed": result.bytes_indexed,
            },
            "documents": ingestor.list_documents(),
        })

    @app.delete("/api/documents/{document_id}")
    def delete_document(document_id: str) -> JSONResponse:
        ingestor = session.document_ingestor
        if ingestor is None:
            raise HTTPException(503, "RAG document store unavailable")
        ok = ingestor.delete_document(document_id)
        if not ok:
            raise HTTPException(404, "document not found")
        return JSONResponse({"deleted": document_id, "documents": ingestor.list_documents()})

    # ── Persona / static assets ─────────────────────────────────────

    # Always mount /personas so the active model is reachable as soon as it's
    # uploaded. The directory is created up-front so StaticFiles doesn't 500.
    _PERSONAS_ROOT.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/personas",
        StaticFiles(directory=str(_PERSONAS_ROOT), check_dir=False),
        name="personas",
    )

    if _PERSONA_DIR.exists():
        app.mount("/persona", StaticFiles(directory=str(_PERSONA_DIR)), name="persona")

    if _DIST_DIR.exists():
        app.mount(
            "/assets",
            StaticFiles(directory=str(_DIST_DIR / "assets")),
            name="assets",
        )

        @app.get("/")
        def root_index() -> FileResponse:
            return FileResponse(str(_DIST_DIR / "index.html"))

        # SPA fallback: every non-API GET returns index.html so React Router works.
        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith(("api/", "ws", "persona/", "personas/", "assets/", "live2d/")):
                raise HTTPException(404, "not found")
            target = _DIST_DIR / full_path
            if target.is_file():
                return FileResponse(str(target))
            return FileResponse(str(_DIST_DIR / "index.html"))
    else:

        @app.get("/")
        def root_dev_hint() -> JSONResponse:
            return JSONResponse({
                "message": (
                    "React bundle not built yet. Run 'npm run dev' inside web/ "
                    "(http://localhost:5173) or 'npm run build' to generate web/dist."
                ),
                "dist_dir": str(_DIST_DIR),
            })

    # ── WebSocket ───────────────────────────────────────────────────

    @app.on_event("startup")
    async def _startup() -> None:
        hub.attach_loop(asyncio.get_running_loop())

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        hub.add(ws)

        # On connect, prime the client with current state.
        try:
            await ws.send_text(json.dumps({
                "type": "hello",
                "session": session.session_key,
                "model": session.effective_chat_model,
                "tts_enabled": bool(session._settings.tts.enabled),
                "voice_active": bool(live_session.is_active),
                "context_window": session.context_window_size,
                "context_source": session.context_window_source,
            }))
        except Exception:
            pass

        active_turn: threading.Event | None = None

        try:
            while True:
                raw = await ws.receive_text()
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue

                msg_type = str(msg.get("type") or "").lower()

                if msg_type == "chat":
                    text = str(msg.get("text") or "").strip()
                    if not text:
                        continue
                    if active_turn is not None and not active_turn.is_set():
                        await ws.send_text(json.dumps({
                            "type": "error",
                            "message": "A turn is already in progress; send 'stop' first.",
                        }))
                        continue
                    active_turn = threading.Event()
                    _spawn_chat_turn(session, hub, text, active_turn)

                elif msg_type == "stop":
                    if active_turn is not None:
                        active_turn.set()
                    try:
                        session._turn_runner.request_stop()
                        session.stop_tts()
                    except Exception:
                        log.debug("stop request failed", exc_info=True)

                elif msg_type == "switch_session":
                    sid = str(msg.get("session_id") or "").strip()
                    if sid:
                        if ":" in sid:
                            sid = sid.split(":", 1)[1]
                        session.switch_session(sid)
                        hub.broadcast({"type": "session_changed", "session": session.session_key})
                        _broadcast_context_window()

                elif msg_type == "new_session":
                    session.new_session()
                    hub.broadcast({"type": "session_changed", "session": session.session_key})
                    _broadcast_context_window()

                elif msg_type == "clear":
                    session.clear_conversation_memory()
                    hub.broadcast({"type": "history_cleared", "session": session.session_key})

                elif msg_type == "voice_start":
                    if not live_session.is_active:
                        live_session.start()
                    else:
                        # Re-broadcast current state so a reconnected client
                        # can re-sync its UI.
                        hub.broadcast({"type": "voice_state", "state": "listening"})

                elif msg_type == "voice_stop":
                    live_session.stop()

                elif msg_type == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))

        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("websocket loop crashed")
        finally:
            hub.discard(ws)

    return app


def _spawn_chat_turn(
    session: "SessionController",
    hub: _Hub,
    text: str,
    done_event: threading.Event,
) -> None:
    """Run a chat turn on a worker thread, streaming tokens via the hub."""

    def _run() -> None:
        try:
            session._notify_message("You", text)

            def on_token(chunk: str) -> None:
                if chunk:
                    hub.broadcast({"type": "token", "chunk": chunk})

            def on_status(status: str) -> None:
                hub.broadcast({"type": "status", "message": status})

            def stop_requested() -> bool:
                return done_event.is_set()

            reply = session.chat_once_streaming(
                user_text=text,
                on_token=on_token,
                on_generation_status=on_status,
                stop_requested=stop_requested,
                mode="typed",
            )
            session._notify_message("Assistant", reply or "")
            hub.broadcast({
                "type": "turn_done",
                "metrics": session.get_last_metrics(),
            })
        except Exception as exc:
            log.exception("chat turn failed")
            hub.broadcast({"type": "error", "message": str(exc)})
        finally:
            done_event.set()

    threading.Thread(target=_run, daemon=True, name="web-chat-turn").start()
