import { useCallback, useEffect, useRef } from "react";
import { api } from "../api";
import { desktop } from "../desktop/commands";
import { backendBase } from "../desktop/runtime";
import { playDone, playThinking } from "../earcons";
import { useAssistantStore } from "../store";
import type { WsClientCommand, WsServerEvent } from "../types";

const WS_PATH = "/ws";
const RECONNECT_DELAY_MS = 1500;
const PING_INTERVAL_MS = 25_000;

function resolveWsUrl(): string {
  // In dev, Vite proxies /ws -> backend. In prod we share an origin with
  // FastAPI. Inside a Tauri webview the origin is `tauri://localhost`, so
  // we route through the configured backend host instead.
  return `${backendBase().ws}${WS_PATH}`;
}

/**
 * Single websocket client. Auto-reconnects on close. Caller gets a `send`
 * function for issuing commands; everything else lives in the Zustand store.
 */
export function useAssistantSocket(): {
  send: (cmd: WsClientCommand) => void;
} {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const pingIntervalRef = useRef<number | null>(null);
  const closedByUser = useRef(false);
  // Set true on the first successful ``open`` event. Until then we
  // suppress the brief "disconnected" flash that the WS lifecycle
  // would otherwise emit on every failed connect attempt while the
  // backend is still booting — the user sees a stable "connecting"
  // amber pill instead of a strobe between amber and red.
  const hasEverConnected = useRef(false);

  const setConnection = useAssistantStore((s) => s.setConnection);

  const handleEvent = useCallback((evt: WsServerEvent) => {
    const store = useAssistantStore.getState();

    switch (evt.type) {
      case "hello":
        store.setSessionKey(evt.session);
        store.setModel(evt.model);
        store.setTtsEnabled(evt.tts_enabled);
        if (evt.context_window) {
          store.setContextInfo(
            evt.context_window,
            evt.context_source ?? "fallback",
          );
        }
        store.setVoiceMode(evt.voice_active ? "listening" : "off");
        // The hello frame includes the avatar payload; on a stale
        // backend that doesn't, fall back to a /api/avatar fetch so
        // the renderer hydrates anyway.
        if (evt.avatar) {
          store.setAvatar(evt.avatar);
        } else {
          api.getAvatar()
            .then((res) => store.setAvatar(res.avatar))
            .catch(() => {
              /* avatar endpoint missing or backend offline -- ignore */
            });
        }
        // Desktop snapshot is optional too: a stale backend may not
        // emit it. The browser layout doesn't care; the persona window
        // re-fetches via /api/desktop if the field is missing.
        if (evt.desktop) {
          store.setDesktop(evt.desktop);
        }
        // Identity (first-run onboarding gate). Stale backends omit
        // it; in that case fall back to GET /api/settings/identity so
        // we never miss the onboarding modal trigger.
        if (evt.identity) {
          store.setIdentity(evt.identity);
        } else {
          api.getIdentity()
            .then((next) => store.setIdentity(next))
            .catch(() => {
              /* identity endpoint missing -- treat as already configured */
            });
        }
        break;

      case "session_changed":
        store.setSessionKey(evt.session);
        store.clearMessages();
        break;

      case "history_cleared":
        store.clearMessages();
        store.pushSystemMessage("History cleared.");
        break;

      case "message":
        // System messages (status events from background workers, etc.)
        // are pushed inline. User messages from the backend's
        // _notify_message hook (typed input or voice STT result) are
        // appended as user bubbles. Streamed assistant turns arrive via
        // the "token" event, so we drop the trailing "message" envelope
        // for them to avoid duplicating the bubble.
        // Exception: proactive nudges aren't streamed -- they arrive as a
        // single complete assistant message and need to be appended here.
        if (evt.role === "system") {
          store.pushSystemMessage(evt.content);
        } else if (evt.role === "user") {
          store.appendUserMessage(evt.content);
        } else if (evt.role === "assistant" && evt.kind === "proactive") {
          store.appendProactiveMessage(evt.content);
        }
        break;

      case "token":
        if (!store.turnInProgress) {
          store.setTurnInProgress(true);
          store.appendAssistantBubble();
        }
        store.appendAssistantToken(evt.chunk);
        break;

      case "turn_done":
        store.finishAssistantBubble();
        store.setMetrics(evt.metrics || {});
        if (evt.metrics?.context_window) {
          store.setContextInfo(
            evt.metrics.context_window,
            String(evt.metrics.context_source ?? "fallback"),
          );
        }
        store.setTurnInProgress(false);
        store.setStatus("");
        store.clearToolActivity();
        break;

      case "metrics_update":
        store.mergeMetrics(evt.metrics || {});
        break;

      case "context_window":
        store.setContextInfo(evt.context_window, evt.context_source);
        store.setModel(evt.model);
        break;

      case "model_changed":
        store.setModel(evt.model);
        break;

      case "tts_state":
        store.setTtsState(
          evt.event === "start" ? "speaking" : "idle",
          evt.text ?? "",
          evt.reaction ?? "neutral",
        );
        // While voice mode is on, mirror tts_state into voiceMode so the
        // mic-button label reads "speaking" while Aiko is talking.
        if (store.voiceMode !== "off") {
          if (evt.event === "start") {
            store.setVoiceMode("speaking");
          } else if (store.voiceMode === "speaking") {
            store.setVoiceMode("listening");
            // Floor returns to user -- play the "done" earcon as a cue.
            playDone();
          }
        }
        break;

      case "stt_partial":
        store.setStatus(`Listening: ${evt.text}`);
        break;

      case "stt_partial_live":
        // Single transient line above the chat input — the latest partial
        // we're hearing right now. Replaces any previous partial in place.
        store.setCurrentPartial(evt.text);
        break;

      case "stt_final":
        store.setLastTranscript(evt.text);
        // Clear the live partial so the transient line vanishes the
        // instant the real transcript lands.
        store.setCurrentPartial("");
        store.setStatus("");
        break;

      case "voice_state": {
        const previous = store.voiceMode;
        store.setVoiceMode(evt.state);
        if (evt.state === "off") {
          store.setAudioLevel(0);
        }
        // Earcon when Aiko transitions into the thinking state from a
        // user-driven listening or transcribing state. Skip if voice mode
        // was already off (suppresses spurious chirps from history hydration).
        if (
          evt.state === "thinking" &&
          previous !== "thinking" &&
          previous !== "off"
        ) {
          playThinking();
        }
        break;
      }

      case "audio_level":
        store.setAudioLevel(evt.level);
        break;

      case "status":
        store.setStatus(evt.message);
        break;

      case "error":
        store.pushSystemMessage(`Error: ${evt.message}`);
        store.setTurnInProgress(false);
        break;

      case "memory_added": {
        store.applyMemoryAdded(evt.memory);
        const text = (evt.memory.content || "").slice(0, 80);
        store.pushToast(
          "memory",
          text ? `Aiko remembered: ${text}` : "Aiko remembered something",
        );
        break;
      }

      case "memory_updated":
        store.applyMemoryUpdated(evt.memory);
        break;

      case "memory_deleted":
        store.applyMemoryDeleted(evt.id);
        break;

      case "world_updated":
        store.applyWorldPatch(evt.patch);
        break;

      case "shared_moment_updated": {
        // Patch is exactly one of {moment} (create/update) or
        // {deleted_moment_id} (delete). Both keep the Together tab
        // timeline + total in sync without a refetch.
        if (evt.patch.moment) {
          store.upsertSharedMoment(evt.patch.moment);
        } else if (typeof evt.patch.deleted_moment_id === "number") {
          store.removeSharedMoment(evt.patch.deleted_moment_id);
        }
        break;
      }

      case "relationship_axes_updated":
        store.setRelationshipAxes(evt.axes);
        break;

      case "avatar_settings_changed":
        store.setAvatarSettings(evt.settings);
        // Server now inlines resolved_outfit + circadian_period so the
        // cross-fade reacts immediately to LLM [[outfit:X]] directives
        // and to the user flipping ``auto_outfit`` via the panel
        // (instead of waiting for the next mood_state broadcast).
        if (evt.circadian_period !== undefined || evt.resolved_outfit !== undefined) {
          store.updateAvatarWorldState({
            circadian_period: evt.circadian_period,
            resolved_outfit: evt.resolved_outfit,
          });
        }
        break;

      case "identity_changed":
        // First-run onboarding success or a later "Change name" submit.
        // Pushed by ``PUT /api/settings/identity`` server-side.
        store.setIdentity({
          user_display_name: evt.user_display_name,
          needs_onboarding: Boolean(evt.needs_onboarding),
        });
        break;

      case "desktop_settings_changed":
        // Mirror the snapshot into the store first so any open window
        // re-renders against the new geometry. Inside the Tauri shell
        // we then issue the matching window-management commands so the
        // OS-level frame matches; in the browser this is a no-op.
        store.setPersonaWindow(evt.persona_window);
        if (typeof window !== "undefined" && "__TAURI_INTERNALS__" in window) {
          import("../desktop/commands")
            .then(({ desktop }) => {
              void desktop.setPersonaGeometry(
                evt.persona_window.width,
                evt.persona_window.height,
              );
              void desktop.setPersonaAlwaysOnTop(
                evt.persona_window.always_on_top,
              );
            })
            .catch(() => {
              /* desktop helpers absent at runtime — ignore */
            });
        }
        break;

      case "avatar_overlay":
        store.setAvatarOverlay({
          name: evt.name,
          expiresAt: Date.now() + Math.max(150, evt.duration_ms),
        });
        break;

      case "avatar_motion":
        store.setAvatarMotion({
          name: evt.name,
          group: evt.group,
          index: evt.index,
          firedAt: Date.now(),
          priority: evt.priority,
        });
        break;

      case "audio_amplitude":
        store.setAudioAmplitude(evt.level);
        break;

      case "tool_event":
        store.pushToolEvent({
          name: evt.payload.name,
          event: evt.event,
          ok: evt.payload.ok,
          preview: evt.payload.preview,
          at: Date.now(),
        });
        break;

      case "mood_state":
        store.setMood({
          label: evt.label,
          intensity: evt.intensity,
          valence: evt.valence,
          arousal: evt.arousal,
        });
        if (evt.circadian_period || evt.resolved_outfit) {
          store.updateAvatarWorldState({
            circadian_period: evt.circadian_period,
            resolved_outfit: evt.resolved_outfit,
          });
        }
        break;

      case "backchannel":
        store.pushBackchannel(evt.hint);
        break;

      case "pong":
        break;
    }
  }, []);

  const connect = useCallback(() => {
    if (socketRef.current && socketRef.current.readyState <= WebSocket.OPEN) {
      return;
    }
    setConnection({ status: "connecting", lastError: null });
    const ws = new WebSocket(resolveWsUrl());
    socketRef.current = ws;

    // React StrictMode (and HMR) double-invoke the mount effect, so the
    // FIRST socket's ``open`` / ``message`` / ``close`` events can fire
    // *after* the second mount has installed a fresh socket. Without
    // this guard the orphaned socket would (a) dispatch its messages
    // through ``handleEvent`` — duplicating every streamed token in the
    // chat — and (b) when its ``close`` fires, null out the active
    // socket ref and schedule a reconnect, leaving us with two live
    // connections forever. Bail out unless the listener fired on the
    // socket that's still the canonical one.
    const isCurrent = () => socketRef.current === ws;

    ws.addEventListener("open", () => {
      if (!isCurrent()) return;
      hasEverConnected.current = true;
      setConnection({ status: "connected", lastError: null });
      if (pingIntervalRef.current) {
        window.clearInterval(pingIntervalRef.current);
      }
      pingIntervalRef.current = window.setInterval(() => {
        if (ws.readyState === WebSocket.OPEN) {
          ws.send(JSON.stringify({ type: "ping" }));
        }
      }, PING_INTERVAL_MS);
    });

    ws.addEventListener("message", (event) => {
      if (!isCurrent()) return;
      try {
        const parsed = JSON.parse(event.data) as WsServerEvent;
        handleEvent(parsed);
      } catch {
        // ignore malformed frames
      }
    });

    const scheduleReconnect = () => {
      if (closedByUser.current) {
        return;
      }
      if (reconnectTimeoutRef.current !== null) {
        return;
      }
      reconnectTimeoutRef.current = window.setTimeout(() => {
        reconnectTimeoutRef.current = null;
        connect();
      }, RECONNECT_DELAY_MS);
    };

    ws.addEventListener("close", () => {
      if (!isCurrent()) return;
      // Suppress the "offline" flash during the initial boot window:
      // until we've successfully opened a WS at least once, every
      // failed attempt stays in "connecting" state so the user sees
      // one stable indicator while Python is warming up. After the
      // first real connection, dropouts ARE meaningful and we
      // surface them as "disconnected" (red pill) so the user knows.
      const nextStatus = hasEverConnected.current
        ? "disconnected"
        : "connecting";
      setConnection({ status: nextStatus, lastError: null });
      if (pingIntervalRef.current) {
        window.clearInterval(pingIntervalRef.current);
        pingIntervalRef.current = null;
      }
      socketRef.current = null;
      scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      if (!isCurrent()) return;
      // Same flash-suppression rationale as the ``close`` handler:
      // a pre-first-connection error stays "connecting" because we
      // ARE about to retry; only show "disconnected" once the
      // connection has been real at least once.
      const nextStatus = hasEverConnected.current
        ? "disconnected"
        : "connecting";
      setConnection({
        status: nextStatus,
        lastError: hasEverConnected.current ? "websocket error" : null,
      });
    });
  }, [handleEvent, setConnection]);

  useEffect(() => {
    closedByUser.current = false;
    hasEverConnected.current = false;
    let cancelled = false;
    // Show "connecting" immediately so the UI doesn't briefly flash
    // "offline" while the bootstrap gate is polling for the backend.
    // This also keeps the placeholder text honest during the initial
    // boot window (``npm run desktop`` starts the API and the UI in
    // parallel, so the UI is up well before Python finishes
    // importing the ML stack).
    setConnection({ status: "connecting", lastError: null });
    // Inside a Tauri webview, gate the WS dial on the backend sidecar
    // being up. ``ensureBackendRunning`` resolves immediately in the
    // browser (where the user runs ``python -m app.web`` themselves)
    // and after a poll loop inside the desktop app where the Rust side
    // may need to spawn the venv first.
    //
    // Failure of the gate is **not** terminal: ``npm run desktop`` on
    // Windows spawns the backend via ``concurrently`` (the Tauri
    // sidecar is a no-op there), so the gate may time out even when
    // the backend is on its way up in a sibling terminal. Fall
    // through to ``connect()`` regardless; the WS close-event
    // reconnect loop keeps retrying every RECONNECT_DELAY_MS until
    // the backend answers.
    void desktop.ensureBackendRunning().then((result) => {
      if (cancelled) return;
      if (!result.ok) {
        setConnection({
          status: "connecting",
          lastError: result.error || null,
        });
      }
      connect();
    });
    return () => {
      cancelled = true;
      closedByUser.current = true;
      if (reconnectTimeoutRef.current !== null) {
        window.clearTimeout(reconnectTimeoutRef.current);
        reconnectTimeoutRef.current = null;
      }
      if (pingIntervalRef.current !== null) {
        window.clearInterval(pingIntervalRef.current);
        pingIntervalRef.current = null;
      }
      socketRef.current?.close();
      socketRef.current = null;
    };
  }, [connect, setConnection]);

  const send = useCallback((cmd: WsClientCommand) => {
    const ws = socketRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      console.warn("WS not ready; dropping command", cmd);
      return;
    }
    ws.send(JSON.stringify(cmd));
  }, []);

  return { send };
}
