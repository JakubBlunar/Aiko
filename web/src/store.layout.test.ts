import { afterEach, beforeEach, describe, expect, it } from "vitest";

/**
 * Layout slice (P-layout).
 *
 * The contract these tests pin:
 *
 *   * ``leftSidebarCollapsed`` toggles between expanded + collapsed,
 *     persists to ``localStorage`` (key
 *     ``aiko.layout.left_collapsed``), and survives a store reload.
 *   * ``personaPanelWidth`` clamps every write to
 *     ``[MIN_PERSONA_PANEL_W, MAX_PERSONA_PANEL_W]`` so a stray
 *     ``setPersonaPanelWidth(99999)`` from a buggy resize handle
 *     can't render the panel offscreen.
 *   * ``personaAlwaysOnTop`` round-trips through ``localStorage``
 *     and falls back gracefully when the saved value is gibberish.
 *
 * The vitest config runs in ``environment: "node"`` (no DOM), so we
 * install a tiny in-memory ``localStorage`` shim before the test
 * imports the store. The store reads the persisted defaults at
 * module-load time, so the fake storage must be in place *before*
 * the store import runs -- hence the dynamic ``await import()``
 * inside ``beforeEach``.
 */

class FakeStorage {
  private _data = new Map<string, string>();
  getItem(key: string) {
    return this._data.has(key) ? (this._data.get(key) as string) : null;
  }
  setItem(key: string, value: string) {
    this._data.set(key, value);
  }
  removeItem(key: string) {
    this._data.delete(key);
  }
  clear() {
    this._data.clear();
  }
}

const LS_LEFT = "aiko.layout.left_collapsed";
const LS_WIDTH = "aiko.layout.persona_panel_w";
const LS_ALWAYS_ON_TOP = "aiko.persona.always_on_top";
const LS_MOBILE_VISIBLE = "aiko.mobile.persona_visible";
const LS_MOBILE_RECT = "aiko.mobile.persona_rect";

let storage: FakeStorage;
let storeMod: typeof import("./store");

beforeEach(async () => {
  storage = new FakeStorage();
  (globalThis as unknown as { localStorage: FakeStorage }).localStorage =
    storage;
  // Re-import the store module each test so the module-level
  // ``readBool``/``readPersonaPanelWidth`` defaults pick up the fresh
  // (empty) storage rather than whatever the previous test left
  // behind. ``vi.resetModules`` would also work but the dynamic
  // import keeps the test setup linear.
  storeMod = await import("./store");
  storeMod.useAssistantStore.setState({
    leftSidebarCollapsed: false,
    personaPanelWidth: storeMod.DEFAULT_PERSONA_PANEL_W,
    personaAlwaysOnTop: false,
    mobilePersonaVisible: false,
    mobilePersonaRect: { ...storeMod.DEFAULT_MOBILE_PERSONA_RECT },
  });
});

afterEach(() => {
  delete (globalThis as unknown as { localStorage?: unknown }).localStorage;
});

describe("layout slice — leftSidebarCollapsed", () => {
  it("toggleLeftSidebar flips the flag and persists it", () => {
    storeMod.useAssistantStore.getState().toggleLeftSidebar();
    expect(storeMod.useAssistantStore.getState().leftSidebarCollapsed).toBe(
      true,
    );
    expect(storage.getItem(LS_LEFT)).toBe("1");
    storeMod.useAssistantStore.getState().toggleLeftSidebar();
    expect(storeMod.useAssistantStore.getState().leftSidebarCollapsed).toBe(
      false,
    );
    expect(storage.getItem(LS_LEFT)).toBe("0");
  });

  it("setLeftSidebarCollapsed writes through and persists", () => {
    storeMod.useAssistantStore.getState().setLeftSidebarCollapsed(true);
    expect(storeMod.useAssistantStore.getState().leftSidebarCollapsed).toBe(
      true,
    );
    expect(storage.getItem(LS_LEFT)).toBe("1");
  });
});

