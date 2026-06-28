import type { StateCreator } from "zustand";
import type { AgendaSlice } from "./slices/agenda";
import type { AvatarSlice } from "./slices/avatar";
import type { BeliefsSlice } from "./slices/beliefs";
import type { ChatSlice } from "./slices/chat";
import type { LayoutSlice } from "./slices/layout";
import type { LlmSlice } from "./slices/llm";
import type { MemorySlice } from "./slices/memory";
import type { MetricsSlice } from "./slices/metrics";
import type { NotificationsSlice } from "./slices/notifications";
import type { SessionSlice } from "./slices/session";
import type { TasksSlice } from "./slices/tasks";
import type { TogetherSlice } from "./slices/together";
import type { UiSlice } from "./slices/ui";
import type { VoiceSlice } from "./slices/voice";
import type { WorldSlice } from "./slices/world";

/**
 * The full assistant store shape: the intersection of every slice. Slices
 * compose into a single Zustand store today (``useAssistantStore`` is the
 * compatibility facade); this intersection is what each slice's ``set`` /
 * ``get`` operate against, so cross-slice reads stay type-safe.
 */
export type AppState = SessionSlice &
  MetricsSlice &
  ChatSlice &
  VoiceSlice &
  AvatarSlice &
  MemorySlice &
  BeliefsSlice &
  AgendaSlice &
  TasksSlice &
  WorldSlice &
  TogetherSlice &
  LlmSlice &
  NotificationsSlice &
  UiSlice &
  LayoutSlice;

/** A slice creator typed against the full {@link AppState}. */
export type SliceCreator<T> = StateCreator<AppState, [], [], T>;
