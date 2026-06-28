import { beforeEach, describe, expect, it } from "vitest";

import { useWorldStore } from "./useWorldStore";
import { WORLD_KINDS } from "../types";
import type {
  WorldItem,
  WorldLocation,
  WorldSnapshot,
  WorldState,
} from "../types";

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
  useWorldStore.getState().setWorld(null);
});

describe("setWorld", () => {
  it("stores the snapshot and surfaces enabled flag", () => {
    useWorldStore.getState().setWorld(makeSnapshot());
    const world = useWorldStore.getState().world;
    expect(world).not.toBeNull();
    expect(world?.locations.length).toBe(1);
    expect(world?.items.length).toBe(1);
    expect(world?.enabled).toBe(true);
  });
});

describe("applyWorldPatch — state", () => {
  it("merges state without touching items / locations", () => {
    useWorldStore.getState().setWorld(makeSnapshot());
    const newState = makeState({ posture: "lying", activity: "napping" });
    useWorldStore.getState().applyWorldPatch({ state: newState });
    const world = useWorldStore.getState().world!;
    expect(world.state.posture).toBe("lying");
    expect(world.state.activity).toBe("napping");
    expect(world.locations.length).toBe(1);
    expect(world.items.length).toBe(1);
  });

  it("is a no-op on a state patch when world is null", () => {
    useWorldStore.getState().applyWorldPatch({ state: makeState() });
    expect(useWorldStore.getState().world).toBeNull();
  });
});

describe("applyWorldPatch — location", () => {
  it("inserts a new location and sorts by position", () => {
    useWorldStore.getState().setWorld(
      makeSnapshot({
        locations: [
          makeLocation({ id: 1, position: 0 }),
          makeLocation({ id: 3, position: 5, slug: "bed", name: "the bed" }),
        ],
      }),
    );
    useWorldStore.getState().applyWorldPatch({
      location: makeLocation({
        id: 2,
        position: 2,
        slug: "lamp_corner",
        name: "the lamp corner",
      }),
    });
    const ids = useWorldStore
      .getState()
      .world!.locations.map((l) => l.id);
    expect(ids).toEqual([1, 2, 3]);
  });

  it("replaces an existing location in place when ids match", () => {
    useWorldStore.getState().setWorld(makeSnapshot());
    useWorldStore.getState().applyWorldPatch({
      location: makeLocation({ id: 1, name: "the renamed desk" }),
    });
    const locs = useWorldStore.getState().world!.locations;
    expect(locs.length).toBe(1);
    expect(locs[0].name).toBe("the renamed desk");
  });
});

describe("applyWorldPatch — item", () => {
  it("inserts a new item without disturbing the rest", () => {
    useWorldStore.getState().setWorld(makeSnapshot());
    useWorldStore.getState().applyWorldPatch({
      item: makeItem({ id: 2, name: "cookies", consumable: true, quantity: 3 }),
    });
    const items = useWorldStore.getState().world!.items;
    expect(items.length).toBe(2);
    const cookies = items.find((i) => i.id === 2);
    expect(cookies?.name).toBe("cookies");
    expect(cookies?.consumable).toBe(true);
  });

  it("upserts an existing item when id matches", () => {
    useWorldStore.getState().setWorld(makeSnapshot());
    useWorldStore.getState().applyWorldPatch({
      item: makeItem({ id: 1, quantity: 0, name: "spent lamp" }),
    });
    const items = useWorldStore.getState().world!.items;
    expect(items.length).toBe(1);
    expect(items[0].quantity).toBe(0);
    expect(items[0].name).toBe("spent lamp");
  });
});

describe("applyWorldPatch — deletions", () => {
  it("removes an item and only that item", () => {
    useWorldStore.getState().setWorld(
      makeSnapshot({
        items: [
          makeItem({ id: 1, name: "lamp" }),
          makeItem({ id: 2, name: "cookies" }),
        ],
      }),
    );
    useWorldStore.getState().applyWorldPatch({ deleted_item_id: 1 });
    const items = useWorldStore.getState().world!.items;
    expect(items.length).toBe(1);
    expect(items[0].id).toBe(2);
  });

  it("removes a location and clears item.location_id + state pointer", () => {
    useWorldStore.getState().setWorld(
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
    useWorldStore.getState().applyWorldPatch({ deleted_location_id: 1 });
    const world = useWorldStore.getState().world!;
    expect(world.locations.length).toBe(1);
    expect(world.items.find((i) => i.id === 1)?.location_id).toBeNull();
    expect(world.state.location_id).toBeNull();
    // Item that wasn't at the deleted location is untouched.
    expect(world.items.find((i) => i.id === 2)?.location_id).toBe(2);
  });
});

describe("applyWorldPatch — full snapshot", () => {
  it("replaces everything and bootstraps from null", () => {
    useWorldStore.getState().setWorld(null);
    const fresh = makeSnapshot({
      locations: [makeLocation({ id: 99, slug: "treehouse", name: "treehouse" })],
      items: [],
      state: makeState({ location_id: 99 }),
    });
    useWorldStore.getState().applyWorldPatch({
      snapshot: {
        state: fresh.state,
        locations: fresh.locations,
        items: fresh.items,
      },
    });
    const world = useWorldStore.getState().world!;
    expect(world.locations[0].slug).toBe("treehouse");
    expect(world.items.length).toBe(0);
    expect(world.enabled).toBe(true);
  });
});

describe("WORLD_KINDS — garden kinds", () => {
  it("exposes plant and seed alongside the existing kinds", () => {
    expect(WORLD_KINDS).toContain("plant");
    expect(WORLD_KINDS).toContain("seed");
  });

  it("round-trips a plant kind through applyWorldPatch", () => {
    useWorldStore.getState().setWorld(makeSnapshot({ items: [] }));
    const plant = makeItem({
      id: 42,
      slug: "basil_seedling",
      name: "basil seedling",
      kind: "plant",
      state: { species: "basil", stage: "sprout" },
    });
    useWorldStore.getState().applyWorldPatch({ item: plant });
    const items = useWorldStore.getState().world!.items;
    expect(items.length).toBe(1);
    expect(items[0].kind).toBe("plant");
    expect((items[0].state as { stage?: string }).stage).toBe("sprout");
  });

  it("round-trips a seed kind through applyWorldPatch", () => {
    useWorldStore.getState().setWorld(makeSnapshot({ items: [] }));
    const seed = makeItem({
      id: 7,
      slug: "seed_packet_sunflower",
      name: "sunflower seed packet",
      kind: "seed",
      location_id: null,
      state: { species: "sunflower" },
    });
    useWorldStore.getState().applyWorldPatch({ item: seed });
    const items = useWorldStore.getState().world!.items;
    expect(items.length).toBe(1);
    expect(items[0].kind).toBe("seed");
    expect(items[0].location_id).toBeNull();
  });
});
