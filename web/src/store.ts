import { create } from "zustand";
import { createAgendaSlice } from "./stores/slices/agenda";
import { createAvatarSlice } from "./stores/slices/avatar";
import { createBeliefsSlice } from "./stores/slices/beliefs";
import { createChatSlice } from "./stores/slices/chat";
import { createLayoutSlice } from "./stores/slices/layout";
import { createLlmSlice } from "./stores/slices/llm";
import { createMemorySlice } from "./stores/slices/memory";
import { createMetricsSlice } from "./stores/slices/metrics";
import { createNotificationsSlice } from "./stores/slices/notifications";
import { createSessionSlice } from "./stores/slices/session";
import { createTasksSlice } from "./stores/slices/tasks";
import { createTogetherSlice } from "./stores/slices/together";
import { createUiSlice } from "./stores/slices/ui";
import { createVoiceSlice } from "./stores/slices/voice";
import { createWorldSlice } from "./stores/slices/world";
import type { AppState } from "./stores/types";

/**
 * The single application store. ``store.ts`` is intentionally thin: it
 * composes the per-domain slices under ``stores/slices/`` into one Zustand
 * store and re-exports the public surface consumers already import from
 * ``@/store``. The slice files own the state shape + reducers; this file
 * only wires them together (and stays the compatibility facade while the
 * high-churn slices are extracted into standalone stores in a later phase).
 */
export const useAssistantStore = create<AppState>()((...a) => ({
  ...createSessionSlice(...a),
  ...createMetricsSlice(...a),
  ...createChatSlice(...a),
  ...createVoiceSlice(...a),
  ...createAvatarSlice(...a),
  ...createMemorySlice(...a),
  ...createBeliefsSlice(...a),
  ...createAgendaSlice(...a),
  ...createTasksSlice(...a),
  ...createWorldSlice(...a),
  ...createTogetherSlice(...a),
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
export type { TogetherViewSlice } from "./stores/slices/together";
export type { AppState } from "./stores/types";
