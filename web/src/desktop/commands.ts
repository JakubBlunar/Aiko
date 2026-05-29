/**
 * Tauri command shims. Safe to call from any context — when not running
 * inside a Tauri webview these no-op (or warn during dev). Keeps the
 * React components blissfully ignorant of which runtime they're in.
 *
 * The string command names match the ``#[tauri::command]`` exports in
 * ``src-tauri/src/lib.rs``. If you rename a command there, rename the
 * matching wrapper here.
 */
import { isTauri } from "./runtime";

interface TauriCoreApi {
  invoke<T = unknown>(cmd: string, args?: Record<string, unknown>): Promise<T>;
}

async function loadInvoke(): Promise<TauriCoreApi["invoke"] | null> {
  if (!isTauri()) return null;
  // Dynamic import so the ``@tauri-apps/api`` module never enters the
  // browser bundle's hot path. The Vite build still tree-shakes it for
  // browser deployments because the dynamic import is reachable only
  // from inside ``isTauri()`` branches.
  try {
    const mod = await import("@tauri-apps/api/core");
    return mod.invoke;
  } catch (err) {
    console.warn("[desktop] failed to load @tauri-apps/api/core", err);
    return null;
  }
}

async function tauriInvoke<T>(
  cmd: string,
  args?: Record<string, unknown>,
): Promise<T | null> {
  const invoke = await loadInvoke();
  if (!invoke) return null;
  try {
    return (await invoke<T>(cmd, args)) ?? null;
  } catch (err) {
    console.warn(`[desktop] invoke("${cmd}") failed`, err);
    return null;
  }
}

export const desktop = {
  /** Show + focus the persona window. No-op in the browser. */
  openPersona: () => tauriInvoke<void>("open_persona"),
  /** Hide the persona window. No-op in the browser. */
  closePersona: () => tauriInvoke<void>("close_persona"),
  /** Synchronous probe used by the main window on mount to seed the
   * "is persona visible right now?" state before the first event
   * lands. Returns ``false`` outside of a Tauri webview. */
  isPersonaVisible: () => tauriInvoke<boolean>("is_persona_visible"),
  /** Reset the persona window to its default size and re-center it
   * on the current monitor. Backs the "Reset window position"
   * button in the settings drawer; primarily a recovery path for
   * "I dragged it offscreen" situations. */
  resetPersonaWindowPosition: () =>
    tauriInvoke<void>("reset_persona_window_position"),
  /** Toggle whether the persona window stays above other apps. */
  setPersonaAlwaysOnTop: (onTop: boolean) =>
    tauriInvoke<void>("set_persona_always_on_top", { onTop }),
  /** Foreground application name (no window titles, no URLs).
   * Returns ``null`` outside of Tauri AND on platforms where the
   * underlying ``active-win-pos-rs`` crate can't resolve a name
   * (Wayland, locked screen, ...). The settings drawer + activity
   * reporter consult the result; the backend never sees an
   * unresolved value. */
  getActiveApp: () => tauriInvoke<string | null>("get_active_app"),
  /** Boot the Python FastAPI backend if it isn't already responding,
   * then wait until ``/api/health`` answers (timeout ~25s).
   *
   * Resolves with ``{ ok: true }`` once the backend is up, or
   * ``{ ok: false, error }`` on failure. Resolves with ``{ ok: true }``
   * unconditionally outside of Tauri because the dev workflow runs the
   * backend separately. Surfacing the error string instead of throwing
   * keeps the React side free to render a friendly "couldn't launch"
   * banner without a try/catch.
   */
  ensureBackendRunning: async (): Promise<{ ok: boolean; error?: string }> => {
    if (!isTauri()) return { ok: true };
    const invoke = await loadInvoke();
    if (!invoke) return { ok: true };
    try {
      await invoke<void>("ensure_backend_running");
      return { ok: true };
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      return { ok: false, error: message };
    }
  },
};
