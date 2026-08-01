import { beforeEach, describe, expect, it } from "vitest";
import { useCuePoolStore } from "./useCuePoolStore";
import type { CueRow, CueState } from "@/types";

/**
 * ``applyCuePoolUpdated`` is the whole reducer surface: one WS event
 * carries both a worker writing a cue and a cue flipping to ``used``,
 * so add and update are the same upsert.
 *
 * The case worth pinning is the flip. A cue going ``pending -> used``
 * while the panel is filtered to ``pending`` no longer matches the
 * filter, and dropping it would hide the one moment the panel exists to
 * show. It stays until the next fetch.
 */
function makeCue(overrides: Partial<CueRow> = {}): CueRow {
  return {
    id: 1,
    cue_type: "curiosity_seed",
    subject: "film photography",
    text: "a cue",
    payload: {},
    state: "pending",
    surfaced_count: 0,
    ask_count: 0,
    last_surfaced_at: null,
    last_asked_at: null,
    not_before: null,
    created_at: "2026-01-01T00:00:00Z",
    expires_at: null,
    used_at: null,
    used_evidence: null,
    ...overrides,
  };
}

function seedView(overrides: {
  items?: CueRow[];
  total?: number;
  page?: number;
  typeFilter?: string | null;
  stateFilter?: CueState | null;
}) {
  const store = useCuePoolStore.getState();
  store.setCuePoolView({
    items: overrides.items ?? [],
    total: overrides.total ?? 0,
    stats: [],
    types: ["curiosity_seed", "dormant_interest"],
    enabled: true,
  });
  store.setCuePoolTypeFilter(overrides.typeFilter ?? null);
  store.setCuePoolStateFilter(overrides.stateFilter ?? null);
  store.setCuePoolPage(overrides.page ?? 0);
}

beforeEach(() => {
  seedView({});
});

describe("applyCuePoolUpdated — a cue she just spent", () => {
  it("replaces the row in place so it turns green where you were looking", () => {
    seedView({ items: [makeCue({ id: 7 }), makeCue({ id: 8 })], total: 2 });
    useCuePoolStore
      .getState()
      .applyCuePoolUpdated(
        makeCue({ id: 8, state: "used", used_evidence: "lexical:0.67" }),
      );
    const view = useCuePoolStore.getState().cuePoolView;
    expect(view.items.map((c) => c.id)).toEqual([7, 8]);
    expect(view.items[1].state).toBe("used");
    expect(view.total).toBe(2);
  });

  it("keeps a row that just left the active filter", () => {
    seedView({
      items: [makeCue({ id: 7 })],
      total: 1,
      stateFilter: "pending",
    });
    useCuePoolStore
      .getState()
      .applyCuePoolUpdated(makeCue({ id: 7, state: "used" }));
    const view = useCuePoolStore.getState().cuePoolView;
    expect(view.items).toHaveLength(1);
    expect(view.items[0].state).toBe("used");
  });
});

describe("applyCuePoolUpdated — a cue a worker just wrote", () => {
  it("prepends on page 0 with no filter", () => {
    seedView({ items: [makeCue({ id: 2 })], total: 1 });
    useCuePoolStore.getState().applyCuePoolUpdated(makeCue({ id: 5 }));
    const view = useCuePoolStore.getState().cuePoolView;
    expect(view.items[0].id).toBe(5);
    expect(view.total).toBe(2);
  });

  it("ignores one that doesn't match the type filter", () => {
    seedView({ items: [], total: 0, typeFilter: "dormant_interest" });
    useCuePoolStore
      .getState()
      .applyCuePoolUpdated(makeCue({ id: 5, cue_type: "curiosity_seed" }));
    const view = useCuePoolStore.getState().cuePoolView;
    expect(view.items).toHaveLength(0);
    expect(view.total).toBe(0);
  });

  it("ignores one that doesn't match the state filter", () => {
    seedView({ items: [], total: 0, stateFilter: "used" });
    useCuePoolStore.getState().applyCuePoolUpdated(makeCue({ id: 5 }));
    expect(useCuePoolStore.getState().cuePoolView.items).toHaveLength(0);
  });

  it("bumps the total but doesn't prepend when you've paged away", () => {
    seedView({ items: [makeCue({ id: 1 })], total: 100, page: 1 });
    useCuePoolStore.getState().applyCuePoolUpdated(makeCue({ id: 5 }));
    const view = useCuePoolStore.getState().cuePoolView;
    expect(view.items[0].id).toBe(1);
    expect(view.total).toBe(101);
  });
});

describe("filters", () => {
  it("changing a filter returns to page 0", () => {
    seedView({ page: 3 });
    useCuePoolStore.getState().setCuePoolTypeFilter("dormant_interest");
    expect(useCuePoolStore.getState().cuePoolView.page).toBe(0);
    useCuePoolStore.getState().setCuePoolPage(2);
    useCuePoolStore.getState().setCuePoolStateFilter("used");
    expect(useCuePoolStore.getState().cuePoolView.page).toBe(0);
  });
});
