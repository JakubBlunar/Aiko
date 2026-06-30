import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "../../store";

/**
 * Tests for the B8 "listening face" ``composing`` flag.
 *
 * The chat composer flips this on each keystroke and back off on send /
 * blur / an idle debounce. The avatar engine polls it per gaze tick so
 * typed mode reads as attentive listening (GazeChannel eye-contact +
 * AmbientBodyChannel lean-in). It's ephemeral UI state — never persisted,
 * never sent to the backend.
 */

describe("ui slice — setComposing", () => {
  beforeEach(() => {
    useAssistantStore.setState({ composing: false });
  });

  it("starts false (relaxed pose)", () => {
    expect(useAssistantStore.getState().composing).toBe(false);
  });

  it("flips on and off", () => {
    useAssistantStore.getState().setComposing(true);
    expect(useAssistantStore.getState().composing).toBe(true);
    useAssistantStore.getState().setComposing(false);
    expect(useAssistantStore.getState().composing).toBe(false);
  });

  it("coerces truthy / falsy values to booleans", () => {
    useAssistantStore.getState().setComposing(1 as unknown as boolean);
    expect(useAssistantStore.getState().composing).toBe(true);
    useAssistantStore.getState().setComposing(0 as unknown as boolean);
    expect(useAssistantStore.getState().composing).toBe(false);
  });

  it("is a no-op (same reference) when the value is unchanged", () => {
    useAssistantStore.getState().setComposing(true);
    const before = useAssistantStore.getState();
    useAssistantStore.getState().setComposing(true);
    const after = useAssistantStore.getState();
    expect(after.composing).toBe(true);
    expect(after).toBe(before);
  });
});
