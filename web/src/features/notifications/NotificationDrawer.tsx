import { useAssistantStore } from "@/store";
import type { NotificationEntry } from "@/store";

/** Compact relative-time formatter for the archive ("just now", "3m",
 * "2h", "4d") off a wall-clock millis timestamp. */
function relativeTime(ms: number): string {
  const delta = Date.now() - ms;
  if (delta < 45_000) return "just now";
  const mins = Math.round(delta / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.round(delta / 3_600_000);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(delta / 86_400_000);
  return `${days}d ago`;
}

function kindClasses(kind: NotificationEntry["kind"]): string {
  switch (kind) {
    case "memory":
      return "border-emerald-500/40 bg-emerald-950/40 text-emerald-100";
    case "warning":
      return "border-amber-500/40 bg-amber-950/40 text-amber-100";
    case "error":
      return "border-rose-500/40 bg-rose-950/40 text-rose-100";
    default:
      return "border-slate-500/40 bg-slate-900/40 text-slate-100";
  }
}

function kindGlyph(kind: NotificationEntry["kind"]): string {
  switch (kind) {
    case "memory":
      return "✦";
    case "warning":
      return "!";
    case "error":
      return "⚠";
    default:
      return "i";
  }
}

/**
 * Slide-in archive of past notifications. Store-driven (``notificationsOpen``)
 * so the mobile top-bar bell and the desktop sidebar bell can both open it.
 * On phones this is the *only* way notifications surface (the corner popups
 * are suppressed there); on desktop it complements the popups.
 */
export function NotificationDrawer() {
  const open = useAssistantStore((s) => s.notificationsOpen);
  const close = useAssistantStore((s) => s.closeNotifications);
  const notifications = useAssistantStore((s) => s.notifications);
  const dismiss = useAssistantStore((s) => s.dismissNotification);
  const clearAll = useAssistantStore((s) => s.clearNotifications);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-[55] flex justify-end"
      role="dialog"
      aria-modal="true"
      aria-label="Notifications"
    >
      <button
        type="button"
        className="absolute inset-0 bg-black/50 backdrop-blur-sm"
        aria-label="Close notifications"
        onClick={close}
      />
      <aside className="relative flex h-full w-[min(420px,100vw)] flex-col border-l border-white/10 bg-slate-950/95 shadow-2xl">
        <header className="flex items-center justify-between gap-2 border-b border-white/10 px-4 py-3">
          <h2 className="text-sm font-semibold text-ink-100">Notifications</h2>
          <div className="flex items-center gap-2">
            {notifications.length > 0 ? (
              <button
                type="button"
                onClick={clearAll}
                className="rounded-md border border-white/10 px-2 py-1 text-[11px] text-ink-100/70 transition hover:border-rose-400 hover:text-rose-200"
              >
                Clear all
              </button>
            ) : null}
            <button
              type="button"
              onClick={close}
              aria-label="Close"
              className="flex h-8 w-8 items-center justify-center rounded-md border border-white/10 text-ink-100/70 transition hover:border-ink-400 hover:text-ink-100"
            >
              ×
            </button>
          </div>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {notifications.length === 0 ? (
            <p className="mt-8 text-center text-sm text-ink-100/40">
              No notifications yet.
            </p>
          ) : (
            <ul className="space-y-2">
              {notifications.map((n) => (
                <li
                  key={n.id}
                  className={`rounded-lg border px-3 py-2 text-sm ${kindClasses(n.kind)}`}
                >
                  <div className="flex items-start gap-2">
                    <span className="select-none" aria-hidden>
                      {kindGlyph(n.kind)}
                    </span>
                    <div className="min-w-0 flex-1">
                      <p className="whitespace-pre-wrap break-words leading-snug">
                        {n.text}
                      </p>
                      <p className="mt-1 text-[10px] uppercase tracking-wide opacity-50">
                        {relativeTime(n.createdAt)}
                      </p>
                    </div>
                    <button
                      type="button"
                      onClick={() => dismiss(n.id)}
                      className="text-xs opacity-50 transition hover:opacity-100"
                      aria-label="Dismiss notification"
                    >
                      ×
                    </button>
                  </div>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>
    </div>
  );
}
