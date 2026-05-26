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
  /** Resize the persona window. The settings drawer dispatches this
   * after the user edits the width / height sliders. */
  setPersonaGeometry: (width: number, height: number) =>
    tauriInvoke<void>("set_persona_geometry", { width, height }),
  /** Toggle whether the persona window stays above other apps. */
  setPersonaAlwaysOnTop: (onTop: boolean) =>
    tauriInvoke<void>("set_persona_always_on_top", { onTop }),
};
