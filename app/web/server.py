"""FastAPI app: REST + WebSocket bridge between the React UI and the SessionController.

Design notes
============
- The **client** owns the audio interfaces. Each connected browser /
  Tauri webview opens its own ``getUserMedia`` mic and ``AudioContext``
  speaker; binary WS frames carry PCM in both directions (see
  ``app.web.audio_frames`` for the type table). JSON events still
  carry tokens, transcripts, state, etc.
- Only one client at a time is the **voice owner** — the one whose
  mic frames the server actually consumes. Every other connected
  client still receives Aiko's TTS audio, transcripts, and chat
  events so multiple tabs / devices stay in sync.
- The chat call is synchronous on a worker thread: the UI POSTs
  ``chat`` over the websocket and we run
  :meth:`SessionController.chat_once_streaming` in a thread,
  forwarding tokens as they arrive.
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

from app.core import crash_logging
from app.core.live_session import LiveSession
from app.core.settings import OUTFIT_MODES
from app.web import audio_frames as _frames


log = logging.getLogger("app.web.server")


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DIST_DIR = _PROJECT_ROOT / "web" / "dist"
# ``data/persona`` is unrelated to the Live2D pipeline — it stores the
# self-image text used by the inner-life prompt. We rename the static
# mount to ``/persona-text/`` so it doesn't collide with the new
# ``/avatar/`` mount and so future readers don't confuse the two.
_PERSONA_TEXT_DIR = _PROJECT_ROOT / "data" / "persona"


# ── Connection registry ────────────────────────────────────────────────


class _Hub:
    """Thread-safe registry of active websockets with broadcast helpers.

    Holds one entry per connection — each tagged with a ``client_id``
    so the voice-owner lock can target a single socket and so
    ``voice_owner_changed`` events identify who's holding the mic.
    Binary frames (TTS / earcon PCM, see :mod:`app.web.audio_frames`)
    are broadcast via :meth:`broadcast_bytes`; JSON state still goes
    through :meth:`broadcast`.
    """

    def __init__(self) -> None:
        # Map: WebSocket -> client_id. Sockets without an id (briefly,
        # during accept) are stored under ``None``.
        self._sockets: dict[WebSocket, str] = {}
        self._lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._voice_owner_id: str | None = None
        # Per-client visibility cache. The WS layer writes the latest
        # ``presence`` frame from each client here and folds the dict
        # via :meth:`any_client_visible` so the proactive director can
        # gate on "is *any* connected window visible right now?" rather
        # than racing on a single session-wide flag overwritten by
        # whichever client wrote last. Disconnects drop the entry so
        # closing every window flips the fold to ``False`` automatically.
        # Keys are ``client_id`` strings (not ``WebSocket`` objects) so
        # ``discard`` -- which already returns the freed id -- can clean
        # up without an extra back-pointer.
        self._visible_by_client: dict[str, bool] = {}

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def add(self, ws: WebSocket, client_id: str) -> None:
        with self._lock:
            self._sockets[ws] = client_id
            # Default to ``False`` until the client sends its first
            # ``presence`` frame. Defaulting to ``True`` would let a
            # stale-boot value override a correctly-reported ``False``
            # from another open window (and the React reporter sends
            # its initial frame eagerly on mount, so the gap is small).

            self._visible_by_client.setdefault(client_id, False)

    def discard(self, ws: WebSocket) -> str | None:
        """Remove ``ws``; if it owned the mic, return the freed id."""
        released: str | None = None
        with self._lock:
            client_id = self._sockets.pop(ws, None)
            if client_id is not None:
                self._visible_by_client.pop(client_id, None)
                if self._voice_owner_id == client_id:
                    released = client_id
                    self._voice_owner_id = None
        return released

    def set_client_presence(self, client_id: str, visible: bool) -> None:
        """Record the latest ``presence`` frame from ``client_id``.

        No-op for unknown ids (e.g. a disconnect raced the frame); the
        dict only grows on :meth:`add` so the cache can't leak across
        reconnects.
        """
        with self._lock:
            if client_id not in self._visible_by_client:
                return
            self._visible_by_client[client_id] = bool(visible)

    def any_client_visible(self) -> bool:
        """``True`` iff at least one connected client most-recently
        reported visible. Empty cache (no clients) returns ``False`` --
        no windows = no presence."""
        with self._lock:
            return any(self._visible_by_client.values())

    def snapshot(self) -> list[tuple[WebSocket, str]]:
        with self._lock:
            return list(self._sockets.items())

    @property
    def voice_owner_id(self) -> str | None:
        with self._lock:
            return self._voice_owner_id

    def claim_voice(self, client_id: str) -> tuple[bool, str | None]:
        """Try to give ``client_id`` mic ownership.

        Returns ``(claimed, previous_owner_id)``. A claim by the
        current owner is idempotent (claimed=True, previous=client_id).
        We allow takeover by default — the most-recent claimant wins
        so a user moving devices doesn't get stuck if they forget to
        release the mic on the other browser.
        """
        with self._lock:
            previous = self._voice_owner_id
            self._voice_owner_id = client_id
            return True, previous

    def release_voice(self, client_id: str) -> bool:
        """Release the lock if ``client_id`` is the current owner."""
        with self._lock:
            if self._voice_owner_id == client_id:
                self._voice_owner_id = None
                return True
            return False

    def _schedule(self, coro: Any) -> None:
        """Submit ``coro`` onto the hub's asyncio loop.

        Works from both on-loop (WS handlers calling broadcast as
        a synchronous side-effect) and off-loop (background worker
        threads — TTS, MCP) callers. The on-loop path uses
        ``loop.create_task`` directly to avoid the
        ``run_coroutine_threadsafe`` quirk where same-thread
        submissions can fail to dispatch under the FastAPI test
        client's portal.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        try:
            if running is loop:
                loop.create_task(coro)
            else:
                asyncio.run_coroutine_threadsafe(coro, loop)
        except Exception:
            log.debug("broadcast scheduling failed", exc_info=True)
            try:
                coro.close()
            except Exception:
                pass

    def broadcast(self, message: dict[str, Any]) -> None:
        """Schedule a JSON broadcast onto the asyncio loop from any thread."""
        self._schedule(self._broadcast_async(message))

    def broadcast_bytes(self, frame: bytes) -> None:
        """Schedule a binary-frame broadcast onto the asyncio loop.

        Used for TTS / earcon PCM streams. The frame must already be
        type-byte-prefixed (see :mod:`app.web.audio_frames`).
        """
        if not frame:
            return
        self._schedule(self._broadcast_bytes_async(frame))

    async def _broadcast_async(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, default=str)
        for ws, _cid in self.snapshot():
            try:
                await ws.send_text(payload)
            except Exception:
                self.discard(ws)

    async def _broadcast_bytes_async(self, frame: bytes) -> None:
        for ws, _cid in self.snapshot():
            try:
                await ws.send_bytes(frame)
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

    # Origins we accept cross-origin requests from:
    # - Vite dev server on :5173
    # - The Tauri 2 webview, which serves bundled assets from
    #   ``tauri://localhost`` on Windows / Linux and
    #   ``http://tauri.localhost`` on macOS. Both must be allowed for
    #   the desktop shell to reach the FastAPI backend.
    # In production (no Tauri, served by FastAPI), the React bundle is
    # same-origin and CORS doesn't apply.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "tauri://localhost",
            "https://tauri.localhost",
            "http://tauri.localhost",
        ],
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

    # Audio frames: TTS / earcon PCM flows from PocketTtsService /
    # EarconPlayer through SessionController, out to every connected
    # client as ``0x10`` / ``0x11`` binary frames. We bracket each
    # clip with ``audio_start`` / ``audio_end`` so the client knows
    # when to spin up / flush its scheduler.
    _stream_started: dict[str, bool] = {"tts": False, "earcon": False}

    def _on_audio_frame(stream: str, sample_rate: int, channels: int, pcm: bytes) -> None:
        if not pcm:
            return
        stream_byte = _frames.stream_byte(stream)
        if stream_byte == 0:
            return
        if not _stream_started.get(stream, False):
            hub.broadcast_bytes(
                _frames.build_audio_start(stream_byte, sample_rate, channels)
            )
            _stream_started[stream] = True
        if stream_byte == _frames.FRAME_TTS_PCM:
            hub.broadcast_bytes(_frames.build_tts_pcm(pcm))
        elif stream_byte == _frames.FRAME_EARCON_PCM:
            hub.broadcast_bytes(_frames.build_earcon_pcm(pcm))

    def _on_audio_frame_end(stream: str) -> None:
        stream_byte = _frames.stream_byte(stream)
        if stream_byte == 0:
            return
        if _stream_started.get(stream, False):
            hub.broadcast_bytes(_frames.build_audio_end(stream_byte))
            _stream_started[stream] = False

    try:
        session.set_audio_frame_listener(
            _on_audio_frame, end_listener=_on_audio_frame_end,
        )
    except Exception:
        log.debug("audio frame listener wiring failed", exc_info=True)

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

    # K2: belief CRUD listener bridge.
    def _on_belief_added(payload: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "belief_added", "belief": dict(payload)})
        except Exception:
            log.debug("belief_added broadcast failed", exc_info=True)

    def _on_belief_updated(payload: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "belief_updated", "belief": dict(payload)})
        except Exception:
            log.debug("belief_updated broadcast failed", exc_info=True)

    def _on_belief_deleted(payload: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "belief_deleted", **dict(payload)})
        except Exception:
            log.debug("belief_deleted broadcast failed", exc_info=True)

    try:
        session.add_belief_added_listener(_on_belief_added)
        session.add_belief_updated_listener(_on_belief_updated)
        session.add_belief_deleted_listener(_on_belief_deleted)
    except Exception:
        log.debug("belief listener subscription failed", exc_info=True)

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

    def _on_avatar_settings(settings_snapshot: dict[str, Any]) -> None:
        # Inline the resolved outfit + circadian period so the
        # frontend cross-fade reacts the *moment* the LLM emits
        # [[outfit:X]] (or the user flips auto_outfit), instead of
        # waiting for the next mood_state broadcast.
        hub.broadcast({
            "type": "avatar_settings_changed",
            "settings": dict(settings_snapshot),
            "resolved_outfit": session.resolve_auto_outfit(),
            "circadian_period": session.current_circadian_period(),
        })

    def _on_avatar_overlay(payload: dict[str, Any]) -> None:
        hub.broadcast({"type": "avatar_overlay", **payload})

    def _on_avatar_motion(payload: dict[str, Any]) -> None:
        hub.broadcast({"type": "avatar_motion", **payload})

    try:
        session.add_avatar_settings_listener(_on_avatar_settings)
    except Exception:
        log.debug("avatar settings listener subscription failed", exc_info=True)
    try:
        session.add_avatar_overlay_listener(_on_avatar_overlay)
    except Exception:
        log.debug("avatar overlay listener subscription failed", exc_info=True)
    try:
        session.add_avatar_motion_listener(_on_avatar_motion)
    except Exception:
        log.debug("avatar motion listener subscription failed", exc_info=True)

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
        # ``id`` is included for the schema-v7 "mark as moment" action;
        # callers that don't need it can ignore the field.
        return JSONResponse([
            {
                "id": int(r.id),
                "role": r.role,
                "content": r.content,
                "created_at": r.created_at,
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
        return get_settings()

    @app.get("/api/models")
    def list_models(refresh: bool = False) -> JSONResponse:
        return JSONResponse(session.list_chat_models(refresh=refresh))

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

    def _on_memory_updated(snapshot: dict[str, Any]) -> None:
        hub.broadcast({"type": "memory_updated", "memory": snapshot})

    try:
        session.add_memory_updated_listener(_on_memory_updated)
    except Exception:
        log.debug("memory updated listener subscription failed", exc_info=True)

    @app.get("/api/memories")
    def list_memories(
        limit: int = 50,
        order: str = "recent",
        offset: int = 0,
        kind: str | None = None,
        tier: str | None = None,
    ) -> JSONResponse:
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        order_norm = "top" if str(order).strip().lower() == "top" else "recent"
        kind_norm = (kind or "").strip().lower() or None
        tier_norm = (tier or "").strip().lower() or None
        items = session.list_memories(
            limit=clamped_limit,
            order=order_norm,
            offset=clamped_offset,
            kind=kind_norm,
            tier=tier_norm,
        )
        return JSONResponse({
            "memories": items,
            "count": len(items),
            "total": session.memory_count(kind=kind_norm, tier=tier_norm),
            "cap": session.memory_cap(),
            "enabled": session.memory_store is not None,
        })

    @app.get("/api/memories/counts")
    def memory_counts() -> JSONResponse:
        """Per-tier memory totals (schema v8). Drives the Memory tab header."""
        store = session.memory_store
        if store is None:
            return JSONResponse(
                {"scratchpad": 0, "long_term": 0, "archive": 0, "total": 0},
            )
        try:
            counts = store.count_by_tier()
        except Exception:
            counts = {"scratchpad": 0, "long_term": 0, "archive": 0, "total": 0}
        return JSONResponse(counts)

    @app.delete("/api/memories/{memory_id}")
    def delete_memory(memory_id: int) -> JSONResponse:
        ok = session.delete_memory(int(memory_id))
        if not ok:
            raise HTTPException(404, "memory not found")
        hub.broadcast({"type": "memory_deleted", "id": int(memory_id)})
        return JSONResponse({"deleted": int(memory_id)})

    @app.patch("/api/memories/{memory_id}")
    async def patch_memory(memory_id: int, payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        content = payload.get("content")
        kind = payload.get("kind")
        salience = payload.get("salience")
        tier = payload.get("tier")
        confidence = payload.get("confidence")
        if (
            content is None
            and kind is None
            and salience is None
            and tier is None
            and confidence is None
        ):
            raise HTTPException(
                400,
                "patch must include at least one of content, kind, salience, "
                "tier, confidence",
            )
        # Type-checks before reaching into the store: clearer than letting the
        # mutator silently coerce arbitrary input.
        if content is not None and not isinstance(content, str):
            raise HTTPException(400, "content must be a string")
        if kind is not None and not isinstance(kind, str):
            raise HTTPException(400, "kind must be a string")
        if salience is not None and not isinstance(salience, (int, float)):
            raise HTTPException(400, "salience must be a number")
        if tier is not None and not isinstance(tier, str):
            raise HTTPException(400, "tier must be a string")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise HTTPException(400, "confidence must be a number")
        try:
            updated = session.update_memory(
                int(memory_id),
                content=content,
                kind=kind,
                salience=float(salience) if salience is not None else None,
                tier=tier,
                confidence=float(confidence) if confidence is not None else None,
            )
        except Exception as exc:
            raise HTTPException(500, f"update failed: {exc}") from exc
        if updated is None:
            raise HTTPException(404, "memory not found")
        return JSONResponse({"memory": updated})

    @app.post("/api/memories")
    async def create_memory(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        content = payload.get("content")
        kind = payload.get("kind", "fact")
        salience = payload.get("salience", 0.6)
        tier = payload.get("tier", "long_term")
        confidence = payload.get("confidence")
        if not isinstance(content, str) or not content.strip():
            raise HTTPException(400, "content must be a non-empty string")
        if not isinstance(kind, str):
            raise HTTPException(400, "kind must be a string")
        if not isinstance(salience, (int, float)):
            raise HTTPException(400, "salience must be a number")
        if not isinstance(tier, str):
            raise HTTPException(400, "tier must be a string")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise HTTPException(400, "confidence must be a number")
        result = session.add_memory(
            content,
            kind=kind,
            salience=float(salience),
            tier=tier,
            confidence=float(confidence) if confidence is not None else None,
        )
        if result is None:
            raise HTTPException(503, "memory store unavailable or content too short")
        return JSONResponse(result)

    # ── REST: knowledge gaps (F2) ────────────────────────────────────

    @app.get("/api/knowledge-gaps")
    def list_knowledge_gaps(include_resolved: bool = False) -> JSONResponse:
        rows = session.list_knowledge_gaps(include_resolved=include_resolved)
        return JSONResponse({"gaps": rows, "total": len(rows)})

    @app.delete("/api/knowledge-gaps/{gap_id}")
    def delete_knowledge_gap(gap_id: int) -> JSONResponse:
        ok = session.delete_knowledge_gap(int(gap_id))
        if not ok:
            raise HTTPException(404, "knowledge gap not found")
        return JSONResponse({"deleted": int(gap_id)})

    @app.post("/api/knowledge-gaps/{gap_id}/resolve")
    async def resolve_knowledge_gap(
        gap_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        answer: str | None = None
        if isinstance(payload, dict):
            raw_answer = payload.get("answer")
            if raw_answer is not None and not isinstance(raw_answer, str):
                raise HTTPException(400, "answer must be a string")
            if isinstance(raw_answer, str):
                answer = raw_answer
        snapshot = session.resolve_knowledge_gap(int(gap_id), answer=answer)
        if snapshot is None:
            raise HTTPException(404, "knowledge gap not found")
        return JSONResponse({"gap": snapshot})

    # ── REST: curiosity seeds (K9) ───────────────────────────────────

    @app.post("/api/curiosity-seeds/run")
    async def run_curiosity_seed_worker() -> JSONResponse:
        """Force a single ``CuriositySeedWorker.run()`` and return the result.

        Used by the Memory tab "Regenerate now" button so a tester
        can verify the worker's output without waiting for the next
        idle window. Mirrors the cooperative shape of the other
        on-demand worker hooks: the call runs synchronously inside
        the request handler since the worker is already designed to
        be quick (one LLM call + a handful of embeds).
        """
        worker = getattr(session, "_curiosity_seed_worker", None)
        if worker is None:
            raise HTTPException(503, "curiosity seed worker unavailable")
        try:
            result = worker.run()
        except Exception as exc:
            raise HTTPException(500, f"worker run failed: {exc}") from exc
        return JSONResponse({"result": result or {}})

    # ── REST: memory conflicts (F5) ──────────────────────────────────

    @app.get("/api/memory-conflicts")
    def list_memory_conflicts(
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        include_recent: bool = True,
    ) -> JSONResponse:
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        status_norm = (status or "").strip().lower() or None
        snapshot = session.list_memory_conflicts(
            limit=clamped_limit,
            offset=clamped_offset,
            status=status_norm,
            include_recently_resolved=bool(include_recent),
        )
        return JSONResponse(snapshot)

    @app.post("/api/memory-conflicts/{pair_id}/resolve")
    async def resolve_memory_conflict(
        pair_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        winner_id = payload.get("winner_id")
        action = payload.get("action", "demote")
        if not isinstance(winner_id, int):
            raise HTTPException(400, "winner_id must be an integer")
        if not isinstance(action, str):
            raise HTTPException(400, "action must be a string")
        try:
            result = session.resolve_memory_conflict(
                int(pair_id),
                winner_id=int(winner_id),
                action=action,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if result is None:
            raise HTTPException(404, "conflict pair not found")
        hub.broadcast({
            "type": "memory_conflict_resolved",
            "pair_id": int(pair_id),
        })
        return JSONResponse(result)

    @app.post("/api/memory-conflicts/{pair_id}/dismiss")
    async def dismiss_memory_conflict(pair_id: int) -> JSONResponse:
        ok = session.dismiss_memory_conflict(int(pair_id))
        if not ok:
            raise HTTPException(404, "conflict pair not found")
        hub.broadcast({
            "type": "memory_conflict_dismissed",
            "pair_id": int(pair_id),
        })
        return JSONResponse({"dismissed": int(pair_id)})

    # ── REST: theory-of-mind beliefs (K2) ────────────────────────────

    @app.get("/api/beliefs")
    def list_beliefs(
        limit: int = 50,
        offset: int = 0,
        kind: str | None = None,
        status: str | None = None,
    ) -> JSONResponse:
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        kind_norm = (kind or "").strip().lower() or None
        status_norm = (status or "").strip().lower() or None
        snapshot = session.list_beliefs(
            limit=clamped_limit,
            offset=clamped_offset,
            kind=kind_norm,
            status=status_norm,
        )
        return JSONResponse(snapshot)

    @app.post("/api/beliefs")
    async def create_belief(
        payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kind = payload.get("kind")
        topic = payload.get("topic")
        state = payload.get("predicted_state")
        confidence = payload.get("confidence")
        if not isinstance(kind, str) or kind.strip().lower() not in ("mood", "opinion"):
            raise HTTPException(400, "kind must be 'mood' or 'opinion'")
        if not isinstance(topic, str) or not topic.strip():
            raise HTTPException(400, "topic must be a non-empty string")
        if not isinstance(state, str) or not state.strip():
            raise HTTPException(400, "predicted_state must be a non-empty string")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise HTTPException(400, "confidence must be a number")
        belief = session.add_belief(
            kind=kind.strip().lower(),
            topic=topic,
            predicted_state=state,
            confidence=float(confidence) if confidence is not None else None,
        )
        if belief is None:
            raise HTTPException(503, "belief tracking unavailable")
        return JSONResponse({"belief": belief})

    @app.patch("/api/beliefs/{belief_id}")
    async def patch_belief(
        belief_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        predicted_state = payload.get("predicted_state")
        confidence = payload.get("confidence")
        status = payload.get("status")
        if predicted_state is not None and not isinstance(predicted_state, str):
            raise HTTPException(400, "predicted_state must be a string")
        if confidence is not None and not isinstance(confidence, (int, float)):
            raise HTTPException(400, "confidence must be a number")
        if status is not None and not isinstance(status, str):
            raise HTTPException(400, "status must be a string")
        # Reject empty PATCH (mirrors PATCH /api/memories behaviour).
        if predicted_state is None and confidence is None and status is None:
            raise HTTPException(
                400, "expected at least one of predicted_state/confidence/status",
            )
        try:
            belief = session.update_belief(
                int(belief_id),
                predicted_state=predicted_state,
                confidence=float(confidence) if confidence is not None else None,
                status=status,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if belief is None:
            raise HTTPException(404, "belief not found")
        return JSONResponse({"belief": belief})

    @app.delete("/api/beliefs/{belief_id}")
    async def delete_belief(belief_id: int) -> JSONResponse:
        ok = session.delete_belief(int(belief_id))
        if not ok:
            raise HTTPException(404, "belief not found")
        return JSONResponse({"deleted": int(belief_id)})

    # ── REST: fact-checker status (F1) ───────────────────────────────

    @app.get("/api/fact-checker/status")
    def fact_checker_status() -> JSONResponse:
        snapshot = session.fact_checker_status()
        return JSONResponse(snapshot)

    @app.post("/api/memories/{memory_id}/pin")
    async def pin_memory(memory_id: int, payload: dict[str, Any] | None = None) -> JSONResponse:
        # ``pinned`` defaults to True (toggle-on); the editor sends an
        # explicit ``{pinned: false}`` to un-pin.
        target = True
        if isinstance(payload, dict) and "pinned" in payload:
            value = payload.get("pinned")
            if not isinstance(value, bool):
                raise HTTPException(400, "pinned must be a boolean")
            target = value
        updated = session.set_memory_pinned(int(memory_id), target)
        if updated is None:
            raise HTTPException(404, "memory not found")
        return JSONResponse({"memory": updated})

    # ── REST: Aiko's room (virtual world) ───────────────────────────

    def _on_world(patch: dict[str, Any]) -> None:
        # Single typed event broadcast over WS. The frontend reducer
        # surgically merges {state} / {location} / {item} /
        # {deleted_*_id} / {snapshot} into its store slice.
        try:
            hub.broadcast({"type": "world_updated", "patch": dict(patch)})
        except Exception:
            log.debug("world updated broadcast failed", exc_info=True)

    try:
        session.add_world_listener(_on_world)
    except Exception:
        log.debug("world listener subscription failed", exc_info=True)

    @app.get("/api/world")
    def get_world() -> JSONResponse:
        return JSONResponse(session.world_snapshot())

    @app.patch("/api/world/state")
    async def patch_world_state(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kwargs: dict[str, Any] = {}
        if "location_id" in payload:
            value = payload["location_id"]
            if value is not None and not isinstance(value, int):
                raise HTTPException(400, "location_id must be an integer or null")
            kwargs["location_id"] = value
        for field_name in ("posture", "activity", "mood_note"):
            if field_name in payload:
                value = payload[field_name]
                if value is not None and not isinstance(value, str):
                    raise HTTPException(400, f"{field_name} must be a string")
                kwargs[field_name] = value
        if not kwargs:
            raise HTTPException(
                400,
                "patch must include at least one of location_id, posture, activity, mood_note",
            )
        result = session.update_world_state(**kwargs)
        if result is None:
            raise HTTPException(503, "world store unavailable")
        return JSONResponse({"state": result})

    @app.post("/api/world/locations")
    async def create_world_location(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "name must be a non-empty string")
        slug = payload.get("slug")
        if slug is not None and not isinstance(slug, str):
            raise HTTPException(400, "slug must be a string")
        description = payload.get("description", "") or ""
        if not isinstance(description, str):
            raise HTTPException(400, "description must be a string")
        result = session.add_world_location(
            slug=slug if isinstance(slug, str) and slug.strip() else None,
            name=name,
            description=description,
        )
        if result is None:
            raise HTTPException(503, "world store unavailable")
        return JSONResponse({"location": result})

    @app.patch("/api/world/locations/{location_id}")
    async def patch_world_location(
        location_id: int, payload: dict[str, Any],
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kwargs: dict[str, Any] = {}
        for field_name in ("name", "description"):
            if field_name in payload:
                value = payload[field_name]
                if not isinstance(value, str):
                    raise HTTPException(400, f"{field_name} must be a string")
                kwargs[field_name] = value
        if "position" in payload:
            value = payload["position"]
            if not isinstance(value, int):
                raise HTTPException(400, "position must be an integer")
            kwargs["position"] = value
        if not kwargs:
            raise HTTPException(400, "patch must include at least one field")
        result = session.update_world_location(int(location_id), **kwargs)
        if result is None:
            raise HTTPException(404, "location not found")
        return JSONResponse({"location": result})

    @app.delete("/api/world/locations/{location_id}")
    def delete_world_location(location_id: int) -> JSONResponse:
        ok = session.delete_world_location(int(location_id))
        if not ok:
            raise HTTPException(404, "location not found")
        return JSONResponse({"deleted_location_id": int(location_id)})

    @app.post("/api/world/items")
    async def create_world_item(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        name = payload.get("name")
        if not isinstance(name, str) or not name.strip():
            raise HTTPException(400, "name must be a non-empty string")
        kind = payload.get("kind", "other")
        if not isinstance(kind, str):
            raise HTTPException(400, "kind must be a string")
        slug = payload.get("slug")
        if slug is not None and not isinstance(slug, str):
            raise HTTPException(400, "slug must be a string")
        description = payload.get("description", "") or ""
        if not isinstance(description, str):
            raise HTTPException(400, "description must be a string")
        location_id = payload.get("location_id")
        if location_id is not None and not isinstance(location_id, int):
            raise HTTPException(400, "location_id must be an integer or null")
        consumable = payload.get("consumable", False)
        if not isinstance(consumable, bool):
            raise HTTPException(400, "consumable must be a boolean")
        quantity = payload.get("quantity", 1)
        if not isinstance(quantity, int) or quantity < 1:
            raise HTTPException(400, "quantity must be a positive integer")
        state = payload.get("state")
        if state is not None and not isinstance(state, dict):
            raise HTTPException(400, "state must be an object or null")
        given_by = payload.get("given_by")
        if given_by is not None and not isinstance(given_by, str):
            raise HTTPException(400, "given_by must be a string")
        result = session.add_world_item(
            name=name,
            kind=kind,
            slug=slug if isinstance(slug, str) and slug.strip() else None,
            description=description,
            location_id=location_id,
            consumable=consumable,
            quantity=quantity,
            state=state,
            given_by=given_by,
        )
        if result is None:
            raise HTTPException(503, "world store unavailable")
        return JSONResponse({"item": result})

    @app.patch("/api/world/items/{item_id}")
    async def patch_world_item(
        item_id: int, payload: dict[str, Any],
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kwargs: dict[str, Any] = {}
        for field_name in ("name", "description", "kind"):
            if field_name in payload:
                value = payload[field_name]
                if not isinstance(value, str):
                    raise HTTPException(400, f"{field_name} must be a string")
                kwargs[field_name] = value
        if "location_id" in payload:
            value = payload["location_id"]
            if value is not None and not isinstance(value, int):
                raise HTTPException(400, "location_id must be an integer or null")
            kwargs["location_id"] = value
        if "quantity" in payload:
            value = payload["quantity"]
            if not isinstance(value, int) or value < 0:
                raise HTTPException(400, "quantity must be a non-negative integer")
            kwargs["quantity"] = value
        if "state" in payload:
            value = payload["state"]
            if not isinstance(value, dict):
                raise HTTPException(400, "state must be an object")
            kwargs["state"] = value
        if not kwargs:
            raise HTTPException(400, "patch must include at least one field")
        result = session.update_world_item(int(item_id), **kwargs)
        if result is None:
            raise HTTPException(404, "item not found")
        return JSONResponse({"item": result})

    @app.delete("/api/world/items/{item_id}")
    def delete_world_item(item_id: int) -> JSONResponse:
        ok = session.delete_world_item(int(item_id))
        if not ok:
            raise HTTPException(404, "item not found")
        return JSONResponse({"deleted_item_id": int(item_id)})

    @app.post("/api/world/items/{item_id}/consume")
    async def consume_world_item(
        item_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        amount = 1
        if isinstance(payload, dict) and "amount" in payload:
            value = payload["amount"]
            if not isinstance(value, int) or value < 1:
                raise HTTPException(400, "amount must be a positive integer")
            amount = value
        result = session.consume_world_item(int(item_id), amount=amount)
        if result is None:
            raise HTTPException(404, "item not found")
        return JSONResponse(result)

    @app.post("/api/world/seed")
    async def seed_world(force: bool = False) -> JSONResponse:
        result = session.reseed_world(force=bool(force))
        if result is None:
            raise HTTPException(503, "world store unavailable")
        return JSONResponse(result)

    # ── REST: Shared moments + Together (schema v7) ─────────────────

    def _on_shared_moment(patch: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "shared_moment_updated", "patch": dict(patch)})
        except Exception:
            log.debug("shared moment broadcast failed", exc_info=True)

    def _on_relationship_axes(state: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "relationship_axes_updated", "axes": dict(state)})
        except Exception:
            log.debug("axes broadcast failed", exc_info=True)

    try:
        session.add_shared_moment_listener(_on_shared_moment)
    except Exception:
        log.debug("shared moment listener subscribe failed", exc_info=True)
    try:
        session.add_relationship_axes_listener(_on_relationship_axes)
    except Exception:
        log.debug("axes listener subscribe failed", exc_info=True)

    def _on_knowledge_gap(patch: dict[str, Any]) -> None:
        try:
            hub.broadcast({"type": "knowledge_gap_updated", "patch": dict(patch)})
        except Exception:
            log.debug("knowledge gap broadcast failed", exc_info=True)

    try:
        session.add_knowledge_gap_listener(_on_knowledge_gap)
    except Exception:
        log.debug("knowledge gap listener subscribe failed", exc_info=True)

    @app.get("/api/together")
    def get_together() -> JSONResponse:
        return JSONResponse(session.get_together_summary())

    @app.get("/api/shared-moments")
    def list_shared_moments(
        offset: int = 0,
        limit: int = 20,
        vibe: str | None = None,
    ) -> JSONResponse:
        result = session.list_shared_moments(
            offset=max(0, int(offset)),
            limit=max(1, min(int(limit), 100)),
            vibe=vibe,
        )
        return JSONResponse(result)

    @app.post("/api/shared-moments")
    async def create_shared_moment(payload: dict[str, Any]) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        summary = payload.get("summary")
        if not isinstance(summary, str) or not summary.strip():
            raise HTTPException(400, "summary must be a non-empty string")
        vibe = payload.get("vibe", "general")
        if not isinstance(vibe, str):
            raise HTTPException(400, "vibe must be a string")
        when = payload.get("when")
        if when is not None and not isinstance(when, str):
            raise HTTPException(400, "when must be an ISO8601 string or null")
        result = session.add_shared_moment(
            summary=summary,
            vibe=vibe,
            when=when,
        )
        if result is None:
            raise HTTPException(503, "shared moments unavailable")
        return JSONResponse({"moment": result})

    @app.patch("/api/shared-moments/{moment_id}")
    async def patch_shared_moment(
        moment_id: int, payload: dict[str, Any],
    ) -> JSONResponse:
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        kwargs: dict[str, Any] = {}
        for field_name in ("summary", "vibe", "when"):
            if field_name in payload:
                value = payload[field_name]
                if value is not None and not isinstance(value, str):
                    raise HTTPException(400, f"{field_name} must be a string")
                kwargs[field_name] = value
        if "pinned" in payload:
            value = payload["pinned"]
            if not isinstance(value, bool):
                raise HTTPException(400, "pinned must be a boolean")
            kwargs["pinned"] = value
        if not kwargs:
            raise HTTPException(400, "patch must include at least one field")
        result = session.update_shared_moment(int(moment_id), **kwargs)
        if result is None:
            raise HTTPException(404, "shared moment not found")
        return JSONResponse({"moment": result})

    @app.delete("/api/shared-moments/{moment_id}")
    def delete_shared_moment(moment_id: int) -> JSONResponse:
        ok = session.delete_shared_moment(int(moment_id))
        if not ok:
            raise HTTPException(404, "shared moment not found")
        return JSONResponse({"deleted_moment_id": int(moment_id)})

    @app.post("/api/chat/messages/{message_id}/mark-moment")
    async def mark_message_as_moment(
        message_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        vibe = "general"
        if isinstance(payload, dict) and "vibe" in payload:
            value = payload["vibe"]
            if value is not None and not isinstance(value, str):
                raise HTTPException(400, "vibe must be a string")
            if isinstance(value, str) and value.strip():
                vibe = value
        result = session.mark_message_as_moment(int(message_id), vibe=vibe)
        if result is None:
            raise HTTPException(404, "message not found")
        return JSONResponse({"moment": result})

    # ── REST: Live2D avatar (fixed Alexia bundle) ───────────────────

    @app.get("/api/avatar")
    def get_avatar() -> JSONResponse:
        return JSONResponse({"avatar": session.avatar_payload()})

    @app.patch("/api/avatar")
    async def patch_avatar(payload: dict[str, Any]) -> JSONResponse:
        scale = payload.get("scale_multiplier")
        outfit = payload.get("auto_outfit")
        expressiveness = payload.get("expressiveness")
        if scale is not None:
            try:
                scale_value = float(scale)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    400, "scale_multiplier must be a number",
                ) from exc
            scale = scale_value
        if outfit is not None:
            outfit_normalized = str(outfit).strip().lower()
            if outfit_normalized not in OUTFIT_MODES:
                raise HTTPException(
                    400,
                    "auto_outfit must be one of: "
                    + ", ".join(sorted(OUTFIT_MODES)),
                )
            outfit = outfit_normalized
        if expressiveness is not None:
            try:
                expressiveness_value = float(expressiveness)
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    400, "expressiveness must be a number",
                ) from exc
            if expressiveness_value < 0.0 or expressiveness_value > 1.5:
                raise HTTPException(
                    400, "expressiveness must be between 0.0 and 1.5",
                )
            expressiveness = expressiveness_value
        snapshot = session.update_avatar_settings(
            scale_multiplier=scale,
            auto_outfit=outfit,
            expressiveness=expressiveness,
        )
        hub.broadcast({
            "type": "avatar_settings_changed",
            "settings": dict(snapshot),
        })
        return JSONResponse({"avatar": session.avatar_payload()})

    @app.get("/api/avatar/accessories")
    def get_avatar_accessories() -> JSONResponse:
        """Phase 4 (expression overhaul): per-accessory catalogue.

        Returns ``{accessories: [...], active_outfit: "..."}`` where
        each catalogue entry carries the current value, the rig's
        availability flag, and the outfit gate (if any). The
        SettingsDrawer renders one row per entry and disables rows
        whose ``allowed_outfits`` doesn't include the current
        ``active_outfit``.
        """
        return JSONResponse(session.avatar_accessories_catalogue())

    @app.patch("/api/avatar/accessories")
    async def patch_avatar_accessories(payload: dict[str, Any]) -> JSONResponse:
        """Phase 4 (expression overhaul): merge accessory toggles.

        Validates each key against the known accessory catalogue
        (lollipop / eyeglasses / head_sunglasses / eye_color /
        crossed_arms) and the enum allow-list. Unknown keys or bad
        enum values return 400. Successful patches broadcast an
        ``avatar_settings_changed`` event over the WS hub so the
        renderer's accessory layer re-syncs on the next frame.
        """
        try:
            snapshot = session.update_avatar_accessories(payload)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        hub.broadcast({
            "type": "avatar_settings_changed",
            "settings": dict(snapshot),
        })
        return JSONResponse(session.avatar_accessories_catalogue())

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

    # ── Avatar / static assets ──────────────────────────────────────

    # Bundled Live2D avatar (Alexia by default). The directory is
    # gitignored, so we tolerate it being missing at boot — the
    # SessionController already fell back to ``avatar = None`` in that
    # case and the front-end shows the empty-avatar placeholder.
    avatar_root = session.avatar_root
    avatar_root.mkdir(parents=True, exist_ok=True)
    app.mount(
        "/avatar",
        StaticFiles(directory=str(avatar_root), check_dir=False),
        name="avatar",
    )

    # Self-image text mount (data/persona/self_image.txt). Renamed to
    # ``/persona-text/`` to avoid the singular-vs-plural footgun the
    # avatar work introduced.
    if _PERSONA_TEXT_DIR.exists():
        app.mount(
            "/persona-text",
            StaticFiles(directory=str(_PERSONA_TEXT_DIR)),
            name="persona-text",
        )

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
            if full_path.startswith(("api/", "ws", "avatar/", "persona-text/", "assets/", "live2d/")):
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

    async def _broadcast_voice_owner_async(owner_id: str | None) -> None:
        """Send ``voice_owner_changed`` to every connected client.

        We send directly inside the active handler task instead of
        scheduling onto the loop so the message lands deterministically
        before the handler awaits its next receive — important for the
        test client which drives requests synchronously.
        """
        payload = json.dumps(
            {"type": "voice_owner_changed", "owner_id": owner_id},
            default=str,
        )
        for sock, _cid in hub.snapshot():
            try:
                await sock.send_text(payload)
            except Exception:
                hub.discard(sock)

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        import uuid as _uuid
        client_id = _uuid.uuid4().hex
        hub.add(ws, client_id)
        ws.client_id = client_id  # type: ignore[attr-defined]

        # On connect, prime the client with current state.
        try:
            await ws.send_text(json.dumps({
                "type": "hello",
                "client_id": client_id,
                "session": session.session_key,
                "model": session.effective_chat_model,
                "tts_enabled": bool(session._settings.tts.enabled),
                "voice_active": bool(live_session.is_active),
                "voice_owner_id": hub.voice_owner_id,
                "context_window": session.context_window_size,
                "context_source": session.context_window_source,
                "avatar": session.avatar_payload(),
                "identity": {
                    "user_display_name": (
                        session._settings.assistant.user_display_name or ""
                    ),
                    "needs_onboarding": bool(session.needs_onboarding),
                },
            }, default=str))
        except Exception:
            pass

        active_turn: threading.Event | None = None

        try:
            while True:
                # ``receive()`` returns the full ASGI envelope so we can
                # handle both text (JSON control messages) and bytes
                # (binary mic frames) without the framework guessing.
                envelope = await ws.receive()
                if envelope.get("type") != "websocket.receive":
                    # Disconnect / connect events surface here too; only
                    # ``receive`` carries payload data.
                    if envelope.get("type") == "websocket.disconnect":
                        break
                    continue

                if "bytes" in envelope and envelope["bytes"] is not None:
                    data: bytes = envelope["bytes"]
                    if not data:
                        continue
                    frame_type = data[0]
                    if frame_type == _frames.FRAME_MIC_START:
                        if hub.voice_owner_id != client_id:
                            # Stale frame from a non-owner; drop.
                            continue
                        parsed = _frames.parse_mic_start(data[1:])
                        if parsed is None:
                            continue
                        sample_rate, channels, dsp_flags = parsed
                        try:
                            session.feed_audio_start(sample_rate, channels, dsp_flags)
                        except Exception:
                            log.debug("feed_audio_start failed", exc_info=True)
                    elif frame_type == _frames.FRAME_MIC_PCM:
                        if hub.voice_owner_id != client_id:
                            continue
                        pcm = data[1:]
                        if not pcm:
                            continue
                        # We've already adopted sample_rate/channels via
                        # ``mic_start``; pass 0 to signal "use whatever
                        # the source last latched".
                        try:
                            session.feed_audio_frame(0, 0, pcm)
                        except Exception:
                            log.debug("feed_audio_frame failed", exc_info=True)
                    else:
                        log.debug("unknown binary frame: 0x%02x", frame_type)
                    continue

                raw = envelope.get("text")
                if raw is None:
                    continue
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
                    # Claim mic ownership for this client. The server
                    # only consumes mic frames from the owning client;
                    # the previous owner (if any) is notified via the
                    # broadcast below so its UI can flip back to idle.
                    _claimed, previous = hub.claim_voice(client_id)
                    if previous and previous != client_id:
                        try:
                            session.feed_audio_end()
                        except Exception:
                            log.debug("feed_audio_end on takeover failed", exc_info=True)
                    if not live_session.is_active:
                        live_session.start()
                    else:
                        hub.broadcast({"type": "voice_state", "state": "listening"})
                    await _broadcast_voice_owner_async(client_id)

                elif msg_type == "voice_stop":
                    released = hub.release_voice(client_id)
                    if released:
                        try:
                            session.feed_audio_end()
                        except Exception:
                            log.debug("feed_audio_end on release failed", exc_info=True)
                        await _broadcast_voice_owner_async(None)
                    if not hub.voice_owner_id:
                        # Only stop the LiveSession when nobody is
                        # holding the mic — another client may still
                        # be the owner after a takeover-then-release.
                        live_session.stop()

                elif msg_type == "ping":
                    await ws.send_text(json.dumps({"type": "pong"}))

                elif msg_type == "presence":
                    # Tab visibility / window focus from the React client.
                    # Folded into a single boolean client-side (browser
                    # ``visibilitychange`` AND-gated with Tauri
                    # ``tauri://focus``/``blur``).
                    #
                    # We then fold ACROSS clients in the hub so a
                    # multi-window session (main + persona) reports
                    # "present" iff at least one window is visible. A
                    # naked ``set_user_present(visible)`` would let a
                    # newly-blurred client overwrite a still-visible
                    # sibling.
                    visible = bool(msg.get("visible", True))
                    hub.set_client_presence(client_id, visible)
                    try:
                        session.set_user_present(hub.any_client_visible())
                    except Exception:
                        log.debug("set_user_present failed", exc_info=True)

                elif msg_type == "user_activity":
                    # Foreground app the user is in (Tauri shell only;
                    # browser shells never emit this). Server-side gate
                    # in ``set_user_active_app`` drops the value when
                    # the privacy toggle is off, so a buggy client
                    # can't leak the data even if it kept emitting.
                    raw_app = msg.get("app")
                    app_name: str | None
                    if raw_app is None:
                        app_name = None
                    else:
                        app_name = str(raw_app)
                    try:
                        session.set_user_active_app(app_name)
                    except Exception:
                        log.debug("set_user_active_app failed", exc_info=True)

        except WebSocketDisconnect:
            pass
        except Exception:
            log.exception("websocket loop crashed")
        finally:
            released_owner = hub.discard(ws)
            # Re-fold presence after the disconnect: ``hub.discard``
            # has already removed this client's entry from
            # ``_visible_by_client``, so an empty hub now reports
            # ``False`` and the typed-proactive timer disarms. This
            # is the path that catches "user closed the last window"
            # cleanly even if the React side never managed to send a
            # final ``presence`` frame.
            try:
                session.set_user_present(hub.any_client_visible())
            except Exception:
                log.debug("set_user_present on disconnect failed", exc_info=True)
            if released_owner is not None:
                try:
                    session.feed_audio_end()
                except Exception:
                    log.debug("feed_audio_end on disconnect failed", exc_info=True)
                if not hub.voice_owner_id:
                    try:
                        live_session.stop()
                    except Exception:
                        log.debug("live_session.stop on disconnect failed", exc_info=True)
                # Broadcast inline so the message lands while the
                # remaining sockets' handlers are still active — a
                # background ``hub.broadcast`` race here can let the
                # disconnect side's cleanup finish before the task
                # fires, which the FastAPI test client hates.
                await _broadcast_voice_owner_async(hub.voice_owner_id)

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
