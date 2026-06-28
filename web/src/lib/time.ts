/**
 * Shared time / duration formatters. Consolidates the copies that had
 * drifted across ContextBadge, DiagnosticsSection, SettingsSection,
 * SessionSidebar, and NotificationDrawer.
 */

/** Human-readable duration from milliseconds: ``— / 240 ms / 1.42 s``. */
export function fmtMs(value: number | undefined | null): string {
  if (!value) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

/**
 * Compact "X seconds/minutes/hours/days ago" from an ISO timestamp.
 * ``null`` / unparseable input renders as "never" so call sites don't
 * need to guard.
 */
export function formatRelative(iso: string | null | undefined): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "never";
  return relativeFromMillis(t);
}

/**
 * Same as {@link formatRelative} but for an epoch-millis number (the
 * shape NotificationDrawer / toasts carry). Non-finite input renders
 * as "just now".
 */
export function formatRelativeMs(millis: number | null | undefined): string {
  if (millis == null || !Number.isFinite(millis)) return "just now";
  return relativeFromMillis(millis);
}

function relativeFromMillis(millis: number): string {
  const delta = Math.max(0, (Date.now() - millis) / 1000);
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}
