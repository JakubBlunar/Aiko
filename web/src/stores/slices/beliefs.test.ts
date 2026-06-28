import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "../../store";
import type { Belief, BeliefKind, BeliefStatus } from "../../types";

/**
 * Covers the Zustand reducers backing the live Beliefs panel (I1). The
 * WS hook dispatches ``applyBeliefAdded`` / ``applyBeliefUpdated`` /
 * ``applyBeliefDeleted``; exercising them directly verifies the
 * filter-aware semantics without standing up the socket hook.
 *
 * Contract under test:
 *   * ``belief_added`` prepends only when it matches the active kind +
 *     status filter and isn't already present.
 *   * ``belief_updated`` is the high-value case: a status flip
 *     (active -> contradicted) drops the row out of an "active" view,
 *     replaces in place when it still matches, and prepends when it
 *     newly matches.
 *   * ``belief_deleted`` removes by id.
 */

function makeBelief(overrides: Partial<Belief> = {}): Belief {
  return {
    id: 1,
    user_id: "u",
    kind: "opinion",
    topic: "horror movies",
    predicted_state: "dislikes them",
    confidence: 0.7,
    valence: null,
    arousal: null,
    source: "worker",
    source_message_id: null,
    observed_at: "2026-01-01T00:00:00Z",
    last_checked_at: null,
    status: "active",
    gap_seen_at: null,
    metadata: {},
    ...overrides,
  };
}

function seedView(overrides: {
  items?: Belief[];
  kindFilter?: BeliefKind | "all";
  statusFilter?: BeliefStatus | "all";
}) {
  const store = useAssistantStore.getState();
  store.setBeliefView({ items: overrides.items ?? [], enabled: true });
  store.setBeliefKindFilter(overrides.kindFilter ?? "all");
  store.setBeliefStatusFilter(overrides.statusFilter ?? "active");
}

beforeEach(() => {
  useAssistantStore.getState().setBeliefView({ items: [], enabled: true });
  useAssistantStore.getState().setBeliefKindFilter("all");
  useAssistantStore.getState().setBeliefStatusFilter("active");
});

describe("applyBeliefAdded", () => {
  it("prepends a matching belief", () => {
    seedView({ items: [makeBelief({ id: 1 })] });
    useAssistantStore.getState().applyBeliefAdded(makeBelief({ id: 2 }));
    const items = useAssistantStore.getState().beliefView.items;
    expect(items.map((b) => b.id)).toEqual([2, 1]);
  });

  it("ignores a belief that doesn't match the status filter", () => {
    seedView({ items: [], statusFilter: "active" });
    useAssistantStore
      .getState()
      .applyBeliefAdded(makeBelief({ id: 2, status: "contradicted" }));
    expect(useAssistantStore.getState().beliefView.items).toEqual([]);
  });

  it("ignores a belief that doesn't match the kind filter", () => {
    seedView({ items: [], kindFilter: "mood" });
    useAssistantStore
      .getState()
      .applyBeliefAdded(makeBelief({ id: 2, kind: "opinion" }));
    expect(useAssistantStore.getState().beliefView.items).toEqual([]);
  });

  it("does not duplicate an already-present belief", () => {
    seedView({ items: [makeBelief({ id: 1 })] });
    useAssistantStore.getState().applyBeliefAdded(makeBelief({ id: 1 }));
    expect(useAssistantStore.getState().beliefView.items).toHaveLength(1);
  });
});

describe("applyBeliefUpdated", () => {
  it("replaces in place when it still matches", () => {
    seedView({ items: [makeBelief({ id: 1, predicted_state: "old" })] });
    useAssistantStore
      .getState()
      .applyBeliefUpdated(makeBelief({ id: 1, predicted_state: "new" }));
    const items = useAssistantStore.getState().beliefView.items;
    expect(items).toHaveLength(1);
    expect(items[0].predicted_state).toBe("new");
  });

  it("drops a row that flips out of the active view", () => {
    seedView({
      items: [makeBelief({ id: 1, status: "active" })],
      statusFilter: "active",
    });
    useAssistantStore
      .getState()
      .applyBeliefUpdated(makeBelief({ id: 1, status: "contradicted" }));
    expect(useAssistantStore.getState().beliefView.items).toEqual([]);
  });

  it("prepends a row that newly matches the filter", () => {
    seedView({ items: [], statusFilter: "contradicted" });
    useAssistantStore
      .getState()
      .applyBeliefUpdated(makeBelief({ id: 9, status: "contradicted" }));
    const items = useAssistantStore.getState().beliefView.items;
    expect(items.map((b) => b.id)).toEqual([9]);
  });
});

describe("applyBeliefDeleted", () => {
  it("removes the row by id", () => {
    seedView({ items: [makeBelief({ id: 1 }), makeBelief({ id: 2 })] });
    useAssistantStore.getState().applyBeliefDeleted(1);
    expect(
      useAssistantStore.getState().beliefView.items.map((b) => b.id),
    ).toEqual([2]);
  });

  it("is a no-op when the id isn't present", () => {
    seedView({ items: [makeBelief({ id: 1 })] });
    useAssistantStore.getState().applyBeliefDeleted(99);
    expect(useAssistantStore.getState().beliefView.items).toHaveLength(1);
  });
});
