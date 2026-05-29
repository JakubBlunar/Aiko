/**
 * Presence reporter — folds browser tab visibility AND Tauri window focus
 * into a single boolean and forwards changes to the backend over the
 * websocket. The backend uses this to gate the typed-mode proactive
 * silence timer so a user who alt-tabbed to VS Code never gets a
 * "you've been quiet, want to chat?" nudge while they're heads-down
 * in another app.
 *
 * Two complementary signals fold client-side:
 *   - **Browser**: ``document.visibilityState === "visible"`` AND
 *     ``document.hasFocus()``. Covers the tab-hidden / tab-blurred
 *     cases the browser ships out of the box.
 *   - **Tauri**: ``tauri://focus`` / ``tauri://blur`` events on the
 *     webview window. Covers the case where the tab is technically
 *     "visible" (no minimisation, no tab switch) but the user
 *     alt-tabbed to a different OS application — the browser's
 *     visibility API stays ``visible`` for that, so without this
 *     signal the typed timer would still fire.
 *
 * Voice mode is intentionally NOT consulted here — the backend's
 * eligibility predicate ignores presence when ``_live_voice_session_active``
 * is true. Reason: a user wearing the mic may legitimately be away
 * from the screen (cooking, walking around) but very much present in
 * the conversation.
 */
import { useEffect, useRef } from "react";
import { isTauri } from "../desktop/runtime";
import type { WsClientCommand } from "../types";

const DEBOUNCE_MS = 500;

type SendCommand = (cmd: WsClientCommand) => void;

interface UsePresenceReporterOptions {
  send: SendCommand;
  /** When ``false``, the hook still wires listeners but never sends the
   * ``presence`` frame. Used during testing to verify wiring without
   * flooding the WS. Defaults to ``true``. */
  enabled?: boolean;
}

/**
 * Pure helper: compute the "is the browser tab actually visible to
 * the user?" boolean. Treats a missing ``document`` (server-side
 * render) as present so an SSR pass doesn't push a falsy value.
 * Exported for unit tests; the hook below uses it directly.
 */
export function computeBrowserPresent(
  doc?: Pick<Document, "visibilityState" | "hasFocus"> | null,
): boolean {
  const target = doc ?? (typeof document !== "undefined" ? document : null);
  if (!target) return true;
  if (target.visibilityState && target.visibilityState !== "visible") {
    return false;
  }
  if (typeof target.hasFocus === "function" && !target.hasFocus()) {
    return false;
  }
  return true;
}

const browserPresent = (): boolean => computeBrowserPresent();

