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


def _search_public_snapshot(search: Any) -> dict[str, Any]:
    """Masked snapshot of the ``search`` block — never echoes the raw key.

    Defensive: returns safe DuckDuckGo defaults when ``search`` is
    ``None`` / a stub without the fields, so a test session whose
    ``_settings`` lacks a real ``SearchSettings`` still serialises.
    """
    if search is None:
        return {
            "provider": "duckduckgo",
            "has_api_key": False,
            "api_key_env": "LANGSEARCH_API_KEY",
            "langsearch_summary": True,
            "langsearch_freshness": "noLimit",
            "langsearch_count": 10,
            "fallback_to_duckduckgo": True,
            "timeout_seconds": 12.0,
            "langsearch_min_interval_seconds": 1.1,
            "query_reformulation_enabled": True,
        }
    from app.llm.search.providers import resolve_api_key

    resolved = resolve_api_key(
        getattr(search, "api_key", "") or "",
        getattr(search, "api_key_env", "") or "",
    )
    return {
        "provider": getattr(search, "provider", "duckduckgo"),
        "has_api_key": bool(resolved),
        "api_key_env": getattr(search, "api_key_env", ""),
        "langsearch_summary": bool(getattr(search, "langsearch_summary", True)),
        "langsearch_freshness": getattr(search, "langsearch_freshness", "noLimit"),
        "langsearch_count": int(getattr(search, "langsearch_count", 10)),
        "fallback_to_duckduckgo": bool(
            getattr(search, "fallback_to_duckduckgo", True)
        ),
        "timeout_seconds": float(getattr(search, "timeout_seconds", 12.0)),
        "langsearch_min_interval_seconds": float(
            getattr(search, "langsearch_min_interval_seconds", 1.1)
        ),
        "query_reformulation_enabled": bool(
            getattr(search, "query_reformulation_enabled", True)
        ),
    }


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DIST_DIR = _PROJECT_ROOT / "web" / "dist"

# The PWA manifest is served via the SPA fallback's ``FileResponse``, which
# infers the content type from the extension. ``.webmanifest`` isn't in the
# stdlib table, so register it (browsers want ``application/manifest+json``).
import mimetypes as _mimetypes

_mimetypes.add_type("application/manifest+json", ".webmanifest")
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

    from app.web.rest import (
        sessions_settings_routes,
        memory_world_routes,
        tasks_files_routes,
    )

    sessions_settings_routes.register(
        app, session, hub, _broadcast_context_window, live_session
    )
    memory_world_routes.register(
        app, session, hub, _broadcast_context_window, live_session
    )
    tasks_files_routes.register(
        app, session, hub, _broadcast_context_window, live_session
    )

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
        # The reject list holds prefixes owned by a dedicated ``app.mount`` (or
        # the WS endpoint) — those must 404 instead of falling through to
        # index.html. ``live2d/`` is intentionally NOT here: those runtime
        # scripts are copied by Vite from ``public/live2d/`` into the dist root
        # and have no mount, so they must reach the ``target.is_file()`` branch
        # below to be served (otherwise they 404 only in the FastAPI/production
        # path while working in dev, where Vite serves ``public/`` directly).
        @app.get("/{full_path:path}")
        def spa_fallback(full_path: str) -> FileResponse:
            if full_path.startswith(("api/", "ws", "avatar/", "attachment-files/", "persona-text/", "assets/")):
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
