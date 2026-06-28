import { DEFAULT_LOGGING_SETTINGS } from "@/types";
import type { LoggingSettings, ToolEvent } from "@/types";
import type { SliceCreator } from "../types";

export interface UiSlice {
  // Tool activity strip ("Aiko is checking the time / web / notebook").
  toolActivity: ToolEvent[];
  pushToolEvent: (event: ToolEvent) => void;
  clearToolActivity: () => void;

  /** Activity awareness toggle (desktop opt-in). Mirrors the settings
   * drawer's checkbox so the activity reporter hook can start/stop the
   * polling loop without a reload. */
  activityAwarenessEnabled: boolean;
  setActivityAwarenessEnabled: (enabled: boolean) => void;
  /** Last foreground app reported by the activity reporter loop, used
   * solely for the live "Currently sees: <App>" readout. ``null`` covers
   * "couldn't determine" / "in our own window" / "feature disabled". */
  liveActiveApp: string | null;
  setLiveActiveApp: (app: string | null) => void;

  /** Debug-logging bridge knobs (mirrors ``LoggingSettings`` on the
   * backend). Synced on ``hello`` + ``logging_settings_changed``. */
  loggingSettings: LoggingSettings;
  setLoggingSettings: (settings: LoggingSettings) => void;
  patchLoggingSettings: (patch: Partial<LoggingSettings>) => void;
}

export const createUiSlice: SliceCreator<UiSlice> = (set) => ({
  toolActivity: [],
  pushToolEvent: (event) =>
    set((state) => {
      // Keep the strip short -- the latest 8 events are enough context.
      const next = [...state.toolActivity, event];
      const trimmed = next.length > 8 ? next.slice(next.length - 8) : next;
      return { toolActivity: trimmed };
    }),
  clearToolActivity: () => set({ toolActivity: [] }),

  activityAwarenessEnabled: false,
  setActivityAwarenessEnabled: (enabled) =>
    set({ activityAwarenessEnabled: Boolean(enabled) }),
  liveActiveApp: null,
  setLiveActiveApp: (app) => set({ liveActiveApp: app ?? null }),

  loggingSettings: { ...DEFAULT_LOGGING_SETTINGS },
  setLoggingSettings: (settings) =>
    set({ loggingSettings: { ...DEFAULT_LOGGING_SETTINGS, ...settings } }),
  patchLoggingSettings: (patch) =>
    set((state) => ({
      loggingSettings: { ...state.loggingSettings, ...patch },
    })),
});
