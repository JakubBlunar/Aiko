import type { MetricsSnapshot } from "@/types";
import type { SliceCreator } from "../types";

export interface MetricsSlice {
  metrics: MetricsSnapshot;
  setMetrics: (m: MetricsSnapshot) => void;
  /** Shallow-merge a partial metrics snapshot (back-fills like tts_ms). */
  mergeMetrics: (m: MetricsSnapshot) => void;
  /** Last-known context window from /api/metrics or hello/ws. */
  contextWindow: number;
  contextSource: string;
  setContextInfo: (window: number, source: string) => void;
}

export const createMetricsSlice: SliceCreator<MetricsSlice> = (set) => ({
  metrics: {},
  setMetrics: (metrics) => set({ metrics }),
  mergeMetrics: (m) =>
    set((state) => ({ metrics: { ...state.metrics, ...m } })),
  contextWindow: 0,
  contextSource: "fallback",
  setContextInfo: (window, source) =>
    set({ contextWindow: window || 0, contextSource: source || "fallback" }),
});
