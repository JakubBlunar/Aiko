import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "./store";
import type {
  WorldItem,
  WorldLocation,
  WorldSnapshot,
  WorldState,
} from "./types";

/**
 * Tests for the ``world`` Zustand slice + ``applyWorldPatch`` reducer.
 *
 * The WS hook fires ``applyWorldPatch`` for every ``world_updated``
 * frame; exercising it directly here covers the surgical merge logic
 * without needing jsdom or a fake WS.
 */

function makeLocation(over: Partial<WorldLocation> = {}): WorldLocation {
  return {
    id: 1,
    slug: "desk",
    name: "the desk",
    description: "",
    position: 0,
    ...over,
  };
}

function makeItem(over: Partial<WorldItem> = {}): WorldItem {
  return {
    id: 1,
    slug: "lamp",
    name: "warm lamp",
    description: "",
    kind: "decor",
    consumable: false,
    quantity: 1,
    location_id: 1,
    state: {},
    given_by: null,
    created_at: "2026-05-27T00:00:00Z",
    updated_at: "2026-05-27T00:00:00Z",
    ...over,
  };
}

function makeState(over: Partial<WorldState> = {}): WorldState {
  return {
    location_id: 1,
    posture: "sitting",
    activity: "watching_screens",
    mood_note: "",
    updated_at: "2026-05-27T00:00:00Z",
    ...over,
  };
}

function makeSnapshot(over: Partial<WorldSnapshot> = {}): WorldSnapshot {
  return {
    state: makeState(),
    locations: [makeLocation()],
    items: [makeItem()],
    enabled: true,
    ...over,
  };
}

beforeEach(() => {
  useAssistantStore.getState().setWorld(null);
});

describe("setWorld", () => {
  it("stores the snapshot and surfaces enabled flag", () => {
    useAssistantStore.getState().setWorld(makeSnapshot());
    const world = useAssistantStore.getState().world;
    expect(world).not.toBeNull();
    expect(world?.locations.length).toBe(1);
    expect(world?.items.length).toBe(1);
    expect(world?.enabled).toBe(true);
  });
});

describe("applyWorldPatch — state", () => {
  it("merges state without touching items / locations", () => {
    useAssistantStore.getState().setWorld(makeSnapshot());
    const newState = makeState({ posture: "lying", activity: "napping" });
    useAssistantStore.getState().applyWorldPatch({ state: newState });
    const world = useAssistantStore.getState().world!;
    expect(world.state.posture).toBe("lying");
    expect(world.state.activity).toBe("napping");
    expect(world.locations.length).toBe(1);
    expect(world.items.length).toBe(1);
  });

  it("is a no-op on a state patch when world is null", () => {
    useAssistantStore.getState().applyWorldPatch({ state: makeState() });
    expect(useAssistantStore.getState().world).toBeNull();
  });
});

describe("applyWorldPatch — location", () => {
  it("inserts a new location and sorts by position", () => {
    useAssistantStore.getState().setWorld(
      makeSnapshot({
        locations: [
          makeLocation({ id: 1, position: 0 }),
          makeLocation({ id: 3, position: 5, slug: "bed", name: "the bed" }),
        ],
      }),
    );
    useAssistantStore.getState().applyWorldPatch({
      location: makeLocation({
        id: 2,
        position: 2,
        slug: "lamp_corner",
        name: "the lamp corner",
      }),
    });
    const ids = useAssistantStore
      .getState()
      .world!.locations.map((l) => l.id);
    expect(ids).toEqual([1, 2, 3]);
  });

  it("replaces an existing location in place when ids match", () => {
    useAssistantStore.getState().setWorld(makeSnapshot());
    useAssistantStore.getState().applyWorldPatch({
      location: makeLocation({ id: 1, name: "the renamed desk" }),
    });
    const locs = useAssistantStore.getState().world!.locations;
    expect(locs.length).toBe(1);
    expect(locs[0].name).toBe("the renamed desk");
  });
});

describe("applyWorldPatch — item", () => {
  it("inserts a new item without disturbing the rest", () => {
    useAssistantStore.getState().setWorld(makeSnapshot());
    useAssistantStore.getState().applyWorldPatch({
      item: makeItem({ id: 2, name: "cookies", consumable: true, quantity: 3 }),
    });
    const items = useAssistantStore.getState().world!.items;
    expect(items.length).toBe(2);
    const cookies = items.find((i) => i.id === 2);
    expect(cookies?.name).toBe("cookies");
    expect(cookies?.consumable).toBe(true);
  });

  it("upserts an existing item when id matches", () => {
    useAssistantStore.getState().setWorld(makeSnapshot());
    useAssistantStore.getState().applyWorldPatch({
      item: makeItem({ id: 1, quantity: 0, name: "spent lamp" }),
    });
    const items = useAssistantStore.getState().world!.items;
    expect(items.length).toBe(1);
    expect(items[0].quantity).toBe(0);
    expect(items[0].name).toBe("spent lamp");
  });
});

describe("applyWorldPatch — deletions", () => {
  it("removes an item and only that item", () => {
    useAssistantStore.getState().setWorld(
      makeSnapshot({
        items: [
          makeItem({ id: 1, name: "lamp" }),
          makeItem({ id: 2, name: "cookies" }),
        ],
      }),
    );
    useAssistantStore.getState().applyWorldPatch({ deleted_item_id: 1 });
    const items = useAssistantStore.getState().world!.items;
    expect(items.length).toBe(1);
    expect(items[0].id).toBe(2);
  });

  it("removes a location and clears item.location_id + state pointer", () => {
    useAssistantStore.getState().setWorld(
      makeSnapshot({
        locations: [
          makeLocation({ id: 1 }),
          makeLocation({ id: 2, slug: "bed", name: "bed" }),
        ],
        items: [
          makeItem({ id: 1, location_id: 1 }),
          makeItem({ id: 2, location_id: 2 }),
        ],
        state: makeState({ location_id: 1 }),
      }),
    );
    useAssistantStore.getState().applyWorldPatch({ deleted_location_id: 1 });
    const world = useAssistantStore.getState().world!;
    expect(world.locations.length).toBe(1);
    expect(world.items.find((i) => i.id === 1)?.location_id).toBeNull();
    expect(world.state.location_id).toBeNull();
    // Item that wasn't at the deleted location is untouched.
    expect(world.items.find((i) => i.id === 2)?.location_id).toBe(2);
  });
});

describe("applyWorldPatch — full snapshot", () => {
  it("replaces everything and bootstraps from null", () => {
    useAssistantStore.getState().setWorld(null);
    const fresh = makeSnapshot({
      locations: [makeLocation({ id: 99, slug: "treehouse", name: "treehouse" })],
      items: [],
      state: makeState({ location_id: 99 }),
    });
    useAssistantStore.getState().applyWorldPatch({
      snapshot: {
        state: fresh.state,
        locations: fresh.locations,
        items: fresh.items,
      },
    });
    const world = useAssistantStore.getState().world!;
    expect(world.locations[0].slug).toBe("treehouse");
    expect(world.items.length).toBe(0);
    expect(world.enabled).toBe(true);
  });
});
