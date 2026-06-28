import type { StateCreator } from "zustand";
import type { AgendaSlice } from "./slices/agenda";
import type { AvatarSlice } from "./slices/avatar";
import type { BeliefsSlice } from "./slices/beliefs";
import type { ChatSlice } from "./slices/chat";
import type { LayoutSlice } from "./slices/layout";
import type { LlmSlice } from "./slices/llm";
import type { MetricsSlice } from "./slices/metrics";
import type { NotificationsSlice } from "./slices/notifications";
import type { SessionSlice } from "./slices/session";
import type { UiSlice } from "./slices/ui";
import type { VoiceSlice } from "./slices/voice";

/**
 * The core assistant store shape: the intersection of every slice composed
 * into ``useAssistantStore``. This is what each slice's ``set`` / ``get``
 * operate against, so cross-slice reads stay type-safe.
 *
 * The high-churn domains (tasks, memory, world, together) are intentionally
 * NOT part of this intersection — they live in standalone stores
 * (``stores/use*Store.ts``).
 */
export type AppState = SessionSlice &
  MetricsSlice &
  ChatSlice &
  VoiceSlice &
  AvatarSlice &
  BeliefsSlice &
  AgendaSlice &
  LlmSlice &
  NotificationsSlice &
  UiSlice &
  LayoutSlice;

/** A slice creator typed against the full {@link AppState}. */
export type SliceCreator<T> = StateCreator<AppState, [], [], T>;
