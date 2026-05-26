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

export const PERSONA_VISIBILITY_EVENT = "persona-visibility";

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
