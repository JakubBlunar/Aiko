/**
 * The situation the app was in when it crashed.
 *
 * Two halves, deliberately separated:
 *
 * - **Environment** — build id, viewport, device pixel ratio, Tauri vs
 *   browser, heap usage, uptime. Read straight off the platform here.
 * - **App state** — voice mode, socket status, which view was open, the
 *   session key. That lives in the Zustand store, which this module must
 *   not import: the crash path has to keep working when the store itself
 *   is what broke, and a static import would make ``crashReport`` depend
 *   on half the app. Instead the app *registers* a provider
 *   (:func:`setCrashContextProvider`) and we call it inside a try/catch.
 *
 * The result is a flat string→string map so it survives JSON, log lines,
 * and grep without anyone having to know its shape in advance.
 */

import { isTauri } from "./desktop/runtime";

/** Injected by Vite's ``define`` at build time (see ``vite.config.ts``).
 * Absent under vitest and in any non-Vite consumer, hence the guard in
 * :func:`buildId` — ``typeof`` on an undeclared name is legal JS and
 * yields ``"undefined"`` rather than throwing. */
declare const __APP_BUILD_ID__: string | undefined;

export type CrashContext = Record<string, string>;

/** Git sha + build timestamp, or ``"dev"`` when not built by Vite. */
export function buildId(): string {
  try {
    return typeof __APP_BUILD_ID__ === "string" && __APP_BUILD_ID__
      ? __APP_BUILD_ID__
      : "dev";
  } catch {
    return "dev";
  }
}

type Provider = () => Record<string, unknown>;

let provider: Provider | null = null;

/**
 * Register the app-state half of the context.
 *
 * Called once from ``App``. The provider runs *during* a crash, so it
 * must be cheap and total — read plain values off the store, don't
 * compute, don't touch the network. Anything it throws is swallowed and
 * recorded as ``appState: "(provider failed)"``.
 */
export function setCrashContextProvider(fn: Provider | null): void {
  provider = fn;
}

function num(value: unknown): string | undefined {
  return typeof value === "number" && Number.isFinite(value)
    ? String(Math.round(value))
    : undefined;
}

function put(into: CrashContext, key: string, value: unknown): void {
  if (value === undefined || value === null || value === "") return;
  const text = typeof value === "string" ? value : String(value);
  into[key] = text.length > 200 ? `${text.slice(0, 200)}…` : text;
}

/**
 * Collect everything worth knowing about the moment of the crash.
 * Never throws; a section that fails is simply absent from the result.
 */
export function collectCrashContext(): CrashContext {
  const out: CrashContext = {};

  put(out, "build", buildId());

  try {
    put(out, "uptimeMs", num(performance?.now?.()));
  } catch {
    /* no performance API */
  }

  try {
    if (typeof window !== "undefined") {
      put(out, "viewport", `${window.innerWidth}x${window.innerHeight}`);
      put(out, "dpr", String(window.devicePixelRatio ?? 1));
    }
  } catch {
    /* detached window */
  }

  try {
    put(out, "shell", isTauri() ? "tauri" : "browser");
  } catch {
    put(out, "shell", "unknown");
  }

  try {
    if (typeof navigator !== "undefined") {
      put(out, "online", String(navigator.onLine));
      put(out, "cores", num((navigator as { hardwareConcurrency?: number }).hardwareConcurrency));
      put(out, "deviceMemoryGb", num((navigator as { deviceMemory?: number }).deviceMemory));
      put(out, "language", navigator.language);
    }
  } catch {
    /* navigator unavailable */
  }

  try {
    // Chrome-only, and exactly what you want when the suspicion is "the
    // tab ran out of memory" — which on a phone is a real cause of the
    // renderer being killed mid-frame.
    const mem = (performance as unknown as {
      memory?: { usedJSHeapSize?: number; jsHeapSizeLimit?: number };
    }).memory;
    if (mem) {
      const used = mem.usedJSHeapSize;
      const limit = mem.jsHeapSizeLimit;
      if (typeof used === "number") {
        put(out, "heapUsedMb", String(Math.round(used / 1048576)));
      }
      if (typeof used === "number" && typeof limit === "number" && limit > 0) {
        put(out, "heapPct", String(Math.round((used / limit) * 100)));
      }
    }
  } catch {
    /* not Chrome */
  }

  try {
    if (typeof document !== "undefined") {
      put(out, "visibility", document.visibilityState);
    }
  } catch {
    /* no document */
  }

  if (provider !== null) {
    try {
      const state = provider();
      if (state && typeof state === "object") {
        for (const [key, value] of Object.entries(state)) {
          put(out, key, value);
        }
      }
    } catch {
      put(out, "appState", "(provider failed)");
    }
  }

  return out;
}

/** Test hook: forget the registered provider. */
export function __resetCrashContextForTests(): void {
  provider = null;
}
