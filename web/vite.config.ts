import { createLogger, defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import path from "node:path";

const BACKEND = "http://127.0.0.1:6275";

// Quietly swallow the ECONNREFUSED storm Vite logs when the FastAPI backend
// isn't running yet. The React client auto-reconnects on its own, so we don't
// need to flood the terminal with stack traces every second.
let lastWarning = 0;
const WARN_EVERY_MS = 5000;

function quietProxyError(err: NodeJS.ErrnoException, label: string) {
  const now = Date.now();
  if (
    err.code === "ECONNREFUSED" ||
    err.code === "ECONNRESET" ||
    err.code === "ECONNABORTED" ||
    err.code === "EPIPE"
  ) {
    if (now - lastWarning > WARN_EVERY_MS) {
      lastWarning = now;
      console.warn(
        `[vite] ${label}: backend not reachable on ${BACKEND}. ` +
          "Start it with: python -m app.web",
      );
    }
    return;
  }
  console.error(`[vite] ${label}:`, err.message);
}

// Vite's built-in proxy registers its own socket error handler inside
// `proxyReqWs` that prints a stack trace as "ws proxy socket error:" any time
// a WebSocket upgrade aborts mid-handshake. That happens routinely in dev:
// React StrictMode double-mounts, HMR reloads, the auto-reconnect loop racing
// against a still-warming backend. None of these are actionable, so filter
// the matching lines out of the logger before they hit the terminal.
const filteredLogger = createLogger("info", { allowClearScreen: true });
const originalError = filteredLogger.error.bind(filteredLogger);
filteredLogger.error = (msg, options) => {
  if (
    typeof msg === "string" &&
    /ws proxy socket error/i.test(msg) &&
    /ECONNABORTED|ECONNRESET|EPIPE/i.test(msg)
  ) {
    return;
  }
  originalError(msg, options);
};

const httpProxy: ProxyOptions = {
  target: BACKEND,
  changeOrigin: true,
  configure: (proxy) => {
    proxy.on("error", (err) => quietProxyError(err, "http proxy error"));
  },
};

const wsProxy: ProxyOptions = {
  target: BACKEND.replace("http", "ws"),
  ws: true,
  configure: (proxy) => {
    proxy.on("error", (err) => quietProxyError(err, "ws proxy error"));
  },
};

export default defineConfig({
  plugins: [react()],
  customLogger: filteredLogger,
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": httpProxy,
      "/persona": httpProxy,
      // ``/persona`` and ``/personas`` are mounted as separate static
      // dirs by the FastAPI backend (``data/persona`` for prose persona
      // text, ``data/personas`` for uploaded Live2D models). Vite uses
      // path-segment-aware matching, so ``/persona`` does NOT cover
      // ``/personas/...`` — list both explicitly.
      "/personas": httpProxy,
      "/ws": wsProxy,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
