/**
 * Breadcrumbs — the last few things that happened before a crash.
 *
 * A stack tells you *where* the app died; it rarely tells you *why*. The
 * useful context is the twenty seconds beforehand: the socket
 * reconnected, voice mode flipped on, a settings PATCH 500'd, React
 * logged a warning. This is a small always-on ring that records those,
 * and :func:`snapshotBreadcrumbs` attaches them to every crash report.
 *
 * Why not reuse ``debugLog`` (``log.ts``)?
 * ---------------------------------------
 * That ring is **opt-in** — it only records while the "Debug logging"
 * toggle is on, which by construction is off when an unexpected crash
 * happens for the first time. This one is always recording and costs
 * nothing until it's read: no network, no serialisation, no timers,
 * one array push per event into a 60-slot buffer. ``debugLog.log``
 * mirrors into here so all the existing instrumentation is captured too,
 * whether or not the bridge is enabled.
 *
 * Everything here is defensive — a breadcrumb recorder that throws would
 * take down the very render it exists to explain.
 */

/** How many breadcrumbs to keep. Sized so a chatty source (socket
 * frames) can't push out the whole trail within one turn, while the
 * serialised payload stays a few KB. */
const CAPACITY = 60;
/** Per-breadcrumb detail cap — these ride along on a crash POST. */
const MAX_DETAIL = 300;
/** Collapse a breadcrumb repeated inside this window into a count. */
const COALESCE_WINDOW_MS = 1000;

export interface Breadcrumb {
  /** ms since page load, so the trail reads as a relative timeline and
   * doesn't depend on the device clock being right. */
  t: number;
  /** Coarse area: ``ws``, ``voice``, ``api``, ``console``, ``avatar``, … */
  cat: string;
  /** What happened. Short and greppable. */
  msg: string;
  /** Optional one-line detail. Objects are JSON-stringified and clipped. */
  detail?: string;
  /** Present when the same breadcrumb repeated in quick succession. */
  count?: number;
}

const ring: Breadcrumb[] = [];
let installed = false;
/** Originals kept so tests can un-patch; re-patching a patched console
 * would record every crumb twice. */
const patchedConsole = new Map<"error" | "warn", (...args: unknown[]) => void>();

function now(): number {
  try {
    if (typeof performance !== "undefined" && typeof performance.now === "function") {
      return Math.round(performance.now());
    }
  } catch {
    /* fall through */
  }
  return 0;
}

function toDetail(value: unknown): string | undefined {
  if (value == null) return undefined;
  let text: string;
  try {
    if (typeof value === "string") {
      text = value;
    } else if (value instanceof Error) {
      text = `${value.name}: ${value.message}`;
    } else if (typeof value === "object") {
      text = JSON.stringify(value);
    } else {
      text = String(value);
    }
  } catch {
    // Circular structure, a getter that throws, a Proxy that refuses —
    // the type name is still worth more than dropping the crumb.
    try {
      text = Object.prototype.toString.call(value);
    } catch {
      return undefined;
    }
  }
  if (!text) return undefined;
  return text.length > MAX_DETAIL
    ? `${text.slice(0, MAX_DETAIL)}…(+${text.length - MAX_DETAIL})`
    : text;
}

/**
 * Record one breadcrumb. Cheap, bounded, never throws — safe to call
 * from a hot path or from inside an error handler.
 */
export function addBreadcrumb(cat: string, msg: string, detail?: unknown): void {
  try {
    const crumb: Breadcrumb = {
      t: now(),
      cat: String(cat || "app"),
      msg: String(msg || ""),
    };
    const rendered = toDetail(detail);
    if (rendered !== undefined) crumb.detail = rendered;

    // A reconnect loop or a per-frame warning would otherwise flush the
    // whole trail; fold repeats into the previous entry instead.
    const last = ring[ring.length - 1];
    if (
      last !== undefined &&
      last.cat === crumb.cat &&
      last.msg === crumb.msg &&
      last.detail === crumb.detail &&
      crumb.t - last.t < COALESCE_WINDOW_MS
    ) {
      last.count = (last.count ?? 1) + 1;
      last.t = crumb.t;
      return;
    }

    ring.push(crumb);
    if (ring.length > CAPACITY) ring.splice(0, ring.length - CAPACITY);
  } catch {
    /* never let breadcrumb recording break the caller */
  }
}

/** Oldest-first copy of the trail. Safe to call from a crash handler. */
export function snapshotBreadcrumbs(): Breadcrumb[] {
  try {
    return ring.slice();
  } catch {
    return [];
  }
}

/** Number of breadcrumbs currently held. */
export function breadcrumbCount(): number {
  return ring.length;
}

/**
 * Mirror ``console.error`` / ``console.warn`` into the trail.
 *
 * This is the highest-yield source by a wide margin and needs no call
 * sites: React reports hook-order violations, unmounted-setState, key
 * warnings and its own "The above error occurred in …" summary through
 * ``console.error`` — usually *immediately before* the throw that
 * white-screens the app. The original console method is always called,
 * so DevTools behaviour is unchanged.
 *
 * Idempotent, and a no-op outside the browser.
 */
export function installConsoleBreadcrumbs(): void {
  if (installed || typeof console === "undefined") return;
  installed = true;

  for (const level of ["error", "warn"] as const) {
    const original = console[level];
    if (typeof original !== "function") continue;
    patchedConsole.set(level, original);
    console[level] = function patched(...args: unknown[]): void {
      try {
        const [first, ...rest] = args;
        addBreadcrumb(
          "console",
          `${level}: ${toDetail(first) ?? ""}`.slice(0, MAX_DETAIL),
          rest.length > 0 ? rest.map((a) => toDetail(a) ?? "").join(" ") : undefined,
        );
      } catch {
        /* keep going — the real console call is what matters */
      }
      original.apply(console, args as never[]);
    };
  }
}

/** Test hook: empty the ring and restore the original console methods. */
export function __resetBreadcrumbsForTests(): void {
  ring.length = 0;
  installed = false;
  if (typeof console !== "undefined") {
    for (const [level, original] of patchedConsole) {
      console[level] = original as typeof console.error;
    }
  }
  patchedConsole.clear();
}

export const BREADCRUMB_CAPACITY = CAPACITY;
