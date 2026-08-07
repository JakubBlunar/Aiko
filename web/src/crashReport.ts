/**
 * UI crash reporting.
 *
 * The React error boundary (``ErrorBoundary.tsx``) and the global
 * ``window`` error/rejection listeners funnel through here to POST a
 * compact crash report to ``/api/logs/ui-crash``. That endpoint is
 * **always on** (unlike the opt-in ``/api/logs/ui`` debug bridge in
 * ``log.ts``) so a white-screen crash lands in ``data/app.log`` +
 * ``crashlog.txt`` the next time it happens — the user doesn't have to
 * have turned on "Debug logging" beforehand.
 *
 * What a report carries, and why
 * ------------------------------
 * A bare message + stack is rarely enough to act on, so each report also
 * gathers:
 *
 * - **breadcrumbs** (``crashBreadcrumbs.ts``) — the last ~60 things that
 *   happened, including everything React wrote to ``console.error``.
 *   This is usually what actually identifies the cause.
 * - **context** (``crashContext.ts``) — build id, viewport, shell, heap,
 *   plus app state (voice mode, socket status, open view) via a
 *   registered provider.
 *
 * The stack itself is minified in a production build. It is de-minified
 * **server-side** against the ``.map`` files in ``web/dist/assets``
 * (``app/core/infra/sourcemap.py``), because the crashes that matter
 * happen on a phone with no DevTools attached.
 *
 * Everything here is best-effort and defensive: a crash reporter that
 * throws would be worse than useless, so every path swallows its own
 * errors. The payload builder (:func:`buildCrashReport`) is a pure
 * function so it can be unit-tested in the Node test environment with
 * no DOM.
 */

import {
  addBreadcrumb,
  installConsoleBreadcrumbs,
  snapshotBreadcrumbs,
  type Breadcrumb,
} from "./crashBreadcrumbs";
import { collectCrashContext, type CrashContext } from "./crashContext";
import { backendBase } from "./desktop/runtime";

/** Where the crash came from. ``render`` = caught by the React error
 * boundary; the other two come from the global window listeners. */
export type CrashSource = "render" | "window.onerror" | "unhandledrejection";

export interface UiCrashReport {
  message: string;
  stack?: string;
  componentStack?: string;
  source: CrashSource;
  url?: string;
  userAgent?: string;
  ts: string;
  /** What happened in the seconds before the crash. See
   * ``crashBreadcrumbs.ts``. */
  breadcrumbs?: Breadcrumb[];
  /** Build id + runtime + app state at the moment of the crash. See
   * ``crashContext.ts``. */
  context?: CrashContext;
}

/** Client-side field cap. The server clips again (8 KB) — this is just
 * to avoid shipping a multi-megabyte stack over the wire. */
const MAX_FIELD = 16_000;
/** Hard ceiling on reports per page-load so a tight crash-loop (e.g. a
 * rejected promise firing every frame) can't hammer the backend. */
const MAX_REPORTS_PER_SESSION = 25;
/** Suppress identical signatures seen within this window (ms). */
const DEDUP_WINDOW_MS = 10_000;

let reportCount = 0;
const recentSignatures = new Map<string, number>();

function clip(value: unknown): string {
  const text = value == null ? "" : String(value);
  if (text.length > MAX_FIELD) {
    return `${text.slice(0, MAX_FIELD)}…(+${text.length - MAX_FIELD} more)`;
  }
  return text;
}

/** Build a normalised crash report from a loose input. Pure + total:
 * never throws, always returns a well-formed report with a timestamp. */
export function buildCrashReport(input: {
  error?: unknown;
  message?: string;
  stack?: string;
  componentStack?: string;
  source: CrashSource;
  url?: string;
  userAgent?: string;
}): UiCrashReport {
  const err = input.error;
  const errObj =
    err instanceof Error
      ? err
      : err && typeof err === "object"
        ? (err as { message?: unknown; stack?: unknown })
        : undefined;

  const message = clip(
    input.message ??
      (errObj?.message != null ? String(errObj.message) : undefined) ??
      (typeof err === "string" ? err : "") ??
      "",
  ) || "(no message)";

  const stack = clip(
    input.stack ?? (errObj?.stack != null ? String(errObj.stack) : ""),
  );

  // Snapshot the trail *before* adding this crash to it, so a report
  // never contains itself as its own final breadcrumb.
  const breadcrumbs = snapshotBreadcrumbs();
  const context = collectCrashContext();

  // A crash is itself a breadcrumb: when the first failure cascades into
  // a second one, the follow-up report carries the original in its
  // trail, which is usually the one that explains both.
  addBreadcrumb("crash", `${input.source}: ${message}`);

  return {
    message,
    stack: stack || undefined,
    componentStack: input.componentStack ? clip(input.componentStack) : undefined,
    source: input.source,
    url: input.url,
    userAgent: input.userAgent,
    ts: new Date().toISOString(),
    breadcrumbs: breadcrumbs.length > 0 ? breadcrumbs : undefined,
    context: Object.keys(context).length > 0 ? context : undefined,
  };
}

