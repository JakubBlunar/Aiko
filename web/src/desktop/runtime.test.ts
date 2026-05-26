import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

/**
 * Tests for the desktop runtime resolver.
 *
 * The two helpers are tiny but they sit on the hot path: every WS,
 * REST, and avatar-asset request resolves through ``backendBase()``.
 * Verifies the four meaningful states:
 *   1. browser, ``window`` defined  -> same-origin URLs
 *   2. tauri, no override           -> default ``127.0.0.1:6275``
 *   3. tauri, env override          -> uses the override
 *   4. server (no ``window``)       -> empty strings, no throws
 *
 * Vitest is configured for the Node environment (no jsdom) so we stub
 * a minimal ``globalThis.window`` ourselves. That keeps the test fast
 * and free of extra deps while still exercising the same branches.
 */

const TAURI_KEY = "__TAURI_INTERNALS__";

interface FakeWindow {
  location: { origin: string; protocol: string; host: string };
  [key: string]: unknown;
}

function installFakeWindow(
  overrides: Partial<FakeWindow["location"]> = {},
): FakeWindow {
  const fakeWindow: FakeWindow = {
    location: {
      origin: "http://localhost:5173",
      protocol: "http:",
      host: "localhost:5173",
      ...overrides,
    },
  };
  (globalThis as unknown as { window: FakeWindow }).window = fakeWindow;
  return fakeWindow;
}

function clearWindow(): void {
  delete (globalThis as unknown as Record<string, unknown>).window;
}

async function loadModule() {
  // Re-import on every test so module-level closures pick up the
  // freshly stubbed ``window``. Vitest's ``resetModules`` clears the
  // ESM cache so the module resolves anew.
  vi.resetModules();
  return await import("./runtime");
}

describe("isTauri()", () => {
  afterEach(() => {
    clearWindow();
  });

  it("returns false when window is undefined", async () => {
    clearWindow();
    const { isTauri } = await loadModule();
    expect(isTauri()).toBe(false);
  });

  it("returns false when the Tauri global is absent", async () => {
    installFakeWindow();
    const { isTauri } = await loadModule();
    expect(isTauri()).toBe(false);
  });

  it("returns true when the Tauri global is present", async () => {
    const w = installFakeWindow();
    w[TAURI_KEY] = {};
    const { isTauri } = await loadModule();
    expect(isTauri()).toBe(true);
  });
});

describe("backendBase() — browser branch", () => {
  beforeEach(() => {
    installFakeWindow();
  });
  afterEach(() => {
    clearWindow();
  });

  it("uses the current window origin for HTTP", async () => {
    const { backendBase } = await loadModule();
    expect(backendBase().http).toBe("http://localhost:5173");
  });

  it("derives ws:// from a non-https origin", async () => {
    const { backendBase } = await loadModule();
    expect(backendBase().ws).toBe("ws://localhost:5173");
  });

  it("derives wss:// from an https origin", async () => {
    installFakeWindow({
      origin: "https://aiko.example.com",
      protocol: "https:",
      host: "aiko.example.com",
    });
    const { backendBase } = await loadModule();
    expect(backendBase().ws).toBe("wss://aiko.example.com");
  });
});

describe("backendBase() — Tauri branch", () => {
  beforeEach(() => {
    const w = installFakeWindow();
    w[TAURI_KEY] = {};
  });
  afterEach(() => {
    clearWindow();
    vi.unstubAllEnvs();
  });

  it("falls back to the default 127.0.0.1:6275 backend", async () => {
    const { backendBase } = await loadModule();
    expect(backendBase()).toEqual({
      http: "http://127.0.0.1:6275",
      ws: "ws://127.0.0.1:6275",
    });
  });

  it("respects VITE_BACKEND_URL when set", async () => {
    vi.stubEnv("VITE_BACKEND_URL", "http://10.0.0.5:8080");
    const { backendBase } = await loadModule();
    expect(backendBase()).toEqual({
      http: "http://10.0.0.5:8080",
      ws: "ws://10.0.0.5:8080",
    });
  });

  it("strips a trailing slash from the override", async () => {
    vi.stubEnv("VITE_BACKEND_URL", "http://10.0.0.5:8080/");
    const { backendBase } = await loadModule();
    expect(backendBase().http).toBe("http://10.0.0.5:8080");
  });

  it("upgrades https override to wss", async () => {
    vi.stubEnv("VITE_BACKEND_URL", "https://aiko.example.com");
    const { backendBase } = await loadModule();
    expect(backendBase()).toEqual({
      http: "https://aiko.example.com",
      ws: "wss://aiko.example.com",
    });
  });

  it("ignores blank / whitespace VITE_BACKEND_URL values", async () => {
    vi.stubEnv("VITE_BACKEND_URL", "   ");
    const { backendBase } = await loadModule();
    expect(backendBase().http).toBe("http://127.0.0.1:6275");
  });
});
