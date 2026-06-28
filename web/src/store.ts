import { create } from "zustand";
import { createAgendaSlice } from "./stores/slices/agenda";
import { createAvatarSlice } from "./stores/slices/avatar";
import { createBeliefsSlice } from "./stores/slices/beliefs";
import { createChatSlice } from "./stores/slices/chat";
import { createLayoutSlice } from "./stores/slices/layout";
import { createLlmSlice } from "./stores/slices/llm";
import { createMetricsSlice } from "./stores/slices/metrics";
import { createNotificationsSlice } from "./stores/slices/notifications";
import { createSessionSlice } from "./stores/slices/session";
import { createUiSlice } from "./stores/slices/ui";
import { createVoiceSlice } from "./stores/slices/voice";
import type { AppState } from "./stores/types";

/**
 * The core application store. ``store.ts`` is intentionally thin: it
 * composes the per-domain slices under ``stores/slices/`` into one Zustand
 * store and re-exports the public surface consumers already import from
 * ``@/store``. The slice files own the state shape + reducers; this file
 * only wires them together.
 *
 * The four highest-churn domains — tasks, memory, world, together — live in
 * their own standalone stores (``stores/use{Tasks,Memory,World,Together}Store``)
 * so their frequent WS events don't re-run every subscriber of the core
 * store. New high-churn domains should follow that pattern rather than being
 * added here.
 */
export const useAssistantStore = create<AppState>()((...a) => ({
  ...createSessionSlice(...a),
  ...createMetricsSlice(...a),
  ...createChatSlice(...a),
  ...createVoiceSlice(...a),
  ...createAvatarSlice(...a),
  ...createBeliefsSlice(...a),
  ...createAgendaSlice(...a),
  ...createLlmSlice(...a),
  ...createNotificationsSlice(...a),
  ...createUiSlice(...a),
  ...createLayoutSlice(...a),
}));

// Convenience getter without subscribing (used inside the WS hook).
export const getStore = useAssistantStore.getState;

// ── Back-compat public surface ──────────────────────────────────────
// Consumers import these names directly from ``@/store``; keep them
// re-exported from their slice modules so the facade stays stable.
export {
  NOTIFICATION_ARCHIVE_CAP,
  type NotificationEntry,
  type Toast,
  type ToastKind,
} from "./stores/slices/notifications";
export {
  DEFAULT_MOBILE_PERSONA_RECT,
  DEFAULT_PERSONA_PANEL_W,
  MAX_PERSONA_PANEL_W,
  MIN_MOBILE_PERSONA_H,
  MIN_MOBILE_PERSONA_W,
  MIN_PERSONA_PANEL_W,
  type MobilePersonaRect,
} from "./stores/slices/layout";
// Standalone high-churn stores, re-exported so existing ``@/store`` imports
// keep resolving. New code should import these from their own modules.
export { useTasksStore } from "./stores/useTasksStore";
export type { TasksSlice } from "./stores/useTasksStore";
export { useMemoryStore } from "./stores/useMemoryStore";
export type { MemorySlice } from "./stores/useMemoryStore";
export { useWorldStore } from "./stores/useWorldStore";
export type { WorldSlice } from "./stores/useWorldStore";
export { useTogetherStore } from "./stores/useTogetherStore";
export type { TogetherSlice, TogetherViewSlice } from "./stores/useTogetherStore";
export type { AppState } from "./stores/types";
