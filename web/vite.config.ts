import { createLogger, defineConfig, type ProxyOptions } from "vite";
import react from "@vitejs/plugin-react";
import { execFileSync } from "node:child_process";
import path from "node:path";

const BACKEND = "http://127.0.0.1:6275";

// Stamped into the bundle and reported with every UI crash, so a crash
// report from a phone can be tied to the build that produced it.
function buildId(): string {
  const stamp = new Date().toISOString().replace(/\.\d+Z$/, "Z");
  try {
    const sha = execFileSync("git", ["rev-parse", "--short", "HEAD"], {
      stdio: ["ignore", "pipe", "ignore"],
    })
      .toString()
      .trim();
    return sha ? `${sha}-${stamp}` : stamp;
  } catch {
    // Not a git checkout (tarball install, Docker build context) — the
    // timestamp alone still distinguishes one build from the next.
    return stamp;
  }
}

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
  define: {
    __APP_BUILD_ID__: JSON.stringify(buildId()),
  },
  resolve: {
    alias: {
      "@": path.resolve(__dirname, "./src"),
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/api": httpProxy,
      // ``/avatar`` -> the bundled Alexia model files (FastAPI mounts
      // them out of ``data/personas/active/Alexia/`` at runtime).
      "/avatar": httpProxy,
      // ``/persona-text`` -> ``data/persona/`` (self-image text used
      // by the inner-life prompt; nothing to do with the avatar).
      "/persona-text": httpProxy,
      "/ws": wsProxy,
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    // Without this a production crash stack reads ``at Ln
    // (index-a1b2c3.js:48:1203)``, which is unactionable — and the app
    // is most often wrong on a phone, where there are no DevTools to
    // attach. The maps are only fetched by the browser when DevTools is
    // open, and the backend reads them off disk to symbolicate the
    // stacks it records in ``crashlog.txt`` (see
    // ``app/core/infra/sourcemap.py``). Emitting them is what makes UI
    // crash reports worth reading; keep it on.
    sourcemap: true,
  },
});
