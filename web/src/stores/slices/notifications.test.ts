import { beforeEach, describe, expect, it, vi } from "vitest";

// Controllable viewport so we can exercise both the desktop (popup +
// archive) and mobile (archive-only) branches of ``pushToast``.
const h = vi.hoisted(() => ({ mobile: false }));
vi.mock("@/hooks/useIsMobile", () => ({
  isMobileViewport: () => h.mobile,
  useIsMobile: () => h.mobile,
  MOBILE_MAX_WIDTH: 767,
}));

import { useAssistantStore, NOTIFICATION_ARCHIVE_CAP } from "../../store";

/**
 * Notification archive contract.
 *
 *   * Every ``pushToast`` lands in the archive (newest-first) and bumps
 *     the unread counter.
 *   * On desktop it ALSO shows a corner popup; on mobile the popup is
 *     suppressed (archive only) so it can't cover the composer.
 *   * ``openNotifications`` clears the unread badge; ``clear`` /
 *     ``dismiss`` prune the archive.
 */
function reset(): void {
  h.mobile = false;
  useAssistantStore.setState({
    toasts: [],
    notifications: [],
    notificationsUnread: 0,
    notificationsOpen: false,
  });
}

beforeEach(reset);

describe("notification archive", () => {
  it("pushToast archives + bumps unread + (desktop) shows a popup", () => {
    useAssistantStore.getState().pushToast("info", "hello");
    const s = useAssistantStore.getState();
    expect(s.notifications).toHaveLength(1);
    expect(s.notifications[0].text).toBe("hello");
    expect(s.notificationsUnread).toBe(1);
    expect(s.toasts).toHaveLength(1); // desktop popup
  });

  it("newest notification is first", () => {
    useAssistantStore.getState().pushToast("info", "first");
    useAssistantStore.getState().pushToast("warning", "second");
    const ids = useAssistantStore.getState().notifications.map((n) => n.text);
    expect(ids).toEqual(["second", "first"]);
  });

  it("mobile suppresses the popup but still archives", () => {
    h.mobile = true;
    useAssistantStore.getState().pushToast("warning", "on phone");
    const s = useAssistantStore.getState();
    expect(s.notifications).toHaveLength(1);
    expect(s.notificationsUnread).toBe(1);
    expect(s.toasts).toHaveLength(0); // no corner popup on mobile
  });

  it("openNotifications opens the drawer and clears unread", () => {
    useAssistantStore.getState().pushToast("info", "a");
    useAssistantStore.getState().pushToast("info", "b");
    expect(useAssistantStore.getState().notificationsUnread).toBe(2);
    useAssistantStore.getState().openNotifications();
    const s = useAssistantStore.getState();
    expect(s.notificationsOpen).toBe(true);
    expect(s.notificationsUnread).toBe(0);
    // Archive is untouched by opening.
    expect(s.notifications).toHaveLength(2);
  });

  it("dismissNotification removes a single entry", () => {
    useAssistantStore.getState().pushToast("info", "keep");
    useAssistantStore.getState().pushToast("info", "drop");
    const dropId = useAssistantStore
      .getState()
      .notifications.find((n) => n.text === "drop")!.id;
    useAssistantStore.getState().dismissNotification(dropId);
    const texts = useAssistantStore.getState().notifications.map((n) => n.text);
    expect(texts).toEqual(["keep"]);
  });

  it("clearNotifications empties the archive and resets unread", () => {
    useAssistantStore.getState().pushToast("info", "a");
    useAssistantStore.getState().clearNotifications();
    const s = useAssistantStore.getState();
    expect(s.notifications).toHaveLength(0);
    expect(s.notificationsUnread).toBe(0);
  });

  it("archive is capped at NOTIFICATION_ARCHIVE_CAP", () => {
    for (let i = 0; i < NOTIFICATION_ARCHIVE_CAP + 10; i++) {
      useAssistantStore.getState().pushToast("info", `n${i}`);
    }
    expect(useAssistantStore.getState().notifications).toHaveLength(
      NOTIFICATION_ARCHIVE_CAP,
    );
    // Newest survives, oldest evicted.
    expect(useAssistantStore.getState().notifications[0].text).toBe(
      `n${NOTIFICATION_ARCHIVE_CAP + 9}`,
    );
  });
});
