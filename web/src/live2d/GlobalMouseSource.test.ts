import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GlobalMouseSource } from "./GlobalMouseSource";
import type { CursorApi } from "../desktop/cursor";

/**
 * Tests for the Tauri-only ``GlobalMouseSource``.
 *
 * Vitest runs in the Node environment by default so there's no DOM —
 * we stub the DOM container, ``window`` globals, and the cursor API
 * directly. The point of these tests is to lock down the translation
 * math and the polling lifecycle, not to exercise the real Tauri IPC.
 */

interface FakeContainerOptions {
  left?: number;
  top?: number;
  width?: number;
  height?: number;
}

function makeContainer(options: FakeContainerOptions = {}) {
  const rect = {
    left: options.left ?? 0,
    top: options.top ?? 0,
    width: options.width ?? 320,
    height: options.height ?? 480,
  };
  return {
    getBoundingClientRect: () => ({
      ...rect,
      right: rect.left + rect.width,
      bottom: rect.top + rect.height,
      x: rect.left,
      y: rect.top,
      toJSON: () => rect,
    }),
  } as unknown as HTMLElement;
}

interface CursorApiState {
  cursor: { x: number; y: number } | null;
  geometry: { innerX: number; innerY: number; scaleFactor: number } | null;
  movedHandler: (() => void) | null;
  scaleHandler: (() => void) | null;
  movedUnlisten: ReturnType<typeof vi.fn<() => void>>;
  scaleUnlisten: ReturnType<typeof vi.fn<() => void>>;
  geometryCalls: number;
}

function makeCursorApi(): { api: CursorApi; state: CursorApiState } {
  const state: CursorApiState = {
    cursor: { x: 0, y: 0 },
    geometry: { innerX: 0, innerY: 0, scaleFactor: 1 },
    movedHandler: null,
    scaleHandler: null,
    movedUnlisten: vi.fn<() => void>(() => undefined),
    scaleUnlisten: vi.fn<() => void>(() => undefined),
    geometryCalls: 0,
  };
  const api: CursorApi = {
    getCursorPositionPhysical: async () => state.cursor,
    getCurrentWindowGeometry: async () => {
      state.geometryCalls += 1;
      return state.geometry;
    },
    onWindowMoved: async (handler) => {
      state.movedHandler = handler;
      return state.movedUnlisten;
    },
    onScaleFactorChanged: async (handler) => {
      state.scaleHandler = handler;
      return state.scaleUnlisten;
    },
  };
  return { api, state };
}

interface ManualScheduler {
  schedule: (cb: FrameRequestCallback) => number;
  cancel: (handle: number) => void;
  /** Run every pending RAF callback once, in order. Returns the
   * count drained — useful for sanity-asserting the loop is alive. */
  drain: () => Promise<number>;
}

function makeScheduler(): ManualScheduler {
  const pending = new Map<number, FrameRequestCallback>();
  let nextId = 1;
  return {
    schedule: (cb) => {
      const id = nextId++;
      pending.set(id, cb);
      return id;
    },
    cancel: (id) => {
      pending.delete(id);
    },
    drain: async () => {
      const callbacks = Array.from(pending.values());
      pending.clear();
      callbacks.forEach((cb) => cb(performance.now()));
      // Each callback's poll resolves on the microtask queue; flush
      // it so the source has actually written ``_x / _y`` before the
      // test's next assertion runs.
      await Promise.resolve();
      await Promise.resolve();
      return callbacks.length;
    },
  };
}

function installFakeWindow() {
  // ``GlobalMouseSource.subscribe`` reads ``document.hasFocus()`` and
  // attaches focus / blur listeners to ``window``. We stub both
  // globals so the source initialises cleanly under Node.
  const win = {
    innerWidth: 320,
    innerHeight: 480,
    addEventListener: vi.fn(),
    removeEventListener: vi.fn(),
  };
  (globalThis as unknown as { window: typeof win }).window = win;
  (globalThis as unknown as {
    document: { hasFocus: () => boolean };
  }).document = { hasFocus: () => true };
  return win;
}

function clearFakeWindow() {
  delete (globalThis as unknown as Record<string, unknown>).window;
  delete (globalThis as unknown as Record<string, unknown>).document;
}

