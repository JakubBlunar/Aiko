/**
 * Activity reporter — desktop-only, opt-in. Polls the Tauri shell for
 * the foreground application name every few seconds and forwards
 * changes (and only changes — same-app polls don't emit) to the
 * backend so Aiko can naturally reference what the user is doing.
 *
 * **Privacy posture (read this before tweaking):**
 *
 *  - **Browser shells are an absolute no-op.** The hook bails out at
 *    the top when ``isTauri()`` is false. There is no signal source
 *    in the browser anyway; this guarantees we never make spurious
 *    WS frames or invoke calls from a regular tab.
 *  - **App name only.** The Tauri ``get_active_app`` command never
 *    reads ``w.title`` — see ``web/src-tauri/src/lib.rs``. Adding
 *    titles would leak bank URLs / file names; deferred.
 *  - **Self-app filter.** When the user is in our own window, the
 *    backend would otherwise be told "Jacob is in Aiko" — which is
 *    confusing and useless. We coerce that case to ``null`` here so
 *    the inner-life block silently skips.
 *  - **Server-side defence.** Even if this hook is somehow tricked
 *    into emitting events while ``activity_awareness_enabled`` is
 *    false, the ``set_user_active_app`` setter on
 *    ``SessionController`` drops them. Belt + braces.
 *  - **No persistence.** Only the latest single string lives in
 *    backend memory and disappears on restart.
 */
import { useEffect, useRef } from "react";
import { desktop } from "../desktop/commands";
import { isTauri } from "../desktop/runtime";
import { useAssistantStore } from "../store";
import type { WsClientCommand } from "../types";

const POLL_INTERVAL_MS = 5_000;

// Self-app names we should coerce to ``null``. The Tauri bundle's
// ``productName`` is ``Aiko`` (see ``web/src-tauri/tauri.conf.json``);
// keep both casings in case a platform reports the executable name
// differently (Linux X11 sometimes returns the lowercase exec name).
// Update this list if the bundle is renamed.
const SELF_APP_NAMES = new Set<string>(["aiko", "aiko-desktop"]);

/**
 * Normalise an active-app value reported by the Tauri ``get_active_app``
 * command. Strips trailing ``.exe`` (so Windows reports collapse with
 * macOS / Linux), trims whitespace, and coerces self-app matches to
 * ``null``. Exported for unit tests.
 */
export function normaliseActiveAppName(raw: string | null): string | null {
  if (raw === null) return null;
  const trimmed = raw.trim();
  if (!trimmed) return null;
  const stripped = trimmed.replace(/\.exe$/i, "");
  if (SELF_APP_NAMES.has(stripped.toLowerCase())) {
    return null;
  }
  return stripped;
}

type SendCommand = (cmd: WsClientCommand) => void;

interface UseActivityReporterOptions {
  send: SendCommand;
  /** Mirrors the settings toggle. ``false`` makes this hook a complete
   * no-op (no polling, no invoke, no WS traffic). Toggling at runtime
   * starts / stops the loop without a reload. */
  enabled: boolean;
}

const normaliseAppName = normaliseActiveAppName;

export function useActivityReporter(options: UseActivityReporterOptions): void {
  const { send, enabled } = options;
  const sendRef = useRef<SendCommand>(send);
  useEffect(() => {
    sendRef.current = send;
  }, [send]);

  const setLiveActiveApp = useAssistantStore((s) => s.setLiveActiveApp);

  useEffect(() => {
    // Browser path: never poll, never invoke. The settings toggle UI
    // still renders so the user knows the feature exists, but on a
    // browser there's nothing to forward.
    if (!isTauri()) return;
    if (!enabled) {
      // Make sure the live readout in settings clears the moment the
      // toggle flips off, even if the next poll hasn't fired yet.
      setLiveActiveApp(null);
      // Also tell the backend to drop its cached value so the next
      // prompt doesn't surface a stale "Jacob is in <App>" line.
      try {
        sendRef.current({ type: "user_activity", app: null });
      } catch {
        /* ignore — backend will drop on its own when it learns the
           toggle is off, this is just a fast-path nudge */
      }
      return;
    }

    let cancelled = false;
    let timer: number | null = null;
    let lastSent: string | null | undefined = undefined;

    const tick = async () => {
      if (cancelled) return;
      let app: string | null = null;
      try {
        const raw = await desktop.getActiveApp();
        app = normaliseAppName(raw);
      } catch (err) {
        console.warn("[activity] get_active_app failed", err);
        app = null;
      }
      if (cancelled) return;
      // Always update the local readout so the settings-drawer
      // "Currently sees: <App>" line stays fresh — even when the
      // value matches the last forwarded one, it's the freshest
      // datum we have.
      setLiveActiveApp(app);
      if (app !== lastSent) {
        lastSent = app;
        try {
          sendRef.current({ type: "user_activity", app });
        } catch (err) {
          console.warn("[activity] send failed", err);
        }
      }
    };

    // Kick off immediately so the user doesn't have to wait the full
    // poll interval before the readout populates.
    void tick();
    timer = window.setInterval(() => void tick(), POLL_INTERVAL_MS);

    return () => {
      cancelled = true;
      if (timer !== null) {
        window.clearInterval(timer);
        timer = null;
      }
    };
  }, [enabled, setLiveActiveApp]);
}
