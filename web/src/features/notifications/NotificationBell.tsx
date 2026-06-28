import { useAssistantStore } from "@/store";

interface NotificationBellProps {
  /** Button classes so each placement (mobile top bar, desktop sidebar)
   * matches its neighbours. The button is ``relative`` internally so the
   * unread badge can anchor to its corner. */
  className?: string;
}

function BellGlyph() {
  return (
    <svg
      viewBox="0 0 20 20"
      className="h-5 w-5"
      fill="none"
      stroke="currentColor"
      strokeWidth={1.5}
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M10 3.2c-2.6 0-4.3 1.9-4.3 4.4 0 3.4-1 4.6-1.6 5.2-.3.3-.1.9.4.9h11c.5 0 .7-.6.4-.9-.6-.6-1.6-1.8-1.6-5.2 0-2.5-1.7-4.4-4.3-4.4Z" />
      <path d="M8.4 16.4a1.8 1.8 0 0 0 3.2 0" />
    </svg>
  );
}

/**
 * Bell button that opens the notification drawer and shows an unread
 * badge. Self-contained: reads ``notificationsUnread`` and calls
 * ``openNotifications`` (which also clears the badge), so call sites only
 * pass styling.
 */
export function NotificationBell({ className }: NotificationBellProps) {
  const unread = useAssistantStore((s) => s.notificationsUnread);
  const open = useAssistantStore((s) => s.openNotifications);
  const label =
    unread > 0 ? `Notifications (${unread} unread)` : "Notifications";
  return (
    <button
      type="button"
      onClick={open}
      aria-label={label}
      title={label}
      className={`relative ${className ?? ""}`}
    >
      <BellGlyph />
      {unread > 0 ? (
        <span
          className="absolute -right-1 -top-1 flex h-4 min-w-4 items-center justify-center rounded-full bg-rose-500 px-1 text-[10px] font-semibold leading-none text-white"
          aria-hidden
        >
          {unread > 9 ? "9+" : unread}
        </span>
      ) : null}
    </button>
  );
}
