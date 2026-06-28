import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "../../store";

/**
 * Tests for the activity-awareness slice of the Zustand store.
 *
 * The settings drawer + ``App.tsx`` mirror server-side state into the
 * ``activityAwarenessEnabled`` flag so the activity reporter hook can
 * start / stop without a reload. ``liveActiveApp`` is a display-only
 * mirror used by the "Currently sees: <App>" readout.
 */

const RESET = {
  activityAwarenessEnabled: false,
  liveActiveApp: null,
} as const;

describe("activity slice — setActivityAwarenessEnabled", () => {
  beforeEach(() => {
    useAssistantStore.setState(RESET);
  });

  it("starts disabled by default (privacy-respecting)", () => {
    expect(useAssistantStore.getState().activityAwarenessEnabled).toBe(false);
  });

  it("stores boolean true", () => {
    useAssistantStore.getState().setActivityAwarenessEnabled(true);
    expect(useAssistantStore.getState().activityAwarenessEnabled).toBe(true);
  });

  it("coerces truthy / falsy values to booleans", () => {
    useAssistantStore.getState().setActivityAwarenessEnabled(
      true as unknown as boolean,
    );
    expect(useAssistantStore.getState().activityAwarenessEnabled).toBe(true);
    useAssistantStore.getState().setActivityAwarenessEnabled(
      0 as unknown as boolean,
    );
    expect(useAssistantStore.getState().activityAwarenessEnabled).toBe(false);
  });
});

describe("activity slice — setLiveActiveApp", () => {
  beforeEach(() => {
    useAssistantStore.setState(RESET);
  });

  it("stores the latest app string", () => {
    useAssistantStore.getState().setLiveActiveApp("Code");
    expect(useAssistantStore.getState().liveActiveApp).toBe("Code");
  });

  it("supports clearing back to null", () => {
    useAssistantStore.getState().setLiveActiveApp("Code");
    useAssistantStore.getState().setLiveActiveApp(null);
    expect(useAssistantStore.getState().liveActiveApp).toBeNull();
  });

  it("treats undefined as null", () => {
    useAssistantStore
      .getState()
      .setLiveActiveApp(undefined as unknown as string | null);
    expect(useAssistantStore.getState().liveActiveApp).toBeNull();
  });
});
