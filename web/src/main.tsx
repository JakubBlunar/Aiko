import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { installGlobalCrashReporters } from "./crashReport";
import { isTauri } from "./desktop/runtime";
import "./index.css";

// Catch event-handler throws + unhandled promise rejections (the
// crashes a React error boundary can't see) and report them to the
// backend for diagnostics. No-op outside the browser; safe to call
// before render.
installGlobalCrashReporters();

const root = document.getElementById("root");
if (!root) {
  throw new Error("Missing #root element in index.html");
}

createRoot(root).render(
  <StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </StrictMode>,
);

// PWA: register the service worker so the app is installable / runnable
// standalone (no browser chrome). Browser-only and production-only — it's
// pointless inside the Tauri shell and would cache dev assets against HMR
// in ``npm run dev``. Vite injects ``import.meta.env.PROD`` at build time;
// the project's tsconfig doesn't pull in ``vite/client`` types so we read
// it through an ``unknown`` cast (same pattern as ``desktop/runtime.ts``).
const viteMeta = import.meta as unknown as { env?: { PROD?: boolean } };
if (
  viteMeta.env?.PROD &&
  !isTauri() &&
  typeof navigator !== "undefined" &&
  "serviceWorker" in navigator
) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/sw.js")
      .catch((err) => console.warn("[pwa] service worker registration failed", err));
  });
}
