import { create } from "zustand";
import type { Memory, MemoryCounts, MemoryTier } from "@/types";

/**
 * Standalone store for the long-term-memory view. Extracted from the
 * composed ``useAssistantStore`` (phase 4a) so ``memory_added`` /
 * ``memory_updated`` / ``memory_deleted`` WS bursts only re-run the Memory
 * tab's selectors, not the whole app.
 */
export interface MemorySlice {
  memoryView: {
    items: Memory[];
    total: number;
    cap: number;
    page: number;
    pageSize: number;
    kindFilter: string | null;
    tierFilter: MemoryTier | null;
    /** Committed (debounced) text search sent to the server as `q`. */
    query: string;
    order: "recent" | "top";
    counts: MemoryCounts | null;
  };
  memoriesEnabled: boolean;
  setMemoryView: (view: {
    items: Memory[];
    total: number;
    cap: number;
    enabled: boolean;
    page: number;
    pageSize: number;
    kindFilter: string | null;
    tierFilter?: MemoryTier | null;
    query?: string;
    order: "recent" | "top";
  }) => void;
  setMemoryPage: (page: number) => void;
  setMemoryKindFilter: (kind: string | null) => void;
  setMemoryTierFilter: (tier: MemoryTier | null) => void;
  setMemoryQuery: (query: string) => void;
  setMemoryOrder: (order: "recent" | "top") => void;
  setMemoryCounts: (counts: MemoryCounts | null) => void;
  /** Reducer for ``memory_added``. */
  applyMemoryAdded: (memory: Memory) => void;
  /** Reducer for ``memory_updated``. */
  applyMemoryUpdated: (memory: Memory) => void;
  /** Reducer for ``memory_deleted``. */
  applyMemoryDeleted: (id: number) => void;
}

export const useMemoryStore = create<MemorySlice>()((set) => ({
  memoryView: {
    items: [],
    total: 0,
    cap: 0,
    page: 0,
    pageSize: 50,
    kindFilter: null,
    tierFilter: null,
    query: "",
    order: "recent",
    counts: null,
  },
  memoriesEnabled: true,
  setMemoryView: ({
    items,
    total,
    cap,
    enabled,
    page,
    pageSize,
    kindFilter,
    tierFilter,
    query,
    order,
  }) =>
    set((state) => ({
      memoryView: {
        items,
        total,
        cap,
        page,
        pageSize,
        kindFilter,
        tierFilter: tierFilter ?? state.memoryView.tierFilter,
        query: query ?? state.memoryView.query,
        order,
        counts: state.memoryView.counts,
      },
      memoriesEnabled: enabled,
    })),
  setMemoryPage: (page) =>
    set((state) => ({
      memoryView: { ...state.memoryView, page: Math.max(0, page) },
    })),
  setMemoryKindFilter: (kind) =>
    set((state) => ({
      memoryView: { ...state.memoryView, kindFilter: kind, page: 0 },
    })),
  setMemoryTierFilter: (tier) =>
    set((state) => ({
      memoryView: { ...state.memoryView, tierFilter: tier, page: 0 },
    })),
  setMemoryQuery: (query) =>
    set((state) => ({
      memoryView: { ...state.memoryView, query, page: 0 },
    })),
  setMemoryOrder: (order) =>
    set((state) => ({
      memoryView: { ...state.memoryView, order, page: 0 },
    })),
  setMemoryCounts: (counts) =>
    set((state) => ({
      memoryView: { ...state.memoryView, counts },
    })),
  applyMemoryAdded: (memory) =>
    set((state) => {
      const view = state.memoryView;
      const kindMatches = !view.kindFilter || view.kindFilter === memory.kind;
      const tierMatches = !view.tierFilter || view.tierFilter === memory.tier;
      // A search view opts out of live insertion entirely. Deciding
      // whether the new row matches would mean reimplementing the
      // server's matcher in TypeScript, and two implementations of one
      // predicate is how they start disagreeing. A searched list is a
      // snapshot until the next fetch, which is the honest reading of it.
      const filterMatches = kindMatches && tierMatches && !view.query;
      const onFirstPageRecent = view.page === 0 && view.order === "recent";
      // Always bump total when the new row would belong in the
      // current filter. Pagers across other tabs / windows then
      // re-render with the right "X of Y" label even though the row
      // itself isn't visible here.
      const nextTotal = filterMatches ? view.total + 1 : view.total;
      if (filterMatches && onFirstPageRecent) {
        // Prepend; trim to pageSize so the visible page count matches
        // the page-size contract.
        const next = [memory, ...view.items.filter((m) => m.id !== memory.id)];
        return {
          memoryView: {
            ...view,
            items: next.slice(0, view.pageSize),
            total: nextTotal,
          },
        };
      }
      return {
        memoryView: { ...view, total: nextTotal },
      };
    }),
  applyMemoryUpdated: (memory) =>
    set((state) => {
      const view = state.memoryView;
      const idx = view.items.findIndex((m) => m.id === memory.id);
      if (idx < 0) return {};
      const next = view.items.slice();
      next[idx] = memory;
      return { memoryView: { ...view, items: next } };
    }),
  applyMemoryDeleted: (id) =>
    set((state) => {
      const view = state.memoryView;
      const wasOnPage = view.items.some((m) => m.id === id);
      return {
        memoryView: {
          ...view,
          items: view.items.filter((m) => m.id !== id),
          total: wasOnPage ? Math.max(0, view.total - 1) : view.total,
        },
      };
    }),
}));
