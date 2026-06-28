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
 * Everything here is best-effort and defensive: a crash reporter that
 * throws would be worse than useless, so every path swallows its own
 * errors. The payload builder (:func:`buildCrashReport`) is a pure
 * function so it can be unit-tested in the Node test environment with
 * no DOM.
 */

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

  return {
    message,
    stack: stack || undefined,
    componentStack: input.componentStack ? clip(input.componentStack) : undefined,
    source: input.source,
    url: input.url,
    userAgent: input.userAgent,
    ts: new Date().toISOString(),
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

  window.addEventListener("error", (event: ErrorEvent) => {
    const where =
      event.filename != null && event.filename !== ""
        ? `${event.filename}:${event.lineno ?? "?"}:${event.colno ?? "?"}`
        : undefined;
    reportUiCrash(
      buildCrashReport({
        error: event.error,
        message: event.message || "uncaught error",
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
          message:
            (event.reason && (event.reason as { message?: string }).message) ||
            "unhandled promise rejection",
          source: "unhandledrejection",
          url: typeof location !== "undefined" ? location.href : undefined,
          userAgent:
            typeof navigator !== "undefined" ? navigator.userAgent : undefined,
        }),
      );
    },
  );
}

/** Test hook: reset the per-session dedup + cap state. */
export function __resetCrashReportStateForTests(): void {
  reportCount = 0;
  recentSignatures.clear();
  globalHandlersInstalled = false;
}
