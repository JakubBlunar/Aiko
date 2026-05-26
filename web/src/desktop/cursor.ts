/**
 * Tauri cursor + window-geometry wrappers used by the global gaze
 * pipeline. Mirrors the dynamic-import pattern in
 * ``commands.ts`` / ``events.ts``: outside a Tauri webview every call
 * resolves to ``null`` (or an unsubscribe-shaped no-op), so the same
 * code path can run safely in a regular browser tab.
 *
 * The shape returned to the caller is intentionally tiny — just the
 * three numbers ``GlobalMouseSource`` needs to translate global cursor
 * coordinates back into window-relative ones. Anyone reaching for the
 * full ``@tauri-apps/api`` surface should import that directly.
 */
import { isTauri } from "./runtime";

export interface CursorPoint {
  /** Global cursor X in physical screen pixels (Tauri returns
   * ``PhysicalPosition`` from ``cursorPosition()``). */
  x: number;
  /** Global cursor Y in physical screen pixels. */
  y: number;
}

export interface WindowGeometryPhysical {
  /** Top-left of the webview's client area in physical screen pixels.
   * For a frameless persona window this is the same as ``outerPosition``;
   * for a decorated main window it skips the title-bar / chrome
   * offset, which is exactly what we want for translating cursor
   * coordinates into a coord space the avatar's DOM container lives in. */
  innerX: number;
  innerY: number;
  /** OS scale factor (DPI) for the monitor the window is on. ``1.0``
   * on a non-HiDPI display, ``2.0`` on a typical Retina, etc. The
   * caller divides physical-pixel offsets by this to land in CSS /
   * logical pixels (which is what ``getBoundingClientRect`` returns). */
  scaleFactor: number;
}

type Unlisten = () => void;
const NOOP_UNLISTEN: Unlisten = () => {};

async function loadWindowApi() {
  if (!isTauri()) return null;
  try {
    return await import("@tauri-apps/api/window");
  } catch (err) {
    console.warn("[desktop] failed to load @tauri-apps/api/window", err);
    return null;
  }
}

/** Read the global cursor position in physical screen pixels.
 * Returns ``null`` outside Tauri or when the call throws (e.g. the
 * webview is mid-shutdown and the IPC channel is gone). */
export async function getCursorPositionPhysical(): Promise<CursorPoint | null> {
  const mod = await loadWindowApi();
  if (!mod) return null;
  try {
    const point = await mod.cursorPosition();
    return { x: point.x, y: point.y };
  } catch (err) {
    console.debug("[desktop] cursorPosition() failed", err);
    return null;
  }
}

/** Read the current window's inner-area top-left + scale factor.
 * Returns ``null`` outside Tauri. The caller usually caches this and
 * refreshes only when ``onWindowMoved`` / ``onScaleChanged`` fires. */
export async function getCurrentWindowGeometry(): Promise<WindowGeometryPhysical | null> {
  const mod = await loadWindowApi();
  if (!mod) return null;
  try {
    const win = mod.getCurrentWindow();
    const inner = await win.innerPosition();
    const scale = await win.scaleFactor();
    return { innerX: inner.x, innerY: inner.y, scaleFactor: scale };
  } catch (err) {
    console.debug("[desktop] window geometry probe failed", err);
    return null;
  }
}

/** Subscribe to window-move events on the *current* window. The
 * handler receives no payload; consumers re-fetch geometry via
 * ``getCurrentWindowGeometry``. Returns a teardown that's a no-op
 * outside Tauri. */
export async function onWindowMoved(handler: () => void): Promise<Unlisten> {
  const mod = await loadWindowApi();
  if (!mod) return NOOP_UNLISTEN;
  try {
    const win = mod.getCurrentWindow();
    return await win.onMoved(() => handler());
  } catch (err) {
    console.warn("[desktop] onMoved subscription failed", err);
    return NOOP_UNLISTEN;
  }
}

/** Subscribe to scale-factor changes (e.g. window dragged onto a
 * different-DPI monitor). */
export async function onScaleFactorChanged(
  handler: () => void,
): Promise<Unlisten> {
  const mod = await loadWindowApi();
  if (!mod) return NOOP_UNLISTEN;
  try {
    const win = mod.getCurrentWindow();
    return await win.onScaleChanged(() => handler());
  } catch (err) {
    console.warn("[desktop] onScaleChanged subscription failed", err);
    return NOOP_UNLISTEN;
  }
}

/** Group all four wrappers behind a single object so the
 * ``GlobalMouseSource`` can take a clean dependency for tests to
 * stub. The production export is simply the four async functions
 * above; tests inject a fake. */
export interface CursorApi {
  getCursorPositionPhysical: typeof getCursorPositionPhysical;
  getCurrentWindowGeometry: typeof getCurrentWindowGeometry;
  onWindowMoved: typeof onWindowMoved;
  onScaleFactorChanged: typeof onScaleFactorChanged;
}

export const productionCursorApi: CursorApi = {
  getCursorPositionPhysical,
  getCurrentWindowGeometry,
  onWindowMoved,
  onScaleFactorChanged,
};
