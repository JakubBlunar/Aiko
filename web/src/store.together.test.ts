import { beforeEach, describe, expect, it } from "vitest";

import { useTogetherStore } from "./stores/useTogetherStore";
import type {
  RelationshipAxes,
  SharedMoment,
  TogetherSummary,
} from "./types";

/**
 * Tests for the ``togetherView`` slice of the Zustand store.
 *
 * Covers the slice's CRUD + WS reducer behaviour:
 *  - setting / clearing summary + moments
 *  - upsertSharedMoment chronological insert and update-in-place
 *  - upsertSharedMoment respecting the active vibe filter
 *  - removeSharedMoment decrementing total
 *  - setRelationshipAxes updating the summary in-place
 *  - setTogetherVibeFilter clearing the page back to 0
 */

function freshState() {
  return {
    togetherView: {
      summary: null as TogetherSummary | null,
      moments: [] as SharedMoment[],
      total: 0,
      page: 0,
      pageSize: 20,
      vibeFilter: null as string | null,
      loading: false,
    },
  };
}

function makeMoment(
  id: number,
  when: string,
  vibe: SharedMoment["vibe"] = "warm",
  overrides: Partial<SharedMoment> = {},
): SharedMoment {
  return {
    id,
    summary: `moment ${id}`,
    vibe,
    when,
    created_at: when,
    salience: 0.7,
    pinned: false,
    source: "manual",
    confidence: 1.0,
    source_message_ids: [],
    last_anniversaried_at: null,
    ...overrides,
  };
}

function makeSummary(overrides: Partial<TogetherSummary> = {}): TogetherSummary {
  return {
    phase: "anchored",
    days_known: 42,
    total_turns: 999,
    total_sessions: 17,
    first_seen_at: "2026-01-01T00:00:00+00:00",
    axes: {
      user_id: "jacob",
      closeness: 0.4,
      humor: 0.2,
      trust: 0.3,
      comfort: 0.1,
      updated_at: "2026-05-27T12:00:00+00:00",
    },
    milestones: [],
    anniversary_today: null,
    recent_moments_count: 0,
    ...overrides,
  };
}

describe("togetherView slice — setters", () => {
  beforeEach(() => {
    useTogetherStore.setState(freshState());
  });

  it("setTogetherSummary stores a summary", () => {
    const s = makeSummary();
    useTogetherStore.getState().setTogetherSummary(s);
    expect(useTogetherStore.getState().togetherView.summary).toEqual(s);
  });

  it("setSharedMoments replaces the page", () => {
    const moments = [
      makeMoment(1, "2026-05-01T12:00:00+00:00"),
      makeMoment(2, "2026-05-02T12:00:00+00:00"),
    ];
    useTogetherStore
      .getState()
      .setSharedMoments(moments, 2, 0, 20, null);
    const tv = useTogetherStore.getState().togetherView;
    expect(tv.moments).toEqual(moments);
    expect(tv.total).toBe(2);
    expect(tv.page).toBe(0);
  });

  it("setTogetherLoading toggles loading", () => {
    useTogetherStore.getState().setTogetherLoading(true);
    expect(useTogetherStore.getState().togetherView.loading).toBe(true);
    useTogetherStore.getState().setTogetherLoading(false);
    expect(useTogetherStore.getState().togetherView.loading).toBe(false);
  });

  it("setTogetherVibeFilter resets the page to 0", () => {
    useTogetherStore.setState({
      togetherView: { ...freshState().togetherView, page: 4, vibeFilter: null },
    });
    useTogetherStore.getState().setTogetherVibeFilter("warm");
    const tv = useTogetherStore.getState().togetherView;
    expect(tv.vibeFilter).toBe("warm");
    expect(tv.page).toBe(0);
  });
});

