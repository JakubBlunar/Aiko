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
    from app.core.session.session_controller import SessionController

from app.core.infra import crash_logging
from app.core.session.live_session import LiveSession
from app.core.infra.settings import (
    OUTFIT_MODES,
    _parse_grounding_line_mode,
    persist_user_overrides,
)
from app.web import audio_frames as _frames


log = logging.getLogger("app.web.server")


def _classify_test_error(exc: BaseException) -> tuple[str, str]:
    """Map an exception from the ``test-connection`` probe to a UI code.

    The UI uses ``error_code`` to pick a leading label ("Unauthorized:",
    "Model not found:", ...) and ``error_message`` for the
    provider-verbatim body. Both are bounded to ~500 chars so a
    runaway provider response can't bloat the JSON payload.
    """
    import requests as _requests

    raw = str(exc)
    text = raw.lower()
    if isinstance(exc, _requests.exceptions.Timeout):
        return "timeout", "Request timed out."
    if isinstance(exc, _requests.exceptions.ConnectionError):
        return "network", f"Cannot reach the endpoint: {raw[:400]}"
    if isinstance(exc, _requests.HTTPError):
        status = (
            exc.response.status_code if exc.response is not None else 0
        )
        if status in (401, 403):
            return "unauthorized", _trim(raw, 500)
        if status == 404:
            return "not_found_model", _trim(raw, 500)
        if status == 429:
            return "rate_limited", _trim(raw, 500)
    if "unauthor" in text or "invalid api key" in text or "api_key" in text:
        return "unauthorized", _trim(raw, 500)
    if "not found" in text or "model" in text and "404" in text:
        return "not_found_model", _trim(raw, 500)
    if "rate limit" in text or "quota" in text:
        return "rate_limited", _trim(raw, 500)
    if "timed out" in text or "timeout" in text:
        return "timeout", _trim(raw, 500)
    return "unknown", _trim(raw, 500)