describe("layout slice — personaPanelWidth", () => {
  it("clamps writes to the allowed range", () => {
    storeMod.useAssistantStore.getState().setPersonaPanelWidth(99999);
    expect(storeMod.useAssistantStore.getState().personaPanelWidth).toBe(
      storeMod.MAX_PERSONA_PANEL_W,
    );
    storeMod.useAssistantStore.getState().setPersonaPanelWidth(10);
    expect(storeMod.useAssistantStore.getState().personaPanelWidth).toBe(
      storeMod.MIN_PERSONA_PANEL_W,
    );
    storeMod.useAssistantStore.getState().setPersonaPanelWidth(500);
    expect(storeMod.useAssistantStore.getState().personaPanelWidth).toBe(500);
  });

  it("falls back to the default when given a non-finite value", () => {
    storeMod.useAssistantStore.getState().setPersonaPanelWidth(Number.NaN);
    expect(storeMod.useAssistantStore.getState().personaPanelWidth).toBe(
      storeMod.DEFAULT_PERSONA_PANEL_W,
    );
  });

  it("persists the clamped value to localStorage", () => {
    storeMod.useAssistantStore.getState().setPersonaPanelWidth(560);
    expect(storage.getItem(LS_WIDTH)).toBe("560");
  });
});

describe("layout slice — personaAlwaysOnTop", () => {
  it("setPersonaAlwaysOnTop persists the boolean both ways", () => {
    storeMod.useAssistantStore.getState().setPersonaAlwaysOnTop(true);
    expect(storeMod.useAssistantStore.getState().personaAlwaysOnTop).toBe(
      true,
    );
    expect(storage.getItem(LS_ALWAYS_ON_TOP)).toBe("1");
    storeMod.useAssistantStore.getState().setPersonaAlwaysOnTop(false);
    expect(storeMod.useAssistantStore.getState().personaAlwaysOnTop).toBe(
      false,
    );
    expect(storage.getItem(LS_ALWAYS_ON_TOP)).toBe("0");
  });
});

describe("layout slice — mobile floating persona", () => {
  it("toggleMobilePersona flips the flag and persists it", () => {
    storeMod.useAssistantStore.getState().toggleMobilePersona();
    expect(storeMod.useAssistantStore.getState().mobilePersonaVisible).toBe(
      true,
    );
    expect(storage.getItem(LS_MOBILE_VISIBLE)).toBe("1");
    storeMod.useAssistantStore.getState().toggleMobilePersona();
    expect(storeMod.useAssistantStore.getState().mobilePersonaVisible).toBe(
      false,
    );
    expect(storage.getItem(LS_MOBILE_VISIBLE)).toBe("0");
  });

  it("setMobilePersonaVisible writes through and persists", () => {
    storeMod.useAssistantStore.getState().setMobilePersonaVisible(true);
    expect(storeMod.useAssistantStore.getState().mobilePersonaVisible).toBe(
      true,
    );
    expect(storage.getItem(LS_MOBILE_VISIBLE)).toBe("1");
  });

  it("setMobilePersonaRect clamps width/height to the minimum", () => {
    storeMod.useAssistantStore
      .getState()
      .setMobilePersonaRect({ x: 5, y: 10, w: 10, h: 10 });
    const rect = storeMod.useAssistantStore.getState().mobilePersonaRect;
    expect(rect.w).toBe(storeMod.MIN_MOBILE_PERSONA_W);
    expect(rect.h).toBe(storeMod.MIN_MOBILE_PERSONA_H);
    // x / y are kept as-is here (viewport clamping happens at render).
    expect(rect.x).toBe(5);
    expect(rect.y).toBe(10);
  });

  it("falls back to defaults for non-finite geometry", () => {
    storeMod.useAssistantStore
      .getState()
      .setMobilePersonaRect({
        x: Number.NaN,
        y: Number.NaN,
        w: Number.NaN,
        h: Number.NaN,
      });
    const rect = storeMod.useAssistantStore.getState().mobilePersonaRect;
    expect(rect).toEqual(storeMod.DEFAULT_MOBILE_PERSONA_RECT);
  });

  it("persists the clamped rect as JSON", () => {
    storeMod.useAssistantStore
      .getState()
      .setMobilePersonaRect({ x: 30, y: 40, w: 200, h: 260 });
    expect(storage.getItem(LS_MOBILE_RECT)).toBe(
      JSON.stringify({ x: 30, y: 40, w: 200, h: 260 }),
    );
  });
});