describe("GlobalMouseSource — translation", () => {
  beforeEach(() => {
    installFakeWindow();
  });
  afterEach(() => {
    clearFakeWindow();
  });

  it("subtracts the window inner position and divides by scale factor", async () => {
    const { api, state } = makeCursorApi();
    state.cursor = { x: 800, y: 300 };
    state.geometry = { innerX: 500, innerY: 200, scaleFactor: 1 };
    const sched = makeScheduler();
    const source = new GlobalMouseSource({
      container: makeContainer(),
      cursorApi: api,
      scheduleFrame: sched.schedule,
      cancelFrame: sched.cancel,
      now: () => 1000,
    });
    const teardown = source.subscribe();

    // Two RAF drains: first drives the geometry probe (it primes the
    // cache asynchronously), second performs the cursor poll that
    // actually writes ``_x / _y``.
    await sched.drain();
    await sched.drain();

    const snap = source.snapshot();
    expect(snap.x).toBe(300); // (800 - 500) / 1
    expect(snap.y).toBe(100); // (300 - 200) / 1
    expect(snap.lastMoveAt).toBe(1000);
    teardown();
  });

  it("respects HiDPI scale factors", async () => {
    const { api, state } = makeCursorApi();
    state.cursor = { x: 1500, y: 600 };
    state.geometry = { innerX: 600, innerY: 300, scaleFactor: 1.5 };
    const sched = makeScheduler();
    const source = new GlobalMouseSource({
      container: makeContainer(),
      cursorApi: api,
      scheduleFrame: sched.schedule,
      cancelFrame: sched.cancel,
    });
    const teardown = source.subscribe();
    await sched.drain();
    await sched.drain();

    const snap = source.snapshot();
    // (1500 - 600) / 1.5 = 600
    expect(snap.x).toBe(600);
    // (600 - 300) / 1.5 = 200
    expect(snap.y).toBe(200);
    teardown();
  });

  it("reports negative coordinates when the cursor is on a left-side monitor", async () => {
    const { api, state } = makeCursorApi();
    // Persona window sits at (500, 300) on the primary monitor;
    // cursor is at (-800, 400) on a secondary monitor placed to the
    // left in the OS display arrangement. The subtraction must
    // preserve the negative offset so ``GazeChannel``'s clamps can
    // saturate the X axis.
    state.cursor = { x: -800, y: 400 };
    state.geometry = { innerX: 500, innerY: 300, scaleFactor: 1 };
    const sched = makeScheduler();
    const source = new GlobalMouseSource({
      container: makeContainer(),
      cursorApi: api,
      scheduleFrame: sched.schedule,
      cancelFrame: sched.cancel,
    });
    const teardown = source.subscribe();
    await sched.drain();
    await sched.drain();

    const snap = source.snapshot();
    expect(snap.x).toBe(-1300);
    expect(snap.y).toBe(100);
    teardown();
  });
});

