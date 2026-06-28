import { create } from "zustand";
import type {
  RelationshipAxes,
  SharedMoment,
  TogetherSummary,
} from "@/types";

/** State for the "Together" tab. ``page`` is zero-indexed. */
export interface TogetherViewSlice {
  summary: TogetherSummary | null;
  moments: SharedMoment[];
  total: number;
  page: number;
  pageSize: number;
  vibeFilter: string | null;
  loading: boolean;
}

/**
 * Standalone store for the "Together" tab. Extracted from the composed
 * ``useAssistantStore`` (phase 4a) so ``shared_moment_updated`` /
 * ``relationship_axes_updated`` events only re-run the Together tab.
 */
export interface TogetherSlice {
  togetherView: TogetherViewSlice;
  setTogetherSummary: (summary: TogetherSummary | null) => void;
  setSharedMoments: (
    moments: SharedMoment[],
    total: number,
    page: number,
    pageSize: number,
    vibeFilter: string | null,
  ) => void;
  setTogetherLoading: (loading: boolean) => void;
  setTogetherVibeFilter: (vibe: string | null) => void;
  upsertSharedMoment: (moment: SharedMoment) => void;
  removeSharedMoment: (momentId: number) => void;
  setRelationshipAxes: (axes: RelationshipAxes) => void;
}

export const useTogetherStore = create<TogetherSlice>()((set) => ({
  togetherView: {
    summary: null,
    moments: [],
    total: 0,
    page: 0,
    pageSize: 20,
    vibeFilter: null,
    loading: false,
  },
  setTogetherSummary: (summary) =>
    set((state) => ({
      togetherView: { ...state.togetherView, summary },
    })),
  setSharedMoments: (moments, total, page, pageSize, vibeFilter) =>
    set((state) => ({
      togetherView: {
        ...state.togetherView,
        moments,
        total,
        page,
        pageSize,
        vibeFilter,
      },
    })),
  setTogetherLoading: (loading) =>
    set((state) => ({
      togetherView: { ...state.togetherView, loading: Boolean(loading) },
    })),
  setTogetherVibeFilter: (vibe) =>
    set((state) => ({
      togetherView: { ...state.togetherView, vibeFilter: vibe, page: 0 },
    })),
  upsertSharedMoment: (moment) =>
    set((state) => {
      const tv = state.togetherView;
      // Filter mismatch — drop from current page, but bump total.
      if (tv.vibeFilter && moment.vibe !== tv.vibeFilter) {
        const existing = tv.moments.findIndex((m) => m.id === moment.id);
        if (existing >= 0) {
          const next = tv.moments.slice();
          next.splice(existing, 1);
          return {
            togetherView: {
              ...tv,
              moments: next,
              total: Math.max(0, tv.total - 1),
            },
          };
        }
        return state;
      }
      const idx = tv.moments.findIndex((m) => m.id === moment.id);
      if (idx >= 0) {
        const next = tv.moments.slice();
        next[idx] = moment;
        return { togetherView: { ...tv, moments: next } };
      }
      // Insert in the right chronological place (newest first by 'when').
      const next = tv.moments.slice();
      const insertAt = next.findIndex((m) => moment.when > m.when);
      if (insertAt < 0) {
        next.push(moment);
      } else {
        next.splice(insertAt, 0, moment);
      }
      return {
        togetherView: { ...tv, moments: next, total: tv.total + 1 },
      };
    }),
  removeSharedMoment: (momentId) =>
    set((state) => {
      const tv = state.togetherView;
      const idx = tv.moments.findIndex((m) => m.id === momentId);
      if (idx < 0) return state;
      const next = tv.moments.slice();
      next.splice(idx, 1);
      return {
        togetherView: {
          ...tv,
          moments: next,
          total: Math.max(0, tv.total - 1),
        },
      };
    }),
  setRelationshipAxes: (axes) =>
    set((state) => ({
      togetherView: {
        ...state.togetherView,
        summary: state.togetherView.summary
          ? { ...state.togetherView.summary, axes }
          : state.togetherView.summary,
      },
    })),
}));
