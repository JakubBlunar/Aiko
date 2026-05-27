import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "./store";
import type { Memory, MemoryOrder, MemoryTier } from "./types";

/**
 * Covers the Zustand reducers backing the new Memory tab. The WS hook
 * dispatches ``applyMemoryAdded`` / ``applyMemoryUpdated`` /
 * ``applyMemoryDeleted`` from incoming events; exercising them
 * directly verifies the page-aware semantics without standing up the
 * hook (which needs jsdom + a fake WS).
 *
 * Page-aware contract under test:
 *   * ``memory_added`` only prepends to the visible page when we're on
 *     page 0 with ``order=recent`` AND the new row matches the active
 *     kind filter. Otherwise just bumps ``total``.
 *   * ``memory_updated`` replaces in place when the row is currently
 *     rendered, no-op otherwise.
 *   * ``memory_deleted`` removes the row + decrements total only when
 *     the row was actually on the visible page (avoids over-counting
 *     when another tab deletes a row we never had).
 */

function makeMemory(overrides: Partial<Memory> = {}): Memory {
  return {
    id: 1,
    content: "default content",
    kind: "fact",
    salience: 0.5,
    source_session: null,
    source_message_id: null,
    created_at: "2026-01-01T00:00:00Z",
    last_used_at: null,
    use_count: 0,
    pinned: false,
    ...overrides,
  };
}

function seedView(overrides: {
  items?: Memory[];
  total?: number;
  page?: number;
  pageSize?: number;
  kindFilter?: string | null;
  tierFilter?: MemoryTier | null;
  order?: MemoryOrder;
}) {
  useAssistantStore.getState().setMemoryView({
    items: overrides.items ?? [],
    total: overrides.total ?? 0,
    cap: 5000,
    enabled: true,
    page: overrides.page ?? 0,
    pageSize: overrides.pageSize ?? 50,
    kindFilter: overrides.kindFilter ?? null,
    tierFilter: overrides.tierFilter ?? null,
    order: overrides.order ?? "recent",
  });
}

beforeEach(() => {
  seedView({});
});