describe("GlobalMouseSource — lifecycle", () => {
  beforeEach(() => {
    installFakeWindow();
  });
  afterEach(() => {
    clearFakeWindow();
  });

  it("only updates lastMoveAt when the cursor actually moves", async () => {
    const { api, state } = makeCursorApi();
    state.cursor = { x: 800, y: 300 };
    state.geometry = { innerX: 500, innerY: 200, scaleFactor: 1 };
    let nowValue = 1000;
    const sched = makeScheduler();
    const source = new GlobalMouseSource({
      container: makeContainer(),
      cursorApi: api,
      scheduleFrame: sched.schedule,
      cancelFrame: sched.cancel,
      now: () => nowValue,
    });
    const teardown = source.subscribe();
    await sched.drain();
    await sched.drain();
    expect(source.snapshot().lastMoveAt).toBe(1000);

    // Cursor sits still — even though we tick the clock, lastMoveAt
    // must not advance, otherwise GazeChannel's idle-break never
    // fires.
    nowValue = 2000;
    await sched.drain();
    expect(source.snapshot().lastMoveAt).toBe(1000);

    // Real movement → timestamp advances.
    state.cursor = { x: 850, y: 320 };
    nowValue = 3000;
    await sched.drain();
    expect(source.snapshot().x).toBe(350);
    expect(source.snapshot().lastMoveAt).toBe(3000);
    teardown();
  });

  it("refreshes cached geometry when the move handler fires", async () => {
    const { api, state } = makeCursorApi();
    state.cursor = { x: 800, y: 300 };
    state.geometry = { innerX: 500, innerY: 200, scaleFactor: 1 };
    const sched = makeScheduler();
    const source = new GlobalMouseSource({
      container: makeContainer(),
      cursorApi: api,
      scheduleFrame: sched.schedule,
      cancelFrame: sched.cancel,
    });
    const teardown = source.subscribe();
    await sched.drain();
    await sched.drain();
    expect(source.snapshot().x).toBe(300);
    const initialCalls = state.geometryCalls;

    // User drags the persona window to (700, 300). Until the move
    // handler fires + the async geometry probe resolves, the cursor
    // translation continues to use the stale (500, 200) origin.
    state.geometry = { innerX: 700, innerY: 300, scaleFactor: 1 };
    expect(state.movedHandler).not.toBeNull();
    state.movedHandler?.();
    // Flush the async refresh + the next RAF tick.
    await Promise.resolve();
    await Promise.resolve();
    await sched.drain();
    expect(state.geometryCalls).toBeGreaterThan(initialCalls);
    expect(source.snapshot().x).toBe(100); // 800 - 700
    teardown();
  });

  it("teardown stops the RAF loop and unsubscribes Tauri listeners", async () => {
    const { api, state } = makeCursorApi();
    state.cursor = { x: 800, y: 300 };
    state.geometry = { innerX: 500, innerY: 200, scaleFactor: 1 };
    const sched = makeScheduler();
    const source = new GlobalMouseSource({
      container: makeContainer(),
      cursorApi: api,
      scheduleFrame: sched.schedule,
      cancelFrame: sched.cancel,
    });
    const teardown = source.subscribe();
    await sched.drain();
    await sched.drain();
    // Loop alive: each drain re-schedules the next tick.
    expect(await sched.drain()).toBe(1);

    teardown();
    expect(state.movedUnlisten).toHaveBeenCalledTimes(1);
    expect(state.scaleUnlisten).toHaveBeenCalledTimes(1);
    // Subsequent drains find no pending callbacks because the loop
    // is dead and ``cancelFrame`` cleared the last handle.
    expect(await sched.drain()).toBe(0);
  });

  it("never writes coordinates before the geometry probe lands", async () => {
    // Slow geometry probe — resolves only after several RAF ticks.
    let releaseGeometry: (
      value: { innerX: number; innerY: number; scaleFactor: number } | null,
    ) => void = () => {};
    const geometryPromise = new Promise<{
      innerX: number;
      innerY: number;
      scaleFactor: number;
    } | null>((resolve) => {
      releaseGeometry = resolve;
    });
    const api: CursorApi = {
      getCursorPositionPhysical: async () => ({ x: 800, y: 300 }),
      getCurrentWindowGeometry: () => geometryPromise,
      onWindowMoved: async () => () => undefined,
      onScaleFactorChanged: async () => () => undefined,
    };
    const sched = makeScheduler();
    const source = new GlobalMouseSource({
      container: makeContainer(),
      cursorApi: api,
      scheduleFrame: sched.schedule,
      cancelFrame: sched.cancel,
    });
    const teardown = source.subscribe();

    // Several frames worth of cursor polls fire while geometry is
    // still pending. The source must keep ``x / y`` at ``null``;
    // otherwise the renderer briefly tracks against (0, 0) which
    // visibly snaps when the real geometry lands.
    for (let i = 0; i < 5; i++) {
      await sched.drain();
    }
    expect(source.snapshot().x).toBeNull();
    expect(source.snapshot().y).toBeNull();

    releaseGeometry({ innerX: 500, innerY: 200, scaleFactor: 1 });
    // Two flushes: one for the geometry resolution to land in the
    // cache, one for the next cursor poll to read it.
    await Promise.resolve();
    await Promise.resolve();
    await sched.drain();
    expect(source.snapshot().x).toBe(300);
    teardown();
  });
});

describe("GlobalMouseSource — defensive paths", () => {
  beforeEach(() => {
    installFakeWindow();
  });
  afterEach(() => {
    clearFakeWindow();
  });

  it("ignores cursor polls that resolve to null (e.g. webview shutdown)", async () => {
    const api: CursorApi = {
      getCursorPositionPhysical: async () => null,
      getCurrentWindowGeometry: async () => ({
        innerX: 500,
        innerY: 200,
        scaleFactor: 1,
      }),
      onWindowMoved: async () => () => undefined,
      onScaleFactorChanged: async () => () => undefined,
    };
    const sched = makeScheduler();
    const source = new GlobalMouseSource({
      container: makeContainer(),
      cursorApi: api,
      scheduleFrame: sched.schedule,
      cancelFrame: sched.cancel,
    });
    const teardown = source.subscribe();
    await sched.drain();
    await sched.drain();
    await sched.drain();
    expect(source.snapshot().x).toBeNull();
    expect(source.snapshot().y).toBeNull();
    teardown();
  });
});
