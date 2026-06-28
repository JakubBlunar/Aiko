import { useEffect } from "react";
import { useMicCapture } from "@/hooks/useMicCapture";
import { usePresenceReporter } from "@/hooks/usePresenceReporter";
import { useAssistantStore } from "@/store";
import { desktop } from "@/desktop/commands";
import type { WsClientCommand } from "@/types";
import { Live2DAvatar } from "@/features/avatar/Live2DAvatar";
import { MicButton } from "@/features/voice/MicButton";
import { PersonaActionBanner } from "./PersonaActionBanner";
import { PersonaInput } from "./PersonaInput";
import { PersonaTaskBanner } from "./PersonaTaskBanner";

interface PersonaWindowProps {
  send: (cmd: WsClientCommand) => void;
  sendBytes: (frame: Uint8Array) => void;
}

/** Toggle a ``persona`` class on the document root so the global CSS in
 * ``index.css`` blanks out the gradient background. Pulled into a hook
 * so the cleanup path is explicit when the route flips back to main. */
function usePersonaBackdrop() {
  useEffect(() => {
    const html = document.documentElement;
    html.classList.add("persona");
    return () => {
      html.classList.remove("persona");
    };
  }, []);
}

/**
 * Detached "persona" window — Aiko's avatar plus a minimum-viable HUD
 * (drag handle, mic toggle, single-line composer, close button). Loaded
 * by the Tauri shell at ``index.html#/persona`` in a transparent,
 * frameless, always-on-top webview.
 *
 * State sync between this window and the main window is implicit: both
 * connect to the same Python backend over WebSocket and receive the
 * same broadcast events. There is no inter-window direct messaging.
 */
export function PersonaWindow({ send, sendBytes }: PersonaWindowProps) {
  usePersonaBackdrop();
  const avatar = useAssistantStore((s) => s.avatar);
  const voiceMode = useAssistantStore((s) => s.voiceMode);
  const audioLevel = useAssistantStore((s) => s.audioLevel);
  const connection = useAssistantStore((s) => s.connection);
  const turnInProgress = useAssistantStore((s) => s.turnInProgress);
  const ttsState = useAssistantStore((s) => s.ttsState);
  const clientId = useAssistantStore((s) => s.clientId);
  const voiceOwnerId = useAssistantStore((s) => s.voiceOwnerId);
  const companionSettings = useAssistantStore((s) => s.companionSettings);
  const remotelyOwned = Boolean(
    voiceOwnerId && clientId && voiceOwnerId !== clientId,
  );

  useMicCapture({ sendBytes });

  // Report presence from the persona window too. Without this the
  // backend's per-client presence fold (see ``_visible_by_client`` in
  // ``app/web/server.py``) would treat the persona-only sessions as
  // "never reported" and the boot default would dominate. Each window
  // ships its own visibility frame; the hub OR-folds them so as long
  // as *any* window is visible+focused, ``_user_present`` is true.
  usePresenceReporter({ send });

  const connected = connection.status === "connected";

  const onMicToggle = () => {
    if (!connected) return;
    if (voiceMode === "off") {
      send({ type: "voice_start" });
    } else {
      send({ type: "voice_stop" });
    }
  };

  const onSend = (text: string) => {
    send({ type: "chat", text });
  };

  // Header label mirrors the main window's status line so the user can
  // tell at a glance whether Aiko is listening or speaking without
  // looking back at the chat panel.
  const headerLabel =
    voiceMode !== "off"
      ? voiceMode
      : ttsState === "speaking"
        ? "speaking"
        : "idle";

  return (
    <div className="persona-window flex h-screen w-screen flex-col overflow-hidden bg-transparent">
      {/* Drag handle. ``data-tauri-drag-region`` makes the entire strip
          draggable in the Tauri webview; in a regular browser it does
          nothing (the attribute is silently ignored by the DOM). The
          background is a translucent pill so the user can see WHERE to
          grab even on a transparent window. */}
      <div
        data-tauri-drag-region
        className="flex items-center gap-2 rounded-b-lg bg-black/35 px-3 py-1.5 text-xs text-ink-100/60 backdrop-blur"
      >
        <span data-tauri-drag-region className="font-medium uppercase tracking-[0.2em]">
          aiko
        </span>
        <span data-tauri-drag-region className="text-ink-100/40">
          · {headerLabel}
        </span>
        <span data-tauri-drag-region className="ml-auto" />
        <button
          type="button"
          onClick={() => desktop.closePersona()}
          aria-label="Close persona window"
          className="flex h-5 w-5 items-center justify-center rounded text-ink-100/50 hover:bg-white/10 hover:text-ink-100"
        >
          ×
        </button>
      </div>

      <div className="relative flex flex-1 min-h-0 items-center justify-center">
        {avatar && avatar.loaded ? (
          <Live2DAvatar manifest={avatar} />
        ) : (
          <div className="text-center text-[11px] text-ink-100/40">
            {connected ? "loading avatar..." : "connecting..."}
          </div>
        )}
        {/* K31 + K32: transient gesture banner. Sits absolutely
            positioned over the avatar so it never displaces the rig.
            Master switch + visibility lifetime threaded from the
            companion settings snapshot (WS hello +
            ``companion_settings_changed``); defaults to enabled / 20s
            when no snapshot has arrived yet. */}
        <PersonaActionBanner
          enabled={companionSettings?.persona_touch_banner_enabled ?? true}
          durationMs={
            (companionSettings?.persona_touch_banner_duration_seconds ?? 20) *
            1000
          }
        />
        {/* Chunk 15: persona-window mirror of ``TaskStrip``.
            Surfaces an ``awaiting_input`` task as a transient
            pill so the user can answer / cancel without
            switching back to the chat window. Layered BELOW the
            touch banner (``top-24`` vs ``top-12``) so the two
            never overlap when both happen to be visible. */}
        <PersonaTaskBanner />
      </div>

      <div className="flex shrink-0 items-center gap-2 rounded-t-lg bg-black/40 px-2 py-2 backdrop-blur">
        <MicButton
          voiceMode={voiceMode}
          audioLevel={audioLevel}
          connected={connected}
          onClick={onMicToggle}
          size="compact"
          remotelyOwned={remotelyOwned}
        />
        <PersonaInput onSend={onSend} connected={connected} busy={turnInProgress} />
      </div>
    </div>
  );
}
