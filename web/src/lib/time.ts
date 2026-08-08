/**
 * Shared time / duration formatters. Consolidates the copies that had
 * drifted across ContextBadge, DiagnosticsSection, SettingsSection,
 * SessionSidebar, and NotificationDrawer.
 *
 * Absolute dates render as ``dd.mm.yyyy`` and clock times as 24-hour
 * ``HH:MM`` everywhere in the app. The parts are assembled by hand rather
 * than via ``toLocaleDateString``: the runtime locale is whatever the OS
 * says, so the same build showed ``7/10/2026`` here and ``10.7.2026``
 * elsewhere -- and a day-first reader can't tell ``7/10`` from ``10/7``.
 * Even asking for ``de-DE`` wouldn't do, since it drops the leading zero
 * (``8.8.2026``) and the widths then jitter in the mono columns these
 * strings sit in.
 */

type DateInput = string | number | Date | null | undefined;

/**
 * Parse anything the API hands us into a Date, or ``null``.
 *
 * Backend stamps are ISO 8601, but with mixed precision: most carry
 * whole seconds while some are raw SQLite values with microseconds
 * (``…:25.371206+00:00``). ``Date`` tolerates the extra digits, so both
 * shapes go through the same path.
 */
function toDate(value: DateInput): Date | null {
  if (value == null || value === "") return null;
  const d =
    value instanceof Date
      ? value
      : new Date(typeof value === "number" ? value : String(value));
  return Number.isNaN(d.getTime()) ? null : d;
}

const pad = (n: number): string => String(n).padStart(2, "0");

/** Local calendar date as ``dd.mm.yyyy``. */
export function formatDate(value: DateInput, fallback = ""): string {
  const d = toDate(value);
  if (!d) return fallback;
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)}.${d.getFullYear()}`;
}

/** Local clock time as 24-hour ``HH:MM``. */
export function formatClock(value: DateInput, fallback = ""): string {
  const d = toDate(value);
  if (!d) return fallback;
  return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** ``dd.mm.yyyy HH:MM`` -- the date with its time, when both matter. */
export function formatDateTime(value: DateInput, fallback = ""): string {
  const d = toDate(value);
  if (!d) return fallback;
  return `${formatDate(d)} ${formatClock(d)}`;
}

/**
 * ``Saturday, 08.08.2026`` for the date headings that group a list by day.
 * The weekday is worth its width in a diary or timeline; the numeric part
 * stays in the app-wide format. Weekday names follow the runtime locale.
 */
export function formatDayHeading(value: DateInput, fallback = ""): string {
  const d = toDate(value);
  if (!d) return fallback;
  let weekday = "";
  try {
    weekday = d.toLocaleDateString(undefined, { weekday: "long" });
  } catch {
    weekday = "";
  }
  return weekday ? `${weekday}, ${formatDate(d)}` : formatDate(d);
}

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
