import type { Memory, MemoryCounts, MemoryTier } from "@/types";
import type { SliceCreator } from "../types";

export interface MemorySlice {
  // Long-term memories. The Memory tab uses ``memoryView`` (paginated +
  // filtered slice). ``memoriesEnabled`` mirrors the backend's
  // ``memory.enabled`` config so the UI can grey out the tab.
  memoryView: {
    items: Memory[];
    total: number;
    cap: number;
    page: number;
    pageSize: number;
    kindFilter: string | null;
    tierFilter: MemoryTier | null;
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
    order: "recent" | "top";
  }) => void;
  setMemoryPage: (page: number) => void;
  setMemoryKindFilter: (kind: string | null) => void;
  setMemoryTierFilter: (tier: MemoryTier | null) => void;
  setMemoryOrder: (order: "recent" | "top") => void;
  setMemoryCounts: (counts: MemoryCounts | null) => void;
  /** Reducer for ``memory_added``. */
  applyMemoryAdded: (memory: Memory) => void;
  /** Reducer for ``memory_updated``. */
  applyMemoryUpdated: (memory: Memory) => void;
  /** Reducer for ``memory_deleted``. */
  applyMemoryDeleted: (id: number) => void;
}

export const createMemorySlice: SliceCreator<MemorySlice> = (set) => ({
  memoryView: {
    items: [],
    total: 0,
    cap: 5000,
    page: 0,
    pageSize: 50,
    kindFilter: null,
    tierFilter: null,
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
      const filterMatches = kindMatches && tierMatches;
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
});
