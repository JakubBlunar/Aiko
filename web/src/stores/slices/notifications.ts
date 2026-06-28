import { isMobileViewport } from "@/hooks/useIsMobile";
import { nextId } from "../ids";
import type { SliceCreator } from "../types";

export type ToastKind = "memory" | "info" | "warning" | "error";

export interface Toast {
  id: string;
  kind: ToastKind;
  text: string;
  /** Wall-clock millis when the toast was created -- used for auto-dismiss. */
  createdAt: number;
  /** How long until auto-dismiss; 0 means sticky. */
  ttlMs: number;
}

/** An archived notification. Same payload as a {@link Toast} minus the
 * transient ``ttlMs`` -- archive entries don't auto-expire, they're kept
 * (capped) until the user clears them. */
export interface NotificationEntry {
  id: string;
  kind: ToastKind;
  text: string;
  createdAt: number;
}

/** Max archived notifications kept in memory (newest-first). */
export const NOTIFICATION_ARCHIVE_CAP = 50;

export interface NotificationsSlice {
  // Toasts (transient corner notifications).
  toasts: Toast[];
  pushToast: (kind: ToastKind, text: string, ttlMs?: number) => void;
  dismissToast: (id: string) => void;
  /** Push every live toast's deadline out by ``deltaMs`` (hover-pause). */
  extendToasts: (deltaMs: number) => void;

  // Notification archive (history of every toast).
  notifications: NotificationEntry[];
  notificationsUnread: number;
  notificationsOpen: boolean;
  openNotifications: () => void;
  closeNotifications: () => void;
  dismissNotification: (id: string) => void;
  clearNotifications: () => void;
}

export const createNotificationsSlice: SliceCreator<NotificationsSlice> = (
  set,
) => ({
  toasts: [],
  // Default toast lifetime. Bumped over time because users couldn't
  // read the longer "Aiko remembered: ..." / memory-merged toasts
  // before they vanished. Hovering the stack now pauses the countdown
  // (see ``extendToasts`` + ``Toasts.tsx``), so this is just the
  // hands-off lifetime. Callers can still pass a shorter ttlMs.
  pushToast: (kind, text, ttlMs = 12000) =>
    set((state) => {
      const id = nextId();
      const createdAt = Date.now();
      const archived = [
        { id, kind, text, createdAt },
        ...state.notifications,
      ].slice(0, NOTIFICATION_ARCHIVE_CAP);
      // Phones suppress the corner popup entirely (it covers the
      // composer + the × is too small to hit reliably) -- the entry
      // still lands in the archive, surfaced via the top-bar bell.
      const showPopup = !isMobileViewport();
      return {
        notifications: archived,
        notificationsUnread: state.notificationsUnread + 1,
        toasts: showPopup
          ? [...state.toasts, { id, kind, text, createdAt, ttlMs }]
          : state.toasts,
      };
    }),
  dismissToast: (id) =>
    set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
  extendToasts: (deltaMs) =>
    set((state) => {
      if (deltaMs <= 0 || state.toasts.length === 0) {
        return {};
      }
      return {
        toasts: state.toasts.map((t) =>
          t.ttlMs > 0 ? { ...t, createdAt: t.createdAt + deltaMs } : t,
        ),
      };
    }),

  notifications: [],
  notificationsUnread: 0,
  notificationsOpen: false,
  openNotifications: () =>
    set({ notificationsOpen: true, notificationsUnread: 0 }),
  closeNotifications: () => set({ notificationsOpen: false }),
  dismissNotification: (id) =>
    set((state) => ({
      notifications: state.notifications.filter((n) => n.id !== id),
    })),
  clearNotifications: () =>
    set({ notifications: [], notificationsUnread: 0 }),
});