function crashUrl(): string {
  // Inlined to match ``log.ts`` and avoid coupling the crash path to
  // ``api.ts`` (which itself can be implicated in a crash).
  const base = backendBase().http;
  return base ? `${base}/api/logs/ui-crash` : "/api/logs/ui-crash";
}

function shouldSuppress(report: UiCrashReport): boolean {
  if (reportCount >= MAX_REPORTS_PER_SESSION) return true;
  const signature = `${report.source}|${report.message}`;
  const now = Date.now();
  const seenAt = recentSignatures.get(signature);
  if (seenAt != null && now - seenAt < DEDUP_WINDOW_MS) {
    return true;
  }
  recentSignatures.set(signature, now);
  // Bound the dedup map so a stream of unique messages can't leak.
  if (recentSignatures.size > 64) {
    const oldest = recentSignatures.keys().next().value;
    if (oldest !== undefined) recentSignatures.delete(oldest);
  }
  return false;
}

/** Fire-and-forget POST of a crash report. Deduped + capped + fully
 * swallowed so it's safe to call from a ``componentDidCatch`` or a
 * global error handler. */
export function reportUiCrash(report: UiCrashReport): void {
  try {
    if (shouldSuppress(report)) return;
    reportCount += 1;
    void fetch(crashUrl(), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(report),
      // ``keepalive`` lets the request survive a navigation/reload that
      // a crash often triggers right after.
      keepalive: true,
    }).catch(() => {
      /* backend down / offline — nothing more we can do */
    });
  } catch {
    /* never let the reporter itself throw */
  }
}

let globalHandlersInstalled = false;

/** Install ``window`` error + unhandledrejection listeners that report
 * to the backend. These catch the crashes a React error boundary can't
 * — event-handler throws, async/promise rejections, and errors outside
 * the React tree — purely for diagnostics (no UI change). Idempotent
 * and a no-op outside the browser. */
export function installGlobalCrashReporters(): void {
  if (globalHandlersInstalled || typeof window === "undefined") return;
  globalHandlersInstalled = true;

  // Start the breadcrumb trail first: React's own ``console.error``
  // diagnostics usually land *before* the throw they describe, so
  // installing this after the listeners would miss the useful half.
  installConsoleBreadcrumbs();

  window.addEventListener("error", (event: ErrorEvent) => {
    const where =
      event.filename != null && event.filename !== ""
        ? `${event.filename}:${event.lineno ?? "?"}:${event.colno ?? "?"}`
        : undefined;
    // A failed <img>/<script>/<link> fires a plain Event on the element
    // (no ``message``, no ``error``) rather than an ErrorEvent. Those are
    // worth a breadcrumb — a missing Live2D texture or model file
    // explains a later "cannot read property of undefined" — but they are
    // not themselves crashes, so don't report them as one.
    const target = event.target as { tagName?: string; src?: string; href?: string } | null;
    if (target != null && target !== (window as unknown as typeof target) && target.tagName) {
      addBreadcrumb(
        "resource",
        `failed to load <${String(target.tagName).toLowerCase()}>`,
        target.src ?? target.href,
      );
      return;
    }
    reportUiCrash(
      buildCrashReport({
        error: event.error,
        // Cross-origin scripts report the opaque "Script error." with no
        // ``error`` object; keep the location so it isn't a total dead end.
        message: event.message || (where ? `uncaught error at ${where}` : "uncaught error"),
        source: "window.onerror",
        url: typeof location !== "undefined" ? location.href : where,
        userAgent:
          typeof navigator !== "undefined" ? navigator.userAgent : undefined,
      }),
    );
  });

  window.addEventListener(
    "unhandledrejection",
    (event: PromiseRejectionEvent) => {
      reportUiCrash(
        buildCrashReport({
          error: event.reason,
          message: describeRejection(event.reason),
          source: "unhandledrejection",
          url: typeof location !== "undefined" ? location.href : undefined,
          userAgent:
            typeof navigator !== "undefined" ? navigator.userAgent : undefined,
        }),
      );
    },
  );
}

/** Describe a rejection reason that isn't an ``Error``.
 *
 * ``Promise.reject({status: 500})`` and ``Promise.reject("nope")`` are
 * both common and both used to collapse to the useless "unhandled
 * promise rejection". Anything with a message keeps it; anything else
 * gets summarised rather than discarded. */
function describeRejection(reason: unknown): string {
  try {
    if (reason == null) return "unhandled promise rejection (no reason)";
    if (typeof reason === "string") return reason || "unhandled promise rejection";
    const message = (reason as { message?: unknown }).message;
    if (typeof message === "string" && message) return message;
    if (typeof reason === "object") {
      const rendered = JSON.stringify(reason);
      if (rendered && rendered !== "{}") {
        return `unhandled rejection: ${rendered.slice(0, 200)}`;
      }
      return `unhandled rejection: ${Object.prototype.toString.call(reason)}`;
    }
    return `unhandled rejection: ${String(reason)}`;
  } catch {
    return "unhandled promise rejection";
  }
}

/** Test hook: reset the per-session dedup + cap state. */
export function __resetCrashReportStateForTests(): void {
  reportCount = 0;
  recentSignatures.clear();
  globalHandlersInstalled = false;
}
