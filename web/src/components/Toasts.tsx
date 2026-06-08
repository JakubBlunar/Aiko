import { useEffect, useRef, useState } from "react";
import { useAssistantStore } from "../store";

/**
 * Stacks ephemeral notifications in the bottom-right corner. Auto-dismisses
 * each toast after its `ttlMs` elapses, and lets the user dismiss manually.
 *
 * Hovering the stack pauses every toast's countdown (the deadline is pushed
 * forward by the time spent hovering) so a long "Aiko remembered ..."
 * message can be read at leisure without vanishing mid-sentence.
 *
 * Currently used for the "Aiko remembered ..." memory-added toast, but the
 * shape is generic so we can reuse it for any transient message (e.g.
 * proactive nudge, document-indexed signal).
 */
export function Toasts() {
  const toasts = useAssistantStore((s) => s.toasts);
  const dismissToast = useAssistantStore((s) => s.dismissToast);
  const extendToasts = useAssistantStore((s) => s.extendToasts);

  const [paused, setPaused] = useState(false);
  // Wall-clock when the hover (pause) began, so we can extend every
  // toast's deadline by the hovered duration once the pointer leaves.
  const pausedAtRef = useRef(0);

  // Single shared timer that sweeps the queue every ~250ms. Cheap and
  // avoids spawning per-toast timers that race with re-renders. While
  // paused we skip dismissal entirely (deadlines are extended on leave).
  useEffect(() => {
    if (toasts.length === 0 || paused) {
      return;
    }
    const id = window.setInterval(() => {
      const now = Date.now();
      for (const t of toasts) {
        if (t.ttlMs > 0 && now - t.createdAt >= t.ttlMs) {
          dismissToast(t.id);
        }
      }
    }, 250);
    return () => window.clearInterval(id);
  }, [toasts, dismissToast, paused]);

  if (toasts.length === 0) {
    return null;
  }

  const onEnter = () => {
    pausedAtRef.current = Date.now();
    setPaused(true);
  };
  const onLeave = () => {
    if (pausedAtRef.current > 0) {
      extendToasts(Date.now() - pausedAtRef.current);
      pausedAtRef.current = 0;
    }
    setPaused(false);
  };

  return (
    <div
      className="pointer-events-none fixed bottom-6 right-6 z-50 flex max-h-[calc(100vh-3rem)] w-[min(380px,calc(100vw-3rem))] flex-col gap-2 overflow-y-auto"
      onMouseEnter={onEnter}
      onMouseLeave={onLeave}
    >
      {toasts.map((t) => (
        <div
          key={t.id}
          role="status"
          className={`pointer-events-auto rounded-lg border px-3 py-2 text-sm shadow-lg backdrop-blur transition-opacity duration-300 ${
            t.kind === "memory"
              ? "border-emerald-500/50 bg-emerald-950/80 text-emerald-100"
              : t.kind === "warning"
                ? "border-amber-500/50 bg-amber-950/80 text-amber-100"
                : t.kind === "error"
                  ? "border-rose-500/50 bg-rose-950/80 text-rose-100"
                  : "border-slate-500/50 bg-slate-900/80 text-slate-100"
          }`}
        >
          <div className="flex items-start gap-2">
            <span className="select-none" aria-hidden>
              {t.kind === "memory"
                ? "✦"
                : t.kind === "warning"
                  ? "!"
                  : t.kind === "error"
                    ? "⚠"
                    : "i"}
            </span>
            <span className="flex-1 whitespace-pre-wrap break-words leading-snug">
              {t.text}
            </span>
            <button
              type="button"
              onClick={() => dismissToast(t.id)}
              className="text-xs opacity-60 hover:opacity-100"
              aria-label="Dismiss"
            >
              ×
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}
