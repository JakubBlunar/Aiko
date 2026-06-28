import type { Belief, BeliefKind, BeliefStatus } from "@/types";
import type { SliceCreator } from "../types";

/** True when ``belief`` belongs in the currently-filtered Beliefs view. */
function beliefMatchesFilter(
  belief: Belief,
  view: { kindFilter: BeliefKind | "all"; statusFilter: BeliefStatus | "all" },
): boolean {
  const kindOk = view.kindFilter === "all" || belief.kind === view.kindFilter;
  const statusOk =
    view.statusFilter === "all" || belief.status === view.statusFilter;
  return kindOk && statusOk;
}

export interface BeliefsSlice {
  // K2 theory-of-mind beliefs. The Beliefs sub-panel reads this slice;
  // it mirrors the memory WS-reducer shape so the panel stays live as the
  // K2 worker / ``[[predict:...]]`` self-tags / the gap detector flip
  // beliefs in the background.
  beliefView: {
    items: Belief[];
    counts: {
      active: number;
      confirmed: number;
      contradicted: number;
      stale: number;
    } | null;
    enabled: boolean;
    kindFilter: BeliefKind | "all";
    statusFilter: BeliefStatus | "all";
  };
  setBeliefView: (view: {
    items: Belief[];
    counts?: {
      active: number;
      confirmed: number;
      contradicted: number;
      stale: number;
    } | null;
    enabled: boolean;
  }) => void;
  setBeliefKindFilter: (kind: BeliefKind | "all") => void;
  setBeliefStatusFilter: (status: BeliefStatus | "all") => void;
  /** Reducer for ``belief_added``. */
  applyBeliefAdded: (belief: Belief) => void;
  /** Reducer for ``belief_updated`` (re-evaluates filter membership). */
  applyBeliefUpdated: (belief: Belief) => void;
  /** Reducer for ``belief_deleted``. */
  applyBeliefDeleted: (id: number) => void;
}

export const createBeliefsSlice: SliceCreator<BeliefsSlice> = (set) => ({
  beliefView: {
    items: [],
    counts: null,
    enabled: true,
    kindFilter: "all",
    statusFilter: "active",
  },
  setBeliefView: (view) =>
    set((state) => ({
      beliefView: {
        ...state.beliefView,
        items: view.items,
        counts: view.counts ?? null,
        enabled: view.enabled,
      },
    })),
  setBeliefKindFilter: (kind) =>
    set((state) => ({
      beliefView: { ...state.beliefView, kindFilter: kind },
    })),
  setBeliefStatusFilter: (status) =>
    set((state) => ({
      beliefView: { ...state.beliefView, statusFilter: status },
    })),
  applyBeliefAdded: (belief) =>
    set((state) => {
      const view = state.beliefView;
      const matches = beliefMatchesFilter(belief, view);
      if (!matches || view.items.some((b) => b.id === belief.id)) return {};
      return { beliefView: { ...view, items: [belief, ...view.items] } };
    }),
  applyBeliefUpdated: (belief) =>
    set((state) => {
      const view = state.beliefView;
      const matches = beliefMatchesFilter(belief, view);
      const idx = view.items.findIndex((b) => b.id === belief.id);
      if (matches) {
        const next = view.items.slice();
        if (idx >= 0) next[idx] = belief;
        else next.unshift(belief);
        return { beliefView: { ...view, items: next } };
      }
      // No longer matches the current view (e.g. flipped out of
      // "active"): drop it if it was visible, otherwise nothing to do.
      if (idx < 0) return {};
      return {
        beliefView: {
          ...view,
          items: view.items.filter((b) => b.id !== belief.id),
        },
      };
    }),
  applyBeliefDeleted: (id) =>
    set((state) => {
      const view = state.beliefView;
      if (!view.items.some((b) => b.id === id)) return {};
      return {
        beliefView: {
          ...view,
          items: view.items.filter((b) => b.id !== id),
        },
      };
    }),
});
