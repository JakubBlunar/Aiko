/**
 * ``useWindowVisible`` — reports whether *this* webview window is
 * currently visible to the user, so per-frame work (notably the Live2D
 * avatar's Pixi render loop + AvatarEngine RAF loops + global cursor
 * poll) can be suspended while the window sits hidden in the tray.
 *
 * Two signals fold together:
 *   - **Tauri window state**: the Rust shell emits ``window-visibility``
 *     to the specific window whenever it is shown/hidden via the tray,
 *     top-bar button, or X-close-to-tray. This is authoritative for the
 *     close-to-tray case, which a Windows ``ShowWindow(SW_HIDE)`` does
 *     NOT reliably surface as a WebView2 ``document.hidden`` transition.
 *   - **Browser visibility**: ``document.visibilityState`` covers OS
 *     minimise / occlusion (which the Rust side never hears about) and
 *     is the only signal available in a plain browser.
 *
 * The result is ``tauriVisible && browserVisible`` — the window is only
 * treated as "active" (worth rendering) when neither signal says it's
 * hidden. Defaults to ``true`` everywhere so first paint always runs.
 */
import { useEffect, useState } from "react";
import { isTauri } from "../desktop/runtime";
import {
  getCurrentWindowVisible,
  listenWindowVisibility,
} from "../desktop/events";

function browserVisible(): boolean {
  if (typeof document === "undefined") return true;
  return document.visibilityState !== "hidden";
}

export function useWindowVisible(): boolean {
  const [active, setActive] = useState<boolean>(true);

  useEffect(() => {
    let cancelled = false;
    // Latest value of each independent signal; the effective state is
    // their AND. Kept in closure vars (not React state) so a change to
    // one doesn't require the other to re-read stale state.
    let tauriVisible = true;
    let docVisible = browserVisible();

    const recompute = () => {
      if (cancelled) return;
      setActive(tauriVisible && docVisible);
    };

    const onVisibilityChange = () => {
      docVisible = browserVisible();
      recompute();
    };
    if (typeof document !== "undefined") {
      document.addEventListener("visibilitychange", onVisibilityChange);
    }

    let unlisten: (() => void) | null = null;
    if (isTauri()) {
      // Seed from the current window state so a webview that booted
      // hidden (the persona window) starts suspended instead of
      // rendering one full frame before the first event lands.
      void getCurrentWindowVisible().then((visible) => {
        if (cancelled) return;
        tauriVisible = visible;
        recompute();
      });
      void listenWindowVisibility((visible) => {
        tauriVisible = visible;
        recompute();
      }).then((fn) => {
        if (cancelled) {
          fn();
        } else {
          unlisten = fn;
        }
      });
    }

    recompute();

    return () => {
      cancelled = true;
      if (typeof document !== "undefined") {
        document.removeEventListener("visibilitychange", onVisibilityChange);
      }
      if (unlisten) unlisten();
    };
  }, []);

  return active;
}
