import { useCallback, useEffect, useRef } from "react";
import { api } from "../api";
import { AudioOutputManager } from "../audio/AudioOutputManager";
import {
  getStoredOutputDeviceId,
  onDeviceListChange,
} from "../audio/DeviceManager";
import { desktop } from "../desktop/commands";
import { backendBase } from "../desktop/runtime";
import { playDone, playThinking } from "../earcons";
import { debugLog } from "../log";
import { useAssistantStore } from "../store";
import type { WsClientCommand, WsServerEvent } from "../types";

/**
 * High-frequency WS event types whose full payload would drown the
 * debug log if captured verbatim. We still record that the event
 * occurred (so the timeline is complete) but strip the payload to keep
 * the line cheap.
 */
const NOISY_WS_EVENTS = new Set([
  "audio_level",
  "audio_amplitude",
  "stt_partial",
  "stt_partial_live",
  "token",
]);

const WS_PATH = "/ws";
const RECONNECT_DELAY_MS = 1500;
const PING_INTERVAL_MS = 25_000;
// How long the tool-activity strip ("aiko is searching her notebook…")
// lingers in the chat after turn_done fires. Was 0ms (immediate
// clear) -- users couldn't read the chips fast enough. The next user
// message cancels the linger early so a fresh turn starts with an
// empty strip.
const TOOL_ACTIVITY_LINGER_MS = 12_000;

function resolveWsUrl(): string {
  // In dev, Vite proxies /ws -> backend. In prod we share an origin with
  // FastAPI. Inside a Tauri webview the origin is `tauri://localhost`, so
  // we route through the configured backend host instead.
  return `${backendBase().ws}${WS_PATH}`;
}

/**
 * Single websocket client. Auto-reconnects on close. Caller gets a `send`
 * function for issuing commands plus a `sendBytes` for binary frames
 * (mic PCM); everything else lives in the Zustand store.
 */
