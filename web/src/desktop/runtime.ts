/**
 * Desktop runtime helpers — figure out whether the bundle is loaded inside
 * a Tauri webview and, if so, which URL to use when calling the Python
 * backend.
 *
 * The browser case is unchanged: same-origin URLs work because either
 *   - the Vite dev server proxies ``/api`` / ``/ws`` / ``/avatar`` to the
 *     FastAPI backend on ``127.0.0.1:6275``, or
 *   - in production, FastAPI itself serves the built ``web/dist`` bundle.
 *
 * The Tauri case needs absolute URLs because the webview origin
 * (``tauri://localhost`` on Windows / Linux, ``tauri.localhost`` on
 * macOS) is NOT the FastAPI origin. The default backend URL points at
 * ``http://127.0.0.1:6275`` to match the FastAPI listener configured in
 * ``config/default.json``. Override at build time via
 * ``VITE_BACKEND_URL`` if you run the Python server on a different host
 * or port (e.g. when developing against a remote machine).
 */

const DEFAULT_TAURI_BACKEND = "http://127.0.0.1:6275";

export interface BackendBase {
  /** Origin to use for ``fetch(...)`` calls. Includes scheme + host + port,
   * no trailing slash. */
  http: string;
  /** Origin to use for ``new WebSocket(url)``. ``ws://`` or ``wss://``. */
  ws: string;
}

/** Whether the JS context is hosted inside a Tauri webview. Tauri 2 sets
 * the ``__TAURI_INTERNALS__`` global before the bundle starts running. We
 * intentionally do NOT depend on the ``@tauri-apps/api`` package here —
 * keeping this check tiny and synchronous matters because every WS / REST
 * call resolves through it. */
export function isTauri(): boolean {
  if (typeof window === "undefined") return false;
  return "__TAURI_INTERNALS__" in window;
}

/** Resolve the backend base URLs for the current runtime. Call sites
 * concatenate the path themselves so the browser-vs-desktop branch only
 * lives here. */
export function backendBase(): BackendBase {
  if (isTauri()) {
    // Vite injects ``import.meta.env`` at build time. The project's
    // tsconfig doesn't pull in ``vite/client`` types, so cast through
    // ``unknown`` to read the optional override without polluting the
    // global types or pulling in extra deps.
    const meta = (import.meta as unknown) as {
      env?: { VITE_BACKEND_URL?: string };
    };
    const override = meta.env?.VITE_BACKEND_URL;
    const http = (override && override.trim()) || DEFAULT_TAURI_BACKEND;
    const trimmed = http.replace(/\/$/, "");
    return {
      http: trimmed,
      ws: trimmed.replace(/^http(s?):\/\//, (_match, secure) =>
        secure ? "wss://" : "ws://",
      ),
    };
  }

  // Browser: same-origin. ``window.location.origin`` already lacks a
  // trailing slash, which keeps the concatenation pattern uniform.
  if (typeof window === "undefined") {
    return { http: "", ws: "" };
  }
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return {
    http: window.location.origin,
    ws: `${proto}://${window.location.host}`,
  };
}