describe("memoryView — applyMemoryAdded", () => {
  it("prepends to page 0 + recent when no filter is active", () => {
    seedView({ items: [makeMemory({ id: 2 })], total: 1 });
    useAssistantStore.getState().applyMemoryAdded(makeMemory({ id: 5 }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items[0].id).toBe(5);
    expect(view.total).toBe(2);
  });

  it("trims the prepended page to pageSize", () => {
    seedView({
      items: [makeMemory({ id: 1 }), makeMemory({ id: 2 })],
      total: 2,
      pageSize: 2,
    });
    useAssistantStore.getState().applyMemoryAdded(makeMemory({ id: 3 }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items.map((m) => m.id)).toEqual([3, 1]);
    expect(view.items.length).toBe(2);
  });

  it("bumps total but does not prepend when not on page 0", () => {
    seedView({ items: [makeMemory({ id: 1 })], total: 100, page: 1 });
    useAssistantStore.getState().applyMemoryAdded(makeMemory({ id: 5 }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items[0].id).toBe(1); // unchanged
    expect(view.total).toBe(101);
  });

  it("bumps total but does not prepend when order=top", () => {
    seedView({ items: [makeMemory({ id: 1 })], total: 5, order: "top" });
    useAssistantStore.getState().applyMemoryAdded(makeMemory({ id: 5 }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items[0].id).toBe(1);
    expect(view.total).toBe(6);
  });

  it("ignores rows that don't match the active kind filter", () => {
    seedView({
      items: [makeMemory({ id: 1, kind: "fact" })],
      total: 1,
      kindFilter: "fact",
    });
    useAssistantStore
      .getState()
      .applyMemoryAdded(makeMemory({ id: 9, kind: "event" }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items[0].id).toBe(1);
    // Filter doesn't match, so total stays put.
    expect(view.total).toBe(1);
  });

  it("prepends when the new row matches the kind filter", () => {
    seedView({
      items: [makeMemory({ id: 1, kind: "fact" })],
      total: 1,
      kindFilter: "fact",
    });
    useAssistantStore
      .getState()
      .applyMemoryAdded(makeMemory({ id: 9, kind: "fact" }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items[0].id).toBe(9);
    expect(view.total).toBe(2);
  });
});

describe("memoryView — applyMemoryUpdated", () => {
  it("replaces in place when the row is rendered", () => {
    seedView({
      items: [
        makeMemory({ id: 1, content: "old" }),
        makeMemory({ id: 2, content: "stable" }),
      ],
      total: 2,
    });
    useAssistantStore
      .getState()
      .applyMemoryUpdated(makeMemory({ id: 1, content: "new" }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items[0]).toMatchObject({ id: 1, content: "new" });
    expect(view.items[1]).toMatchObject({ id: 2, content: "stable" });
    // Replace doesn't touch total.
    expect(view.total).toBe(2);
  });

  it("no-ops when the row isn't on the current page", () => {
    seedView({ items: [makeMemory({ id: 1 })], total: 50 });
    useAssistantStore
      .getState()
      .applyMemoryUpdated(makeMemory({ id: 999, content: "off-page" }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items.map((m) => m.id)).toEqual([1]);
    expect(view.total).toBe(50);
  });
});

describe("memoryView — applyMemoryDeleted", () => {
  it("removes a rendered row and decrements total", () => {
    seedView({
      items: [makeMemory({ id: 1 }), makeMemory({ id: 2 })],
      total: 5,
    });
    useAssistantStore.getState().applyMemoryDeleted(2);
    const view = useAssistantStore.getState().memoryView;
    expect(view.items.map((m) => m.id)).toEqual([1]);
    expect(view.total).toBe(4);
  });

  it("leaves total alone when the deleted row was off the visible page", () => {
    // The row never landed in our paginated slice; another tab/window
    // deleted it and the WS broadcast arrived. We don't double-count.
    seedView({
      items: [makeMemory({ id: 1 })],
      total: 10,
    });
    useAssistantStore.getState().applyMemoryDeleted(99);
    const view = useAssistantStore.getState().memoryView;
    expect(view.total).toBe(10);
    expect(view.items.length).toBe(1);
  });
});

describe("memoryView — page / filter setters reset page", () => {
  it("setMemoryKindFilter resets page to 0", () => {
    seedView({ items: [], total: 0, page: 3 });
    useAssistantStore.getState().setMemoryKindFilter("fact");
    expect(useAssistantStore.getState().memoryView.page).toBe(0);
    expect(useAssistantStore.getState().memoryView.kindFilter).toBe("fact");
  });

  it("setMemoryOrder resets page to 0", () => {
    seedView({ items: [], total: 0, page: 2 });
    useAssistantStore.getState().setMemoryOrder("top");
    expect(useAssistantStore.getState().memoryView.page).toBe(0);
    expect(useAssistantStore.getState().memoryView.order).toBe("top");
  });

  it("setMemoryPage clamps negative pages to 0", () => {
    seedView({ items: [], total: 0, page: 1 });
    useAssistantStore.getState().setMemoryPage(-3);
    expect(useAssistantStore.getState().memoryView.page).toBe(0);
  });

  it("setMemoryTierFilter resets page to 0 and updates filter", () => {
    seedView({ items: [], total: 0, page: 4 });
    useAssistantStore.getState().setMemoryTierFilter("scratchpad");
    const view = useAssistantStore.getState().memoryView;
    expect(view.page).toBe(0);
    expect(view.tierFilter).toBe("scratchpad");
  });

  it("setMemoryCounts stores the per-tier counts snapshot", () => {
    const counts = { scratchpad: 4, long_term: 12, archive: 3, total: 19 };
    useAssistantStore.getState().setMemoryCounts(counts);
    expect(useAssistantStore.getState().memoryView.counts).toEqual(counts);
  });
});

// Schema v8: tier filter interactions with the added-row reducer. A
// scratchpad row should only prepend when tierFilter is null OR
// matches; otherwise total stays put just like the kind filter.
describe("memoryView — tier-aware applyMemoryAdded", () => {
  it("prepends when tier filter matches", () => {
    seedView({
      items: [makeMemory({ id: 1, tier: "scratchpad" })],
      total: 1,
      tierFilter: "scratchpad",
    });
    useAssistantStore
      .getState()
      .applyMemoryAdded(makeMemory({ id: 5, tier: "scratchpad" }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items[0].id).toBe(5);
    expect(view.total).toBe(2);
  });

  it("ignores rows whose tier doesn't match the active filter", () => {
    seedView({
      items: [makeMemory({ id: 1, tier: "scratchpad" })],
      total: 1,
      tierFilter: "scratchpad",
    });
    useAssistantStore
      .getState()
      .applyMemoryAdded(makeMemory({ id: 7, tier: "long_term" }));
    const view = useAssistantStore.getState().memoryView;
    expect(view.items.map((m) => m.id)).toEqual([1]);
    expect(view.total).toBe(1);
  });
});