export function useAssistantSocket(): {
  send: (cmd: WsClientCommand) => void;
  sendBytes: (frame: Uint8Array) => void;
  audioOutput: AudioOutputManager;
} {
  const socketRef = useRef<WebSocket | null>(null);
  const reconnectTimeoutRef = useRef<number | null>(null);
  const pingIntervalRef = useRef<number | null>(null);
  const closedByUser = useRef(false);
  // Pending deferred-clear timer for the tool-activity strip. See
  // ``TOOL_ACTIVITY_LINGER_MS`` -- nulled out whenever a new user
  // message arrives (so the next turn starts with a fresh strip) or
  // when the component unmounts.
  const toolClearTimerRef = useRef<number | null>(null);
  const audioOutputRef = useRef<AudioOutputManager | null>(null);
  if (audioOutputRef.current === null) {
    audioOutputRef.current = new AudioOutputManager({
      sinkId: getStoredOutputDeviceId(),
    });
  }
  // Set true on the first successful ``open`` event. Until then we
  // suppress the brief "disconnected" flash that the WS lifecycle
  // would otherwise emit on every failed connect attempt while the
  // backend is still booting — the user sees a stable "connecting"
  // amber pill instead of a strobe between amber and red.
  const hasEverConnected = useRef(false);

  const setConnection = useAssistantStore((s) => s.setConnection);

  const handleEvent = useCallback((evt: WsServerEvent) => {
    const store = useAssistantStore.getState();

    // Tap every WS event into the debug log so the entire dispatch
    // stream is recoverable from ``app.log``. ``debugLog.log`` is a
    // no-op when the toggle is off, so this is essentially free in
    // the normal case. We strip the payload for noisy event types
    // (audio amplitude, partials, streamed tokens) to keep the log
    // bounded — only the ``kind`` is interesting for those.
    if (NOISY_WS_EVENTS.has(evt.type)) {
      debugLog.log({ source: "ws", kind: evt.type });
    } else {
      debugLog.log({ source: "ws", kind: evt.type, payload: evt });
    }

    switch (evt.type) {
      case "hello":
        store.setSessionKey(evt.session);
        store.setModel(evt.model);
        store.setTtsEnabled(evt.tts_enabled);
        if (typeof evt.client_id === "string") {
          store.setClientId(evt.client_id);
        }
        store.setVoiceOwnerId(
          typeof evt.voice_owner_id === "string" ? evt.voice_owner_id : null,
        );
        store.setAudioOwnerId(
          typeof evt.audio_owner_id === "string" ? evt.audio_owner_id : null,
        );
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
        // Companion soft-physicality flags for the persona overlay (I5).
        if (evt.companion) {
          store.setCompanionSettings(evt.companion);
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
          // New user message = new turn boundary. Cancel any pending
          // tool-activity linger timer from the previous turn and
          // clear the strip so the next turn's chips render fresh.
          if (toolClearTimerRef.current !== null) {
            window.clearTimeout(toolClearTimerRef.current);
            toolClearTimerRef.current = null;
          }
          store.clearToolActivity();
        } else if (evt.role === "assistant" && evt.kind === "proactive") {
          // K32: carry the persisted id so reactions work on the
          // proactive bubble immediately.
          store.appendProactiveMessage(evt.content, evt.message_id);
        }
        break;

      case "user_attachments":
        // D2 Part B: stamp the just-appended user bubble with the
        // attachments the client uploaded for this turn so the chips /
        // thumbnails render live (history reloads pick them up from
        // ``messages.attachments``).
        store.attachLastUserAttachments(evt.attachments);
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
        // K32: stamp the just-finished bubble with its persisted id so
        // the reaction tray + "mark as moment" enable without a reload.
        store.stampAssistantBackendId(evt.assistant_message_id);
        store.setMetrics(evt.metrics || {});
        if (evt.metrics?.context_window) {
          store.setContextInfo(
            evt.metrics.context_window,
            String(evt.metrics.context_source ?? "fallback"),
          );
        }
        store.setTurnInProgress(false);
        store.setStatus("");
        // Keep the tool-activity strip on screen for a few extra
        // seconds after the turn ends so the user can actually read
        // the chips before they vanish. Cancelled early when the next
        // user message lands (see the "message" case above).
        if (toolClearTimerRef.current !== null) {
          window.clearTimeout(toolClearTimerRef.current);
        }
        toolClearTimerRef.current = window.setTimeout(() => {
          toolClearTimerRef.current = null;
          useAssistantStore.getState().clearToolActivity();
        }, TOOL_ACTIVITY_LINGER_MS);
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

      case "llm_settings_changed":
        // Backend broadcast after:
        //   - legacy PATCH /api/settings { chat_llm: ... } / PUT
        //     /api/settings/llm-credentials (carries chat_llm? only),
        //   - PR 2 PATCH /api/llm/providers / /api/llm/routes (carries
        //     providers + routes snapshots).
        // The CustomEvent kept for back-compat with any drawer code
        // that listens on the window for "settings reload, please".
        if (evt.providers !== undefined) {
          store.setLlmProviders(evt.providers);
        }
        if (evt.routes !== undefined) {
          store.setLlmRoutes(evt.routes);
        }
        if (typeof window !== "undefined") {
          window.dispatchEvent(new CustomEvent("aiko:llm-settings-changed"));
        }
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
        // Voice mode transitions are the entry point for several
        // avatar cascades (the cry-bug came from "thinking" mapping
        // to "concerned"). Log the from/to pair explicitly so the
        // backend ``app.log`` shows the transition next to whatever
        // backend event drove it (filler injection, tool dispatch,
        // STT final, etc.).
        if (previous !== evt.state) {
          debugLog.log({
            source: "voice",
            kind: "modeChanged",
            payload: { from: previous, to: evt.state },
          });
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
        // Commit any partial streamed text into the bubble before
        // we drop ``turnInProgress``. Without this the bubble that
        // was streaming when the error fired is left with
        // ``streaming: true`` forever (selectable in the DOM via
        // the ``.streaming-caret`` blink) and the partial reply
        // would silently disappear from view on the next session
        // switch when ``streamingDraft`` clears. ``finishAssistantBubble``
        // is a no-op when there's nothing to commit.
        store.finishAssistantBubble();
        store.setTurnInProgress(false);
        break;

      case "memory_added": {
        store.applyMemoryAdded(evt.memory);
        // Keep the toast readable but don't chop a memory mid-thought:
        // show up to ~220 chars and mark truncation with an ellipsis so
        // it's clear there's more in the Memory tab.
        const raw = (evt.memory.content || "").trim();
        const text = raw.length > 220 ? `${raw.slice(0, 219).trimEnd()}…` : raw;
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

      case "belief_added":
        store.applyBeliefAdded(evt.belief);
        break;

      case "belief_updated":
        store.applyBeliefUpdated(evt.belief);
        break;

      case "belief_deleted":
        store.applyBeliefDeleted(evt.id);
        break;

      case "world_updated":
        store.applyWorldPatch(evt.patch);
        break;

      case "thread_note_updated":
        // K21: a fresh-eyes note was upserted; nudge the sidebar to
        // refetch its session list so the new title shows up.
        store.bumpSessionListSignal();
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

      case "avatar_touch":
        // K31 soft physicality: stamp the gesture badge on the
        // currently-streaming assistant bubble AND fan out to the
        // Live2D engine (lean-in animation) AND to the persona
        // action banner. The store reducers do all the routing.
        store.pushAvatarTouch({
          kind: String(evt.kind ?? ""),
          label: String(evt.label ?? ""),
          emoji: String(evt.emoji ?? ""),
          duration_ms: Number(evt.duration_ms ?? 0),
          lean_amount: Number(evt.lean_amount ?? 0),
          overlays: Array.isArray(evt.overlays)
            ? evt.overlays.map((s: unknown) => String(s))
            : [],
        });
        if (evt.kind) {
          // B7: pass the full descriptor so invented custom gestures
          // keep their model-supplied label / emoji on the bubble badge.
          store.appendGestureToCurrentTurn({
            kind: String(evt.kind),
            label: evt.label != null ? String(evt.label) : undefined,
            emoji: evt.emoji != null ? String(evt.emoji) : undefined,
          });
        }
        break;

      case "message_reaction_updated": {
        // K32 reciprocity: a reaction click landed (either from
        // this window or another tab); merge the new counter map
        // onto the matching message so both surfaces stay in sync.
        const mid = Number(evt.message_id ?? 0) | 0;
        if (mid > 0 && evt.reactions && typeof evt.reactions === "object") {
          const reactions: Record<string, number> = {};
          for (const [k, v] of Object.entries(
            evt.reactions as Record<string, unknown>,
          )) {
            const n = Number(v);
            if (Number.isFinite(n) && n > 0) {
              reactions[String(k)] = n | 0;
            }
          }
          store.applyMessageReactions(mid, reactions);
        }
        break;
      }

      case "task_started":
        // Chunk 14: a new task row landed. The strip prepends the
        // chip; the tasks tab prepends to history when the user is
        // on page 0 with a matching status filter. The
        // ``visible_to_user=false`` filter is enforced server-side
        // in ``app/web/server.py`` so anything reaching us is
        // safe to surface.
        store.applyTaskStarted(evt.task);
        break;

      case "task_progress":
        // Progress events are UI-only by hard rule (see
        // ``docs/brain-orchestration.md`` § "Progress events are
        // UI-only"). They never park a prompt cue; the strip
        // just moves the bar.
        store.applyTaskProgress(evt.task_id, evt.patch || {});
        break;

      case "task_input_needed":
        // The handler emitted ``TaskInputNeeded``; status flips to
        // ``awaiting_input`` and ``input_request`` carries the
        // prompt + click-options. The chat-first answer path
        // (Aiko asks naturally in her next turn) is unaffected
        // by this — the strip is the optional click-fallback.
        store.applyTaskInputNeeded(evt.task);
        break;

      case "task_completed":
        // Terminal transition — ``status`` is one of
        // ``done`` / ``failed`` / ``cancelled``. The strip keeps
        // the chip visible briefly; the sweep helper drops it
        // after ``TASK_STRIP_FADE_MS``.
        store.applyTaskCompleted(evt.task);
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

      case "logging_settings_changed":
        // Another tab (or the user via this one) flipped the
        // ``ui_log_enabled`` toggle. Mirror into the store + flip the
        // batcher so this tab's debug bridge matches the backend.
        store.setLoggingSettings({
          ui_log_enabled: Boolean(evt.logging.ui_log_enabled),
          ui_log_categories: Array.isArray(evt.logging.ui_log_categories)
            ? evt.logging.ui_log_categories.map((token) => String(token))
            : [],
          ui_log_max_batch: Number(evt.logging.ui_log_max_batch) || 50,
          ui_log_max_payload_bytes:
            Number(evt.logging.ui_log_max_payload_bytes) || 2048,
        });
        debugLog.setEnabled(Boolean(evt.logging.ui_log_enabled));
        break;

      case "companion_settings_changed":
        // A companion knob changed in another window; mirror the
        // persona-banner / touch flags so the overlay reconciles live.
        store.setCompanionSettings(evt.companion);
        break;

      case "voice_owner_changed":
        store.setVoiceOwnerId(evt.owner_id ?? null);
        break;

      case "audio_owner_changed":
        store.setAudioOwnerId(evt.owner_id ?? null);
        break;

      case "pong":
        break;
    }
  }, []);

  // Gate for incoming TTS / earcon PCM. The server already targets the
  // elected owner, so in practice a non-owner never receives audio
  // frames — but this is a cheap belt-and-suspenders check so a stray
  // broadcast (or a future code path that forgets to target) can't make
  // two windows play the same clip. Play when the server hasn't elected
  // anyone yet (single-client boot) or when we are the owner.
  const shouldPlayAudio = useCallback((): boolean => {
    const { audioOwnerId, clientId } = useAssistantStore.getState();
    if (!audioOwnerId) return true;
    return audioOwnerId === clientId;
  }, []);

  const connect = useCallback(() => {
    if (socketRef.current && socketRef.current.readyState <= WebSocket.OPEN) {
      return;
    }
    setConnection({ status: "connecting", lastError: null });
    const ws = new WebSocket(resolveWsUrl());
    ws.binaryType = "arraybuffer";
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
      // Binary frames carry TTS / earcon PCM; pass them straight to
      // the audio output manager. Text frames are JSON envelopes.
      if (event.data instanceof ArrayBuffer) {
        const out = audioOutputRef.current;
        if (out && shouldPlayAudio()) {
          out.handleFrame(event.data);
        }
        return;
      }
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
  }, [handleEvent, setConnection, shouldPlayAudio]);

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
      if (toolClearTimerRef.current !== null) {
        window.clearTimeout(toolClearTimerRef.current);
        toolClearTimerRef.current = null;
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

  const sendBytes = useCallback((frame: Uint8Array) => {
    const ws = socketRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      return;
    }
    ws.send(frame);
  }, []);

  // Re-route audio when the OS adds / removes devices (eg. plug in
  // headphones while the app is open). We keep the cached deviceId
  // around because the browser may reassign ids on re-enumeration;
  // if the previous device is gone the manager falls back to the
  // system default automatically.
  useEffect(() => {
    const out = audioOutputRef.current;
    if (!out) return;
    const stored = getStoredOutputDeviceId();
    if (stored) {
      void out.setSinkId(stored).catch(() => {
        /* sink missing — fall back to default */
      });
    }
    return onDeviceListChange(() => {
      const next = getStoredOutputDeviceId();
      if (next) {
        void out.setSinkId(next).catch(() => {
          /* ignore */
        });
      }
    });
  }, []);

  // Pre-warm the AudioContext on the first user gesture. Creating an
  // AudioContext blocks the main thread for 50-150 ms (sample-rate
  // negotiation, sink probing) and used to land on the very first
  // ``audio_start`` frame — which was almost always a short filler
  // ("Okay, okay,", "Let me see —"). The avatar's render loop
  // would stutter visibly during that one clip even though every
  // subsequent clip ran smooth. Doing the work eagerly here pays
  // the cost during a typed keystroke or a mouse click instead.
  // Browser autoplay policies require the gesture anyway, so this
  // also unlocks ``ctx.resume()`` for the first incoming clip.
  useEffect(() => {
    const out = audioOutputRef.current;
    if (!out) return;
    let warmed = false;
    const warmUp = () => {
      if (warmed) return;
      warmed = true;
      void out.resume().catch(() => {
        /* still locked — try again on the next gesture */
        warmed = false;
      });
      detach();
    };
    const events: (keyof WindowEventMap)[] = [
      "pointerdown",
      "keydown",
      "touchstart",
    ];
    const detach = () => {
      for (const evt of events) {
        window.removeEventListener(evt, warmUp);
      }
    };
    for (const evt of events) {
      window.addEventListener(evt, warmUp, { once: false, passive: true });
    }
    return detach;
  }, []);

  return { send, sendBytes, audioOutput: audioOutputRef.current };
}
