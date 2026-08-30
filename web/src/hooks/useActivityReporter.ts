/**
 * Activity reporter — desktop-only, opt-in. Listens for collector
 * envelopes from the Tauri shell and forwards them over WebSocket.
 * JS does not poll, merge, or sessionize.
 *
 * **Privacy posture (read this before tweaking):**
 *
 *  - **Browser shells are an absolute no-op.** The hook bails out at
 *    the top when ``isTauri()`` is false.
 *  - **Titles ride the envelope + allowlist.** Rust may read a title
 *    but only emits it when the app is on the allowlist; Python
 *    re-applies the same list and strips URL-shaped titles before
 *    persist. The prompt still gets the app name only.
 *  - **Self-app filter.** ``Aiko`` / ``aiko-desktop`` coerce to
 *    ``null`` so the inner-life block silently skips.
 *  - **Server-side defence.** Toggle off drops envelopes in ingest.
 *  - **No await of OS polls.** The collector thread emits
 *    ``activity://sample``; this hook never ``invoke``s the poll.
 */
import { useEffect, useRef } from "react";
import { desktop } from "../desktop/commands";
import { listenActivitySample } from "../desktop/events";
import { isTauri } from "../desktop/runtime";
import { useAssistantStore } from "../store";
import type { WsClientCommand } from "../types";

const SELF_APP_NAMES = new Set<string>(["aiko", "aiko-desktop"]);

/**
 * Normalise an active-app value. Strips trailing ``.exe``, trims, and
 * coerces self-app matches to ``null``. Exported for unit tests.
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
  enabled: boolean;
  titleAllowlist: string[];
}

export function useActivityReporter(options: UseActivityReporterOptions): void {
  const { send, enabled, titleAllowlist } = options;
  const sendRef = useRef<SendCommand>(send);
  useEffect(() => {
    sendRef.current = send;
  }, [send]);

  const setLiveActiveApp = useAssistantStore((s) => s.setLiveActiveApp);
  const setLiveActiveTitle = useAssistantStore((s) => s.setLiveActiveTitle);

  useEffect(() => {
    if (!isTauri()) return;
    void desktop.setActivityCollectorConfig(enabled, titleAllowlist);
  }, [enabled, titleAllowlist]);

  useEffect(() => {
    if (!isTauri()) return;
    if (!enabled) {
      setLiveActiveApp(null);
      setLiveActiveTitle(null);
      try {
        sendRef.current({ type: "user_activity", app: null });
      } catch {
        /* backend drops on its own when the toggle is off */
      }
      return;
    }

    let cancelled = false;
    let unlisten: (() => void) | null = null;

    void listenActivitySample((envelope) => {
      if (cancelled) return;
      const rawApp = envelope.subject?.app ?? null;
      const app = normaliseActiveAppName(
        rawApp === null || rawApp === undefined ? null : String(rawApp),
      );
      const title = envelope.subject?.title
        ? String(envelope.subject.title)
        : null;
      setLiveActiveApp(app);
      setLiveActiveTitle(title);
      try {
        sendRef.current({ type: "user_activity", envelope });
      } catch (err) {
        console.warn("[activity] send failed", err);
      }
    }).then((fn) => {
      if (cancelled) {
        fn();
        return;
      }
      unlisten = fn;
    });

    return () => {
      cancelled = true;
      if (unlisten) unlisten();
    };
  }, [enabled, setLiveActiveApp, setLiveActiveTitle]);
}
