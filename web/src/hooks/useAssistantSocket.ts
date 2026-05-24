import { useCallback, useEffect, useRef } from "react";
import { api } from "../api";
import { playDone, playThinking } from "../earcons";
import { useAssistantStore } from "../store";
import type { WsClientCommand, WsServerEvent } from "../types";

const WS_URL = "/ws";
const RECONNECT_DELAY_MS = 1500;
const PING_INTERVAL_MS = 25_000;

function resolveWsUrl(): string {
  // In dev, Vite proxies /ws -> backend. In prod we share an origin with FastAPI.
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${WS_URL}`;
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
        // Re-sync voice mode if the backend was already running a loop
        // when this socket connected (e.g. page refresh mid-session).
        store.setVoiceMode(evt.voice_active ? "listening" : "off");
        // Pull current persona once we know we're connected so the avatar
        // hydrates on a hard refresh.
        api.getPersona()
          .then((res) => store.setPersona(res.persona))
          .catch(() => {
            /* persona endpoint missing or backend offline -- ignore */
          });
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

      case "stt_final":
        store.setLastTranscript(evt.text);
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
        store.upsertMemory(evt.memory);
        const text = (evt.memory.content || "").slice(0, 80);
        store.pushToast(
          "memory",
          text ? `Aiko remembered: ${text}` : "Aiko remembered something",
        );
        break;
      }

      case "memory_deleted":
        store.removeMemory(evt.id);
        break;

      case "persona_changed":
        store.setPersona(evt.persona);
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

    ws.addEventListener("open", () => {
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
      setConnection({ status: "disconnected", lastError: null });
      if (pingIntervalRef.current) {
        window.clearInterval(pingIntervalRef.current);
        pingIntervalRef.current = null;
      }
      socketRef.current = null;
      scheduleReconnect();
    });

    ws.addEventListener("error", () => {
      setConnection({ status: "disconnected", lastError: "websocket error" });
    });
  }, [handleEvent, setConnection]);

  useEffect(() => {
    closedByUser.current = false;
    connect();
    return () => {
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
  }, [connect]);

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