def _trim(text: str, limit: int) -> str:
    if not text:
        return ""
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


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
        # Single elected "audio owner" — the one client that actually
        # plays TTS / earcon PCM. The desktop shell keeps the persona
        # window's webview alive-but-hidden in the background (see
        # ``hide_persona_window`` in ``src-tauri``); without an owner
        # lock BOTH that hidden webview and the visible main window
        # would receive the broadcast PCM and play it ~tens of ms apart,
        # which the user hears as an echo/mumble on the first sentence
        # of every turn. We elect one owner (preferring a visible
        # window) and send binary audio frames only to it. ``None`` when
        # no clients are connected. Lipsync ``audio_amplitude`` JSON
        # still broadcasts to every window so a hidden persona can keep
        # animating its mouth without playing sound.
        self._audio_owner_id: str | None = None
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

    # ── Audio-playback owner election ──────────────────────────────

    def _elect_audio_owner_locked(self) -> str | None:
        """Pick the single client that should play audio. Caller holds
        ``self._lock``.

        Rules, in order:
          1. Stability — if the current owner is still connected AND is
             either visible OR nobody is visible, keep it (avoids
             flip-flopping the owner mid-session).
          2. Otherwise prefer the first-connected *visible* client (a
             hidden persona webview reports ``visible=False`` so it is
             never chosen while a real window is up).
          3. Otherwise (no visible clients) fall back to the
             first-connected client so audio is never silently lost.
        """
        client_ids = list(self._sockets.values())
        if not client_ids:
            return None
        visible = [cid for cid in client_ids if self._visible_by_client.get(cid)]
        current = self._audio_owner_id
        if current is not None and current in client_ids:
            if self._visible_by_client.get(current) or not visible:
                return current
        if visible:
            return visible[0]
        return client_ids[0]

    def recompute_audio_owner(self) -> tuple[bool, str | None]:
        """Re-elect the audio owner. Returns ``(changed, owner_id)``.

        Call after any event that can change eligibility: connect,
        disconnect, or a ``presence`` update.
        """
        with self._lock:
            new_owner = self._elect_audio_owner_locked()
            changed = new_owner != self._audio_owner_id
            self._audio_owner_id = new_owner
            return changed, new_owner

    @property
    def audio_owner_id(self) -> str | None:
        with self._lock:
            return self._audio_owner_id

    def _audio_owner_socket_locked(self) -> WebSocket | None:
        owner = self._audio_owner_id
        if owner is None:
            return None
        for ws, cid in self._sockets.items():
            if cid == owner:
                return ws
        return None

    def send_audio_bytes(self, frame: bytes) -> None:
        """Schedule a binary audio frame to the elected owner only.

        Replaces :meth:`broadcast_bytes` for TTS / earcon PCM so only
        one window plays the stream (see ``_audio_owner_id``).
        """
        if not frame:
            return
        self._schedule(self._send_audio_bytes_async(frame))

    async def _send_audio_bytes_async(self, frame: bytes) -> None:
        with self._lock:
            ws = self._audio_owner_socket_locked()
        if ws is None:
            # No elected owner (no clients, or owner raced a disconnect).
            return
        try:
            await ws.send_bytes(frame)
        except Exception:
            # Owner vanished mid-stream; drop it and re-elect so the
            # next frame finds a live socket.
            self.discard(ws)
            self.recompute_audio_owner()

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

    def _on_message(
        speaker: str, text: str, message_id: int | None = None,
    ) -> None:
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
        # K32: proactive bubbles persist a row but bypass the streamed
        # turn_done path, so carry the id here so the client can stamp
        # backendId and enable the reaction tray on the new bubble.
        if message_id is not None:
            payload["message_id"] = int(message_id)
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
            hub.send_audio_bytes(
                _frames.build_audio_start(stream_byte, sample_rate, channels)
            )
            _stream_started[stream] = True
        if stream_byte == _frames.FRAME_TTS_PCM:
            hub.send_audio_bytes(_frames.build_tts_pcm(pcm))
        elif stream_byte == _frames.FRAME_EARCON_PCM:
            hub.send_audio_bytes(_frames.build_earcon_pcm(pcm))

    def _on_audio_frame_end(stream: str) -> None:
        stream_byte = _frames.stream_byte(stream)
        if stream_byte == 0:
            return
        if _stream_started.get(stream, False):
            hub.send_audio_bytes(_frames.build_audio_end(stream_byte))
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

    def _on_avatar_touch(payload: dict[str, Any]) -> None:
        """K31: forward an ``avatar_touch`` event to every webview.

        Both the chat window and the persona overlay subscribe to
        the same WS hub; the store reducer routes the event to
        the avatar engine (lean-in animation) AND to the bubble
        badge / persona-banner surface.
        """
        hub.broadcast({"type": "avatar_touch", **payload})

    def _on_message_reaction_updated(payload: dict[str, Any]) -> None:
        """K32: rebroadcast a reaction-counter update.

        Payload shape: ``{"message_id": int, "reactions":
        dict[str, int]}``. The frontend reducer merges the
        new map onto the matching message row so both windows
        stay in sync (a heart click in chat shows up in the
        persona action banner too).
        """
        hub.broadcast({"type": "message_reaction_updated", **payload})

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
    try:
        session.add_avatar_touch_listener(_on_avatar_touch)
    except Exception:
        log.debug("avatar touch listener subscription failed", exc_info=True)
    try:
        session.add_message_reaction_listener(_on_message_reaction_updated)
    except Exception:
        log.debug(
            "message reaction listener subscription failed", exc_info=True,
        )

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
            _metrics = payload.get("metrics", {}) or {}
            hub.broadcast({
                "type": "turn_done",
                "metrics": _metrics,
                # K32: lift the persisted assistant row id to the top
                # level so the client can stamp the live bubble's
                # backendId and enable reactions immediately.
                "assistant_message_id": _metrics.get("assistant_message_id"),
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

    # ── REST: long-term goals (K1) ───────────────────────────────────

    @app.post("/api/goals/run")
    async def run_goal_worker() -> JSONResponse:
        """Force a single ``GoalWorker.run()`` and return the result.

        Mirrors the cooperative shape of ``/api/curiosity-seeds/run``: the
        Memory tab's "Regenerate now" / "Reflect now" button posts here so
        a tester can verify the worker's output (bootstrap on a cold ring,
        or one reflection note on an existing goal) without waiting for
        the next idle tick. Bypasses the idle-window gate but still
        respects the worker's own rate limiter, so calling this in a
        loop won't blow past ``agent.goal_worker_per_*_cap``.
        """
        worker = getattr(session, "_goal_worker", None)
        if worker is None:
            raise HTTPException(503, "goal worker unavailable")
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

    @app.get("/api/topic-graph")
    def get_topic_graph() -> JSONResponse:
        """K9: read-only snapshot of the memory topic-cluster graph.

        Advisory + lazily rebuilt from the in-process memory mirror, so
        there's no body and no WS event -- the Memory-tab panel fetches
        on open and on manual refresh.
        """
        return JSONResponse(session.topic_graph_snapshot())

    @app.get("/api/persona-drift")
    def get_persona_drift() -> JSONResponse:
        """K10: last persona-regression snapshot (``{}`` until first run).

        Pull-only — the Diagnostics panel fetches on open and after a
        manual "Run check", so there's no WS event.
        """
        return JSONResponse(session.persona_regression_snapshot())

    @app.post("/api/persona-drift/run")
    def run_persona_drift() -> JSONResponse:
        """K10: replay the golden-turn fixture and return a fresh snapshot.

        Synchronous handler — FastAPI runs it in the threadpool, so the
        blocking worker-LLM calls stay off the event loop.
        """
        return JSONResponse(session.run_persona_regression())

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

    def _on_thread_note(payload: dict[str, Any]) -> None:
        # K21: fresh-eyes note upserted. The sidebar refetches its
        # session list on this event to pick up the new title.
        try:
            hub.broadcast({"type": "thread_note_updated", "payload": dict(payload)})
        except Exception:
            log.debug("thread note broadcast failed", exc_info=True)

    try:
        session.add_thread_note_listener(_on_thread_note)
    except Exception:
        log.debug("thread note listener subscription failed", exc_info=True)

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

    # ── REST + WS bridge: Background tasks (chunk 13) ───────────────
    #
    # ``/api/tasks`` is read-mostly: paginated history, single-row
    # snapshot, cancel, and answer. There's deliberately NO
    # ``POST /api/tasks`` to spawn new tasks — spawning is exclusively
    # Aiko's job (``start_*`` LLM tools) or system code's job (idle
    # workers, MCP debug). The frontend's role is observation +
    # cancel + answer. See ``docs/brain-orchestration.md`` for the
    # rationale.
    #
    # The WS listener bridge fans every orchestrator event out as a
    # JSON frame so the frontend can keep its local task cache in
    # sync without polling. ``visible_to_user=false`` rows are
    # filtered at the bridge — system-internal tasks never reach the
    # wire.

    def _resolve_task_user_id() -> str:
        """Mirror ``app.llm.tools.file_tasks._user_id``.

        Lifts ``session._user_id`` (set by the identity layer) so
        REST + LLM tool calls land on the same task rows. Falls back
        to ``"default"`` so a brand-new install before onboarding
        still has a coherent user_id stamp.
        """
        return str(getattr(session, "_user_id", "default") or "default")

    # Tracks task IDs whose ``task_started`` was suppressed because
    # ``visible_to_user=false``. ``task_progress`` events for those
    # IDs must also be filtered (the orchestrator dispatches them
    # regardless of visibility); ``task_completed`` clears the entry.
    # The set lives in the bridge closure so it doesn't survive a
    # listener resubscribe — that's intentional since the orchestrator
    # itself doesn't persist anything we don't want to lose.
    _hidden_task_ids: set[int] = set()
    _hidden_lock = threading.Lock()

    def _on_task_event(kind: str, payload: dict[str, Any]) -> None:
        """Broadcast every task lifecycle event to connected WS clients.

        Runs on the orchestrator's worker thread (or the caller's
        thread for ``task_started`` / cancel). Must stay cheap —
        ``hub.broadcast`` queues to each client's send loop.

        ``visible_to_user=false`` snapshots are dropped here so the
        wire only ever carries user-visible tasks. The orchestrator
        still fans the event out so future metric / audit listeners
        can opt in to system-internal traffic.
        """
        try:
            if kind == "task_progress":
                task_id = int(payload.get("task_id", 0) or 0)
                with _hidden_lock:
                    if task_id in _hidden_task_ids:
                        return
                hub.broadcast(
                    {
                        "type": "task_progress",
                        "task_id": task_id,
                        "patch": dict(payload.get("patch", {}) or {}),
                    }
                )
                return
            task = payload.get("task") if isinstance(payload, dict) else None
            if not isinstance(task, dict):
                return
            visible = bool(task.get("visible_to_user", True))
            task_id = int(task.get("id", 0) or 0)
            if kind == "task_started" and not visible and task_id:
                with _hidden_lock:
                    _hidden_task_ids.add(task_id)
            if kind == "task_completed" and task_id:
                # Always clear so the set doesn't grow unbounded
                # across long sessions; visibility filter still
                # blocks the broadcast below for hidden rows.
                with _hidden_lock:
                    _hidden_task_ids.discard(task_id)
            if not visible:
                return
            hub.broadcast({"type": kind, "task": dict(task)})
        except Exception:
            log.debug("task event broadcast failed: kind=%s", kind, exc_info=True)

    try:
        orchestrator = getattr(session, "_task_orchestrator", None)
        if orchestrator is not None:
            orchestrator.add_task_listener(_on_task_event)
    except Exception:
        log.debug("task listener subscribe failed", exc_info=True)

    @app.get("/api/tasks")
    def list_tasks(
        limit: int = 50,
        offset: int = 0,
        status: str | None = None,
        roots_only: bool = False,
    ) -> JSONResponse:
        """Paginated task history for the current user.

        Filters ``visible_to_user=false`` rows. ``status`` accepts
        one of the canonical task statuses (``running``,
        ``awaiting_input``, ``paused``, ``done``, ``failed``,
        ``cancelled``, ``interrupted``) or omitted for all. ``limit``
        is clamped to ``[1, 200]``. ``roots_only=true`` restricts the
        page (and ``total``) to top-level tasks so the Tasks tab can
        render parents only and fetch each parent's children on
        demand via ``GET /api/tasks/{id}/children``.
        """
        from app.core.tasks import task_snapshot as _snapshot
        from app.core.tasks.task_handler import VALID_STATUSES

        store = getattr(session, "_task_store", None)
        if store is None:
            return JSONResponse(
                {"tasks": [], "count": 0, "total": 0, "enabled": False}
            )
        clamped_limit = max(1, min(int(limit), 200))
        clamped_offset = max(0, int(offset))
        status_norm: str | None = None
        if status is not None:
            candidate = str(status).strip().lower()
            if candidate:
                if candidate not in VALID_STATUSES:
                    raise HTTPException(
                        400,
                        f"status must be one of {sorted(VALID_STATUSES)}",
                    )
                status_norm = candidate
        user_id = _resolve_task_user_id()
        try:
            rows = store.list_for_user(
                user_id,
                status=status_norm,
                limit=clamped_limit,
                offset=clamped_offset,
                visible_only=True,
                roots_only=bool(roots_only),
            )
            total = store.count_for_user(
                user_id,
                status=status_norm,
                visible_only=True,
                roots_only=bool(roots_only),
            )
        except Exception as exc:
            log.exception("list_tasks failed: status=%s", status_norm)
            raise HTTPException(500, f"list failed: {exc}") from exc
        items = [_snapshot(r) for r in rows]
        return JSONResponse(
            {
                "tasks": items,
                "count": len(items),
                "total": int(total),
                "enabled": True,
            }
        )

    @app.get("/api/tasks/{task_id}")
    def get_task(task_id: int) -> JSONResponse:
        """Single-row snapshot. 404 when row missing or hidden."""
        from app.core.tasks import task_snapshot as _snapshot

        store = getattr(session, "_task_store", None)
        if store is None:
            raise HTTPException(503, "task subsystem unavailable")
        row = store.get(int(task_id))
        if row is None or not bool(row.visible_to_user):
            raise HTTPException(404, "task not found")
        return JSONResponse({"task": _snapshot(row)})

    @app.post("/api/tasks/{task_id}/cancel")
    def cancel_task_rest(task_id: int) -> JSONResponse:
        """User-initiated cancel.

        Idempotent: cancelling an already-terminal task returns 200
        with ``cancelled=False`` so the UI can render "already done"
        without a noisy error.
        """
        orch = getattr(session, "_task_orchestrator", None)
        if orch is None:
            raise HTTPException(503, "task subsystem unavailable")
        store = getattr(session, "_task_store", None)
        if store is not None:
            row = store.get(int(task_id))
            if row is None or not bool(row.visible_to_user):
                raise HTTPException(404, "task not found")
        try:
            cancelled = orch.cancel(int(task_id))
        except Exception as exc:
            log.exception("task cancel failed: task=%d", task_id)
            raise HTTPException(500, f"cancel failed: {exc}") from exc
        return JSONResponse(
            {"task_id": int(task_id), "cancelled": bool(cancelled)}
        )

    @app.post("/api/tasks/{task_id}/answer")
    async def answer_task_rest(
        task_id: int, payload: dict[str, Any]
    ) -> JSONResponse:
        """Resolve an ``awaiting_input`` task with a user-supplied answer.

        Body shape: ``{"input": str}``. Mirrors the
        :class:`TaskInputNeeded` -> ``answer`` semantics in
        :class:`TaskOrchestrator`. Returns 409 when the task is not
        currently ``awaiting_input`` so the UI can refresh and show
        the new state.
        """
        if not isinstance(payload, dict):
            raise HTTPException(400, "expected JSON object body")
        answer = payload.get("input")
        if answer is None:
            answer = payload.get("answer")  # forgiving alias
        if not isinstance(answer, str) or not answer.strip():
            raise HTTPException(400, "input must be a non-empty string")
        orch = getattr(session, "_task_orchestrator", None)
        if orch is None:
            raise HTTPException(503, "task subsystem unavailable")
        store = getattr(session, "_task_store", None)
        if store is not None:
            row = store.get(int(task_id))
            if row is None or not bool(row.visible_to_user):
                raise HTTPException(404, "task not found")
        try:
            accepted = orch.answer(int(task_id), answer)
        except Exception as exc:
            log.exception("task answer failed: task=%d", task_id)
            raise HTTPException(500, f"answer failed: {exc}") from exc
        if not accepted:
            raise HTTPException(
                409,
                "task did not accept the answer (wrong status or handler "
                "unregistered)",
            )
        return JSONResponse({"task_id": int(task_id), "accepted": True})

    @app.get("/api/tasks/{task_id}/events")
    def list_task_events(
        task_id: int,
        limit: int = 100,
        offset: int = 0,
        order: str = "asc",
    ) -> JSONResponse:
        """Paginated event log for a single task (schema v17).

        Returns the audit trail the orchestrator + handlers appended
        via the event-log path. ``order`` is ``asc`` (default,
        chronological replay) or ``desc`` (newest first).
        Clamped to ``[1, 1000]`` per page.
        """
        store = getattr(session, "_task_store", None)
        event_store = getattr(session, "_task_event_store", None)
        if store is None or event_store is None:
            raise HTTPException(503, "task subsystem unavailable")
        row = store.get(int(task_id))
        if row is None or not bool(row.visible_to_user):
            raise HTTPException(404, "task not found")
        order_norm = str(order or "").strip().lower()
        if order_norm not in ("asc", "desc"):
            order_norm = "asc"
        clamped_limit = max(1, min(int(limit), 1000))
        clamped_offset = max(0, int(offset))
        try:
            events = event_store.list_for_task(
                int(task_id),
                limit=clamped_limit,
                offset=clamped_offset,
                ascending=order_norm == "asc",
            )
            total = event_store.count_for_task(int(task_id))
        except Exception as exc:
            log.exception("list_task_events failed: task=%d", task_id)
            raise HTTPException(500, f"list failed: {exc}") from exc
        return JSONResponse(
            {
                "task_id": int(task_id),
                "events": [
                    {
                        "id": int(e.id),
                        "task_id": int(e.task_id),
                        "type": str(e.type),
                        "data": (dict(e.data) if e.data is not None else None),
                        "created_at": str(e.created_at),
                    }
                    for e in events
                ],
                "count": len(events),
                "total": int(total),
            }
        )

    @app.get("/api/tasks/{task_id}/children")
    def list_task_children(task_id: int) -> JSONResponse:
        """Child tasks of a parent (schema v17 task tree).

        Used by the Tasks tab to lazily expand a parent into its
        workflow steps. Returns visible children only, ascending by
        id (spawn order). No pagination — the per-parent fan-out is
        bounded. 404 mirrors ``get_task`` when the parent row is
        missing or hidden.
        """
        from app.core.tasks import task_snapshot as _snapshot

        store = getattr(session, "_task_store", None)
        if store is None:
            raise HTTPException(503, "task subsystem unavailable")
        row = store.get(int(task_id))
        if row is None or not bool(row.visible_to_user):
            raise HTTPException(404, "task not found")
        try:
            children = [
                c
                for c in store.list_children(int(task_id))
                if bool(c.visible_to_user)
            ]
        except Exception as exc:
            log.exception("list_task_children failed: task=%d", task_id)
            raise HTTPException(500, f"list failed: {exc}") from exc
        return JSONResponse(
            {
                "task_id": int(task_id),
                "children": [_snapshot(c) for c in children],
                "count": len(children),
            }
        )

    @app.get("/api/tasks/{task_id}/inputs")
    def list_task_inputs(task_id: int) -> JSONResponse:
        """Full input/answer history for a task (schema v17).

        Returns one row per question the handler asked (pending /
        answered / superseded / cancelled). Chronological. No
        pagination — the per-task volume is bounded.
        """
        store = getattr(session, "_task_store", None)
        input_store = getattr(session, "_task_input_store", None)
        if store is None or input_store is None:
            raise HTTPException(503, "task subsystem unavailable")
        row = store.get(int(task_id))
        if row is None or not bool(row.visible_to_user):
            raise HTTPException(404, "task not found")
        try:
            inputs = input_store.list_for_task(int(task_id), ascending=True)
        except Exception as exc:
            log.exception("list_task_inputs failed: task=%d", task_id)
            raise HTTPException(500, f"list failed: {exc}") from exc
        return JSONResponse(
            {
                "task_id": int(task_id),
                "inputs": [
                    {
                        "id": int(inp.id),
                        "task_id": int(inp.task_id),
                        "prompt": str(inp.prompt),
                        "kind": inp.kind,
                        "options": (
                            list(inp.options) if inp.options is not None else None
                        ),
                        "status": str(inp.status),
                        "response": inp.response,
                        "created_at": str(inp.created_at),
                        "answered_at": inp.answered_at,
                    }
                    for inp in inputs
                ],
                "count": len(inputs),
            }
        )

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

    # ── REST: K32 user reactions on Aiko bubbles ────────────────────

    @app.post("/api/chat/messages/{message_id}/reactions")
    async def add_user_reaction(
        message_id: int, payload: dict[str, Any] | None = None,
    ) -> JSONResponse:
        """K32: register one reaction click on an assistant bubble.

        Body: ``{"kind": "heart" | "hug" | "laugh" | "thumbs" |
        "rose" | "surprise"}``. Returns the new full reactions
        map. Side-effects (axes nudge + inner-life cue) live in
        :meth:`SessionController.apply_user_reaction`.
        """
        if not bool(
            getattr(
                session._settings.agent, "user_reactions_enabled", True,
            ),
        ):
            raise HTTPException(503, "user reactions feature disabled")
        if not isinstance(payload, dict) or "kind" not in payload:
            raise HTTPException(400, "kind is required")
        kind = payload.get("kind")
        if not isinstance(kind, str) or not kind.strip():
            raise HTTPException(400, "kind must be a non-empty string")
        from app.core.relationship.user_reactions import is_valid_kind

        if not is_valid_kind(kind):
            raise HTTPException(
                400, f"unknown reaction kind: {kind}",
            )
        result = session.apply_user_reaction(int(message_id), kind)
        if result is None:
            raise HTTPException(
                404, "message not found or not an assistant bubble",
            )
        return JSONResponse(result)

    @app.delete("/api/chat/messages/{message_id}/reactions/{kind}")
    async def remove_user_reaction(
        message_id: int, kind: str,
    ) -> JSONResponse:
        """K32: undo one reaction click (decrements the counter).

        Symmetric with the POST endpoint for persistence + WS
        broadcast; axes are NOT subtracted on undo.
        """
        if not bool(
            getattr(
                session._settings.agent, "user_reactions_enabled", True,
            ),
        ):
            raise HTTPException(503, "user reactions feature disabled")
        from app.core.relationship.user_reactions import is_valid_kind

        if not is_valid_kind(kind):
            raise HTTPException(
                400, f"unknown reaction kind: {kind}",
            )
        result = session.remove_user_reaction(int(message_id), kind)
        if result is None:
            raise HTTPException(404, "message not found")
        return JSONResponse(result)

    # ── REST: Live2D avatar (fixed Alexia bundle) ───────────────────

    @app.get("/api/avatar")
    def get_avatar() -> JSONResponse:
        return JSONResponse({"avatar": session.avatar_payload()})

    @app.patch("/api/avatar")
    async def patch_avatar(payload: dict[str, Any]) -> JSONResponse:
        scale = payload.get("scale_multiplier")
        outfit = payload.get("auto_outfit")
        expressiveness = payload.get("expressiveness")
        mood_inertia_damping = payload.get("mood_inertia_damping")
        if mood_inertia_damping is not None:
            mood_inertia_damping = bool(mood_inertia_damping)
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
            mood_inertia_damping=mood_inertia_damping,
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

    # ── REST: in-chat attachments (D2 Part B) ───────────────────────
    #
    # Images + text files dropped into the chat composer. They land in
    # the managed ``data/attachments/`` root (auto-registered read-only
    # sandbox root) so Aiko can resolve ``Attachments:<file>`` through
    # the describe_image / read_file workflow skills. No bytes are sent
    # to the cloud chat model — the worker (local) model reads them.

    @app.post("/api/chat/attachments")
    async def upload_attachment(file: UploadFile = File(...)) -> JSONResponse:
        from app.core.tasks.attachments import (
            DEFAULT_MAX_ATTACHMENT_BYTES,
            save_attachment,
        )

        if not file.filename:
            raise HTTPException(400, "missing filename")
        body = await file.read()
        if len(body) == 0:
            raise HTTPException(400, "uploaded file is empty")
        # Image allow-list mirrors the live vision config; text set uses
        # the module default. Byte cap rides the vision cap when set.
        vision_cfg = getattr(session._settings.agent, "vision", None)
        image_exts = tuple(
            getattr(vision_cfg, "allowed_extensions", ()) or ()
        ) or None
        max_bytes = int(
            getattr(vision_cfg, "max_bytes", DEFAULT_MAX_ATTACHMENT_BYTES)
            or DEFAULT_MAX_ATTACHMENT_BYTES
        )
        try:
            saved = save_attachment(
                data=body,
                filename=file.filename,
                image_extensions=image_exts,
                max_bytes=max_bytes,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            log.exception("attachment save failed")
            raise HTTPException(500, f"attachment save failed: {exc}") from exc
        return JSONResponse({"attachment": saved.as_dict()})

    @app.delete("/api/chat/attachments/{stored_name}")
    def delete_attachment_endpoint(stored_name: str) -> JSONResponse:
        from app.core.tasks.attachments import delete_attachment

        ok = delete_attachment(stored_name)
        return JSONResponse({"deleted": bool(ok), "stored_name": stored_name})

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

    # D2 Part B: serve uploaded in-chat attachments so the composer +
    # chat bubbles can render image thumbnails. The dir is the managed
    # ``Attachments`` sandbox root; gitignored + auto-created.
    from app.core.tasks.attachments import ensure_attachments_dir

    _attach_dir = ensure_attachments_dir()
    app.mount(
        "/attachment-files",
        StaticFiles(directory=str(_attach_dir), check_dir=False),
        name="attachment-files",
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
            if full_path.startswith(("api/", "ws", "avatar/", "attachment-files/", "persona-text/", "assets/", "live2d/")):
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

    async def _broadcast_audio_owner_async(owner_id: str | None) -> None:
        """Tell every client which one owns audio playback.

        Sent inline (not scheduled) for the same determinism reason as
        :func:`_broadcast_voice_owner_async`. The client uses it as a
        belt-and-suspenders gate on top of the server-side targeted
        send: only the owner plays PCM, so even a stray broadcast can't
        double up.
        """
        payload = json.dumps(
            {"type": "audio_owner_changed", "owner_id": owner_id},
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

        # Elect the audio owner now that this socket is registered, so
        # the hello below carries an accurate ``audio_owner_id`` and a
        # change (e.g. the first client connecting) is announced to all.
        audio_owner_changed, _audio_owner = hub.recompute_audio_owner()

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
                "audio_owner_id": hub.audio_owner_id,
                "context_window": session.context_window_size,
                "context_source": session.context_window_source,
                "avatar": session.avatar_payload(),
                "identity": {
                    "user_display_name": (
                        session._settings.assistant.user_display_name or ""
                    ),
                    "needs_onboarding": bool(session.needs_onboarding),
                },
                # Persona-overlay banners (K31/K32) read these on connect so
                # they honour the master switch + duration instead of the
                # hardcoded defaults they used before (I5).
                "companion": {
                    "touch_enabled": bool(
                        getattr(session._settings.agent, "touch_enabled", True),
                    ),
                    "user_reactions_enabled": bool(
                        getattr(
                            session._settings.agent,
                            "user_reactions_enabled",
                            True,
                        ),
                    ),
                    "persona_touch_banner_enabled": bool(
                        getattr(
                            session._settings.agent,
                            "persona_touch_banner_enabled",
                            True,
                        ),
                    ),
                    "persona_touch_banner_duration_seconds": int(
                        getattr(
                            session._settings.agent,
                            "persona_touch_banner_duration_seconds",
                            20,
                        ),
                    ),
                },
            }, default=str))
        except Exception:
            pass

        # If this connection changed who owns audio (e.g. it's the first
        # client, or it displaced a stale owner), tell everyone so the
        # client-side gate stays in sync with the server's targeted send.
        if audio_owner_changed:
            try:
                await _broadcast_audio_owner_async(hub.audio_owner_id)
            except Exception:
                log.debug("audio owner broadcast on connect failed", exc_info=True)

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
                    # D2 Part B — optional in-chat attachments. Each is a
                    # ``{id, filename, kind, rel_path, bytes}`` ref the
                    # client got back from ``POST /api/chat/attachments``.
                    # We only forward well-formed refs that point at the
                    # managed ``Attachments`` root (never trust a
                    # client-supplied path into another root).
                    attachments = _sanitize_attachment_refs(msg.get("attachments"))
                    active_turn = threading.Event()
                    _spawn_chat_turn(session, hub, text, active_turn, attachments)

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
                    # A visibility change can move audio ownership (e.g.
                    # the main window became visible and should take over
                    # from a hidden persona, or vice-versa). Re-elect and
                    # announce only on an actual change.
                    try:
                        _changed, _owner = hub.recompute_audio_owner()
                        if _changed:
                            await _broadcast_audio_owner_async(_owner)
                    except Exception:
                        log.debug("audio owner recompute on presence failed", exc_info=True)

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
            # Re-elect the audio owner: if the client that just left was
            # the owner, hand playback to a remaining (preferably
            # visible) window so the next turn still has sound.
            try:
                _audio_changed, _audio_owner = hub.recompute_audio_owner()
                if _audio_changed:
                    await _broadcast_audio_owner_async(_audio_owner)
            except Exception:
                log.debug("audio owner recompute on disconnect failed", exc_info=True)
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


def _sanitize_attachment_refs(raw: Any) -> list[dict]:
    """Validate client-supplied attachment refs from a ``chat`` command.

    Each ref must be a dict carrying a ``rel_path`` that targets the
    managed ``Attachments`` root (``Attachments:<name>``). Anything else
    — a path into another root, a missing rel_path, a non-dict — is
    dropped so a buggy/hostile client can't point Aiko at an arbitrary
    file via the attachment side-channel. Only the small allow-listed
    fields are kept.
    """
    from app.core.tasks.attachments import ATTACHMENTS_LABEL

    if not isinstance(raw, list):
        return []
    out: list[dict] = []
    prefix = ATTACHMENTS_LABEL + ":"
    for item in raw:
        if not isinstance(item, dict):
            continue
        rel = str(item.get("rel_path") or "").strip()
        if not rel.startswith(prefix) or "/" in rel or "\\" in rel:
            continue
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in ("image", "text"):
            continue
        out.append({
            "id": str(item.get("id") or "").strip(),
            "filename": str(item.get("filename") or "").strip(),
            "kind": kind,
            "rel_path": rel,
            "bytes": int(item.get("bytes") or 0),
        })
        if len(out) >= 8:
            break
    return out


def _spawn_chat_turn(
    session: "SessionController",
    hub: _Hub,
    text: str,
    done_event: threading.Event,
    attachments: list[dict] | None = None,
) -> None:
    """Run a chat turn on a worker thread, streaming tokens via the hub.

    Chunk 8 of the brain-orchestration refactor: this routes through
    :meth:`SessionController.enqueue_user_message` rather than calling
    :meth:`SessionController.chat_once_streaming` directly. The
    worker thread blocks on the queue's reply future, but the actual
    turn (LLM stream, TTS dispatch, post-turn jobs) runs on the
    brain-loop thread. This is the contract the design doc calls
    "the queue is the single consumer for chat" — two WS clients
    typing at the same time can no longer race a turn against each
    other. Whichever message lands on the queue first wins; the
    second waits its turn.

    The streaming callbacks (per-token broadcast, generation-status
    progress, user-side stop button via ``done_event``) ride the new
    ``on_token`` / ``on_generation_status`` / ``stop_requested``
    kwargs which the mixin bundles into a
    :class:`ProducerCallbacks` and stashes on the queued event. The
    handler unbundles them on the brain-loop thread and threads
    them straight into ``chat_once_streaming`` — the hub broadcast
    is already thread-safe so this works without further locking.
    """

    def _run() -> None:
        try:
            session._notify_message("You", text)
            # D2 Part B: the user bubble is created from the
            # ``_notify_message`` broadcast above (which has no
            # attachment channel); follow it with a dedicated event so
            # the live bubble renders the chips/thumbnails. History
            # reloads pick the same data off ``messages.attachments``.
            if attachments:
                hub.broadcast({
                    "type": "user_attachments",
                    "attachments": attachments,
                })

            def on_token(chunk: str) -> None:
                if chunk:
                    hub.broadcast({"type": "token", "chunk": chunk})

            def on_status(status: str) -> None:
                hub.broadcast({"type": "status", "message": status})

            def stop_requested() -> bool:
                return done_event.is_set()

            reply = session.enqueue_user_message(
                text=text,
                mode="typed",
                wait_for_reply=True,
                # Generous: post-turn workers can push to a minute or
                # so on slow boxes. The WS client doesn't have a
                # tighter expectation than the legacy direct call did.
                timeout=180.0,
                on_token=on_token,
                on_generation_status=on_status,
                stop_requested=stop_requested,
                attachments=attachments,
            )
            session._notify_message("Assistant", reply or "")
            _metrics = session.get_last_metrics() or {}
            hub.broadcast({
                "type": "turn_done",
                "metrics": _metrics,
                # K32: lift the persisted assistant row id so the client
                # can stamp the live bubble's backendId and enable the
                # reaction tray without a history reload.
                "assistant_message_id": _metrics.get("assistant_message_id"),
            })
        except Exception as exc:
            log.exception("chat turn failed")
            hub.broadcast({"type": "error", "message": str(exc)})
        finally:
            done_event.set()

    threading.Thread(target=_run, daemon=True, name="web-chat-turn").start()
