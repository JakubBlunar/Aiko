/**
 * Tiny localStorage boolean helpers shared by the layout + avatar slices.
 * ``localStorage`` access is wrapped in ``try/catch`` because some
 * environments (incognito, restricted embeds, SSR) make it throw on
 * access -- callers just fall back to defaults rather than crashing.
 */
export function readBool(key: string, fallback: boolean): boolean {
  try {
    const raw = localStorage.getItem(key);
    if (raw == null) return fallback;
    if (raw === "1" || raw === "true") return true;
    if (raw === "0" || raw === "false") return false;
    return fallback;
  } catch {
    return fallback;
  }
}

export function writeBool(key: string, value: boolean): void {
  try {
    localStorage.setItem(key, value ? "1" : "0");
  } catch {
    // Storage quota / permissions / SSR -- not worth surfacing.
  }
}
