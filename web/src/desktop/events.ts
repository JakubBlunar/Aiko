/**
 * Tauri event-listener shims. Mirrors ``commands.ts``: dynamic import
 * so the ``@tauri-apps/api`` module never enters the browser bundle's
 * hot path, no-op outside of Tauri.
 *
 * The string event names match the constants in
 * ``src-tauri/src/lib.rs`` (``PERSONA_VISIBILITY_EVENT``). Rename one
 * side, rename the other.
 */
import { isTauri } from "./runtime";
import type { ActivityEnvelope } from "../types";

export const PERSONA_VISIBILITY_EVENT = "persona-visibility";
export const WINDOW_VISIBILITY_EVENT = "window-visibility";
export const ACTIVITY_SAMPLE_EVENT = "activity://sample";

type Unlisten = () => void;

/**
 * Subscribe to the ``persona-visibility`` Tauri event. The provided
 * handler receives a ``boolean`` payload — ``true`` when the persona
 * window has just been shown, ``false`` when it has just been hidden.
 *
 * Returns a teardown function. Outside of a Tauri webview the
 * subscription is a no-op and the teardown is a no-op too, so callers
 * can store the result without branching.
 */
export async function listenPersonaVisibility(
  handler: (visible: boolean) => void,
): Promise<Unlisten> {
  if (!isTauri()) {
    return () => {};
  }
  try {
    const mod = await import("@tauri-apps/api/event");
    const unlisten = await mod.listen<boolean>(
      PERSONA_VISIBILITY_EVENT,
      (event) => {
        handler(Boolean(event.payload));
      },
    );
    return unlisten;
  } catch (err) {
    console.warn(
      `[desktop] failed to subscribe to ${PERSONA_VISIBILITY_EVENT}`,
      err,
    );
    return () => {};
  }
}

/**
 * Subscribe to the ``window-visibility`` Tauri event for *this* webview
 * window. The Rust side emits it (via ``emit_to(label, ...)``) whenever
 * the current window is shown or hidden through any of the tray /
 * top-bar / close-button paths, so a webview that has been closed to
 * the tray can pause its avatar render loops. ``true`` = now visible.
 *
 * Delivered through the global ``listen`` channel because ``emit_to``
 * routes by window label — only the targeted window's JS context sees
 * the event (mirrors the ``presence-hide`` wiring in
 * ``usePresenceReporter``). Returns a no-op teardown outside Tauri.
 */
export async function listenWindowVisibility(
  handler: (visible: boolean) => void,
): Promise<Unlisten> {
  if (!isTauri()) {
    return () => {};
  }
  try {
    const mod = await import("@tauri-apps/api/event");
    const unlisten = await mod.listen<boolean>(
      WINDOW_VISIBILITY_EVENT,
      (event) => {
        handler(Boolean(event.payload));
      },
    );
    return unlisten;
  } catch (err) {
    console.warn(
      `[desktop] failed to subscribe to ${WINDOW_VISIBILITY_EVENT}`,
      err,
    );
    return () => {};
  }
}

/**
 * Best-effort synchronous-ish probe of the current webview window's
 * visibility, used to seed the initial render-active state before the
 * first ``window-visibility`` event lands (e.g. the persona window
 * boots hidden and must start suspended). Resolves ``true`` outside
 * Tauri and on any failure so the browser path always renders.
 */
export async function getCurrentWindowVisible(): Promise<boolean> {
  if (!isTauri()) {
    return true;
  }
  try {
    const mod = await import("@tauri-apps/api/webviewWindow");
    const win = mod.getCurrentWebviewWindow();
    return Boolean(await win.isVisible());
  } catch (err) {
    console.warn("[desktop] failed to probe window visibility", err);
    return true;
  }
}

/**
 * Subscribe to collector envelopes. JS is a dumb pipe: listen and
 * forward, never await an OS poll. No-op outside Tauri.
 */
export async function listenActivitySample(
  handler: (envelope: ActivityEnvelope) => void,
): Promise<Unlisten> {
  if (!isTauri()) {
    return () => {};
  }
  try {
    const mod = await import("@tauri-apps/api/event");
    const unlisten = await mod.listen<ActivityEnvelope>(
      ACTIVITY_SAMPLE_EVENT,
      (event) => {
        if (event.payload && typeof event.payload === "object") {
          handler(event.payload);
        }
      },
    );
    return unlisten;
  } catch (err) {
    console.warn(
      `[desktop] failed to subscribe to ${ACTIVITY_SAMPLE_EVENT}`,
      err,
    );
    return () => {};
  }
}
