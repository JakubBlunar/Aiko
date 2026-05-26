import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "./store";

/**
 * Covers the Zustand setters introduced for the Tauri persona-window
 * pipeline. The WS handler in ``useAssistantSocket.ts`` does the
 * receive-side work of dispatching ``setPersonaWindow(evt.persona_window)``;
 * exercising the setter directly verifies the same state mutation
 * without standing up the full hook (which needs jsdom + a fake WS).
 */

const RESET_DESKTOP = { desktop: null } as const;

describe("desktop slice — setDesktop", () => {
  beforeEach(() => {
    useAssistantStore.setState(RESET_DESKTOP);
  });

  it("seeds the slice from a hello payload", () => {
    useAssistantStore.getState().setDesktop({
      persona_window: { width: 360, height: 520, always_on_top: false },
    });
    const slice = useAssistantStore.getState().desktop;
    expect(slice).toEqual({
      persona_window: { width: 360, height: 520, always_on_top: false },
    });
  });

  it("can be cleared by passing null", () => {
    useAssistantStore.getState().setDesktop({
      persona_window: { width: 320, height: 480, always_on_top: true },
    });
    useAssistantStore.getState().setDesktop(null);
    expect(useAssistantStore.getState().desktop).toBeNull();
  });
});

describe("desktop slice — setPersonaWindow", () => {
  beforeEach(() => {
    useAssistantStore.setState(RESET_DESKTOP);
  });

  it("synthesises the slice on the first patch when desktop is null", () => {
    useAssistantStore.getState().setPersonaWindow({ width: 400 });
    const slice = useAssistantStore.getState().desktop;
    expect(slice).toEqual({
      // Width landed; height/always_on_top fall back to dataclass-style
      // defaults so the renderer never sees ``undefined`` for those.
      persona_window: { width: 400, height: 480, always_on_top: true },
    });
  });

  it("merges into an existing slice without stomping unrelated keys", () => {
    useAssistantStore.getState().setDesktop({
      persona_window: { width: 320, height: 480, always_on_top: true },
    });
    useAssistantStore.getState().setPersonaWindow({ height: 700 });
    expect(useAssistantStore.getState().desktop).toEqual({
      persona_window: { width: 320, height: 700, always_on_top: true },
    });
  });

  it("supports a full replace via patch", () => {
    useAssistantStore.getState().setDesktop({
      persona_window: { width: 320, height: 480, always_on_top: true },
    });
    useAssistantStore.getState().setPersonaWindow({
      width: 240,
      height: 360,
      always_on_top: false,
    });
    expect(useAssistantStore.getState().desktop).toEqual({
      persona_window: { width: 240, height: 360, always_on_top: false },
    });
  });
});