describe("togetherView slice — upsertSharedMoment", () => {
  beforeEach(() => {
    useTogetherStore.setState(freshState());
  });

  it("inserts a new moment in chronological order (newest first)", () => {
    useTogetherStore.getState().setSharedMoments(
      [
        makeMoment(1, "2026-05-01T12:00:00+00:00"),
        makeMoment(2, "2026-04-01T12:00:00+00:00"),
      ],
      2,
      0,
      20,
      null,
    );
    useTogetherStore
      .getState()
      .upsertSharedMoment(makeMoment(3, "2026-05-15T12:00:00+00:00"));
    const tv = useTogetherStore.getState().togetherView;
    expect(tv.moments.map((m) => m.id)).toEqual([3, 1, 2]);
    expect(tv.total).toBe(3);
  });

  it("updates an existing moment in place without touching total", () => {
    useTogetherStore.getState().setSharedMoments(
      [makeMoment(1, "2026-05-01T12:00:00+00:00", "warm")],
      1,
      0,
      20,
      null,
    );
    useTogetherStore.getState().upsertSharedMoment(
      makeMoment(1, "2026-05-01T12:00:00+00:00", "tender", {
        summary: "edited",
      }),
    );
    const tv = useTogetherStore.getState().togetherView;
    expect(tv.moments).toHaveLength(1);
    expect(tv.moments[0].vibe).toBe("tender");
    expect(tv.moments[0].summary).toBe("edited");
    expect(tv.total).toBe(1);
  });

  it("drops a vibe-mismatched moment from the active filter view", () => {
    useTogetherStore.getState().setSharedMoments(
      [makeMoment(1, "2026-05-01T12:00:00+00:00", "warm")],
      1,
      0,
      20,
      "warm",
    );
    useTogetherStore.getState().upsertSharedMoment(
      makeMoment(1, "2026-05-01T12:00:00+00:00", "playful"),
    );
    const tv = useTogetherStore.getState().togetherView;
    expect(tv.moments).toEqual([]);
    expect(tv.total).toBe(0);
  });

  it("ignores a new mismatched moment when a vibe filter is active", () => {
    useTogetherStore.getState().setSharedMoments(
      [],
      0,
      0,
      20,
      "warm",
    );
    useTogetherStore.getState().upsertSharedMoment(
      makeMoment(99, "2026-05-01T12:00:00+00:00", "playful"),
    );
    const tv = useTogetherStore.getState().togetherView;
    expect(tv.moments).toEqual([]);
    expect(tv.total).toBe(0);
  });
});

describe("togetherView slice — removeSharedMoment", () => {
  beforeEach(() => {
    useTogetherStore.setState(freshState());
  });

  it("removes the row and decrements total", () => {
    useTogetherStore.getState().setSharedMoments(
      [
        makeMoment(1, "2026-05-01T12:00:00+00:00"),
        makeMoment(2, "2026-04-01T12:00:00+00:00"),
      ],
      2,
      0,
      20,
      null,
    );
    useTogetherStore.getState().removeSharedMoment(1);
    const tv = useTogetherStore.getState().togetherView;
    expect(tv.moments.map((m) => m.id)).toEqual([2]);
    expect(tv.total).toBe(1);
  });

  it("is a no-op when the moment is not on the current page", () => {
    useTogetherStore.getState().setSharedMoments(
      [makeMoment(1, "2026-05-01T12:00:00+00:00")],
      5,
      0,
      20,
      null,
    );
    useTogetherStore.getState().removeSharedMoment(999);
    const tv = useTogetherStore.getState().togetherView;
    expect(tv.moments).toHaveLength(1);
    expect(tv.total).toBe(5);
  });
});

describe("togetherView slice — setRelationshipAxes", () => {
  beforeEach(() => {
    useTogetherStore.setState(freshState());
  });

  it("updates the axes inside the summary", () => {
    useTogetherStore.getState().setTogetherSummary(makeSummary());
    const next: RelationshipAxes = {
      user_id: "jacob",
      closeness: 0.9,
      humor: 0.8,
      trust: 0.7,
      comfort: 0.6,
      updated_at: "2026-05-28T00:00:00+00:00",
    };
    useTogetherStore.getState().setRelationshipAxes(next);
    expect(
      useTogetherStore.getState().togetherView.summary?.axes,
    ).toEqual(next);
  });

  it("is a no-op when no summary is loaded yet", () => {
    const next: RelationshipAxes = {
      user_id: "jacob",
      closeness: 0.9,
      humor: 0.8,
      trust: 0.7,
      comfort: 0.6,
      updated_at: "2026-05-28T00:00:00+00:00",
    };
    useTogetherStore.getState().setRelationshipAxes(next);
    expect(useTogetherStore.getState().togetherView.summary).toBeNull();
  });
});

