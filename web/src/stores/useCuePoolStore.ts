import { create } from "zustand";
import type { CueRow, CueState, CueTypeStats } from "@/types";

/**
 * The Cues panel's slice.
 *
 * Standalone for the same reason the memory view is: the panel is one
 * tab deep and its updates shouldn't re-run selectors anywhere else.
 *
 * There's a single WS event behind it, ``cue_pool_updated``, and it
 * carries both halves of a cue's life — a worker writing a new one, and
 * a cue flipping to ``used`` the moment Aiko says the thing. Both are an
 * upsert, so there's one reducer rather than the add/update pair the
 * memory slice needs.
 */
export interface CuePoolSlice {
  cuePoolView: {
    items: CueRow[];
    total: number;
    stats: CueTypeStats[];
    /** Every type with a policy, so the filter row is complete even for
     *  types with nothing in the pool yet. */
    types: string[];
    page: number;
    pageSize: number;
    typeFilter: string | null;
    stateFilter: CueState | null;
  };
  cuePoolEnabled: boolean;
  setCuePoolView: (view: {
    items: CueRow[];
    total: number;
    stats: CueTypeStats[];
    types: string[];
    enabled: boolean;
  }) => void;
  setCuePoolPage: (page: number) => void;
  setCuePoolTypeFilter: (cueType: string | null) => void;
  setCuePoolStateFilter: (state: CueState | null) => void;
  /** Reducer for ``cue_pool_updated``. */
  applyCuePoolUpdated: (cue: CueRow) => void;
}

const PAGE_SIZE = 50;

function matchesFilters(
  cue: CueRow,
  typeFilter: string | null,
  stateFilter: CueState | null,
): boolean {
  if (typeFilter && cue.cue_type !== typeFilter) return false;
  if (stateFilter && cue.state !== stateFilter) return false;
  return true;
}

export const useCuePoolStore = create<CuePoolSlice>()((set) => ({
  cuePoolView: {
    items: [],
    total: 0,
    stats: [],
    types: [],
    page: 0,
    pageSize: PAGE_SIZE,
    typeFilter: null,
    stateFilter: null,
  },
  cuePoolEnabled: true,
  setCuePoolView: ({ items, total, stats, types, enabled }) =>
    set((state) => ({
      cuePoolView: { ...state.cuePoolView, items, total, stats, types },
      cuePoolEnabled: enabled,
    })),
  setCuePoolPage: (page) =>
    set((state) => ({
      cuePoolView: { ...state.cuePoolView, page: Math.max(0, page) },
    })),
  setCuePoolTypeFilter: (cueType) =>
    set((state) => ({
      cuePoolView: { ...state.cuePoolView, typeFilter: cueType, page: 0 },
    })),
  setCuePoolStateFilter: (cueState) =>
    set((state) => ({
      cuePoolView: { ...state.cuePoolView, stateFilter: cueState, page: 0 },
    })),
  applyCuePoolUpdated: (cue) =>
    set((state) => {
      const view = state.cuePoolView;
      const idx = view.items.findIndex((row) => row.id === cue.id);
      if (idx >= 0) {
        // Replace in place rather than re-sorting: seeing the row you
        // were already looking at turn green is the whole point.
        const next = view.items.slice();
        next[idx] = cue;
        // It may have just left the filter (pending -> used while
        // filtered to pending); the next fetch settles that. Dropping it
        // mid-flip would hide the one moment worth watching.
        return { cuePoolView: { ...view, items: next } };
      }
      if (!matchesFilters(cue, view.typeFilter, view.stateFilter)) return {};
      const total = view.total + 1;
      if (view.page !== 0) return { cuePoolView: { ...view, total } };
      return {
        cuePoolView: {
          ...view,
          items: [cue, ...view.items].slice(0, view.pageSize),
          total,
        },
      };
    }),
}));