export function usePresenceReporter(options: UsePresenceReporterOptions): void {
  const { send, enabled = true } = options;

  // Stash the latest send so the listeners installed below see fresh
  // closures across re-renders without re-running the effect (which
  // would keep tearing down + re-attaching listeners).
  const sendRef = useRef<SendCommand>(send);
  useEffect(() => {
    sendRef.current = send;
  }, [send]);

  // Track the Tauri focus state separately. Defaults to ``true`` so a
  // freshly-loaded UI that hasn't received the first focus event yet
  // is treated as present (matches the backend's ``_user_present``
  // default and keeps a brand-new typed turn from being silently
  // gated out before the first event lands).
  const tauriFocusedRef = useRef<boolean>(true);
  // Last value we sent over the WS so we can dedupe. Initialised to
  // ``null`` (rather than ``true``) so the first computed value is
  // always pushed — that's the "send once on connect" requirement.
  const lastSentRef = useRef<boolean | null>(null);
  const debounceTimerRef = useRef<number | null>(null);

  useEffect(() => {
    if (!enabled) return;

    const compute = (): boolean => {
      const tauriOk = isTauri() ? tauriFocusedRef.current : true;
      return tauriOk && browserPresent();
    };

    const flush = () => {
      debounceTimerRef.current = null;
      const current = compute();
      if (lastSentRef.current === current) return;
      lastSentRef.current = current;
      try {
        sendRef.current({ type: "presence", visible: current });
      } catch (err) {
        console.warn("[presence] send failed", err);
      }
    };

    const schedule = () => {
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
      }
      debounceTimerRef.current = window.setTimeout(flush, DEBOUNCE_MS);
    };

    // ── Browser listeners ────────────────────────────────────────
    const onVisibility = () => schedule();
    const onFocus = () => schedule();
    const onBlur = () => schedule();
    document.addEventListener("visibilitychange", onVisibility);
    window.addEventListener("focus", onFocus);
    window.addEventListener("blur", onBlur);

    // ── Tauri focus listeners ────────────────────────────────────
    let tauriUnlistenFocus: (() => void) | null = null;
    let tauriUnlistenBlur: (() => void) | null = null;
    let tauriUnlistenHide: (() => void) | null = null;
    let cancelled = false;

    /**
     * Force a ``presence:false`` send right now, no debounce. Used by
     * the ``presence-hide`` Tauri event the Rust side fires *before*
     * the X-close path hides the window. Without this the 500 ms
     * debounce would lose the race against Chromium freezing
     * background webview timers, leaving the backend stuck on the
     * last-known-good ``True`` until the user reopens the window
     * (and the proactive timer would fire in the meantime).
     */
    const flushHidden = () => {
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
      tauriFocusedRef.current = false;
      lastSentRef.current = false;
      try {
        sendRef.current({ type: "presence", visible: false });
      } catch (err) {
        console.warn("[presence] flush-hidden send failed", err);
      }
    };

    if (isTauri()) {
      // Dynamic import keeps the @tauri-apps/api module out of the
      // browser bundle's hot path. The mirror in commands.ts /
      // events.ts uses the same trick.
      void import("@tauri-apps/api/webviewWindow")
        .then(async (mod) => {
          if (cancelled) return;
          try {
            const win = mod.getCurrentWebviewWindow();
            const focusUnlisten = await win.listen<unknown>(
              "tauri://focus",
              () => {
                tauriFocusedRef.current = true;
                schedule();
              },
            );
            const blurUnlisten = await win.listen<unknown>(
              "tauri://blur",
              () => {
                tauriFocusedRef.current = false;
                schedule();
              },
            );
            // App-level event emitted just before the main window
            // hides via the X close. Listened on the app handle
            // (not the window) because Tauri's ``app.emit_to``
            // routes by window label, and the window event channel
            // is for ``tauri://*`` builtins.
            const appMod = await import("@tauri-apps/api/event");
            const hideUnlisten = await appMod.listen<unknown>(
              "presence-hide",
              () => {
                flushHidden();
              },
            );
            if (cancelled) {
              focusUnlisten();
              blurUnlisten();
              hideUnlisten();
              return;
            }
            tauriUnlistenFocus = focusUnlisten;
            tauriUnlistenBlur = blurUnlisten;
            tauriUnlistenHide = hideUnlisten;
            // Seed the initial Tauri focus state directly; the events
            // only fire on transitions and we don't want to block the
            // first send waiting for a transition.
            try {
              const focused = await win.isFocused();
              tauriFocusedRef.current = Boolean(focused);
              schedule();
            } catch {
              /* leave default true */
            }
          } catch (err) {
            console.warn("[presence] tauri focus listeners failed", err);
          }
        })
        .catch((err) => {
          console.warn("[presence] tauri import failed", err);
        });
    }

    // Always send once on mount so the backend has a known initial
    // value (rather than relying on its ``_user_present = True``
    // default which can disagree with reality on a fresh page that
    // was opened in a background tab).
    flush();

    return () => {
      cancelled = true;
      document.removeEventListener("visibilitychange", onVisibility);
      window.removeEventListener("focus", onFocus);
      window.removeEventListener("blur", onBlur);
      if (tauriUnlistenFocus) tauriUnlistenFocus();
      if (tauriUnlistenBlur) tauriUnlistenBlur();
      if (tauriUnlistenHide) tauriUnlistenHide();
      if (debounceTimerRef.current !== null) {
        window.clearTimeout(debounceTimerRef.current);
        debounceTimerRef.current = null;
      }
    };
  }, [enabled]);
}
