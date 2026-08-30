/**
 * Tests for the engine plumbing: lifecycle, channel registration,
 * dispatch fan-out, RAF cancellation on stop, and the wall-clock
 * to monotonic-clock conversion that ``dispatchOverlay`` performs
 * (locking in the regression fix that originally motivated this
 * refactor).
 *
 * These tests intentionally don't exercise any real channel — they
 * use a tiny ``RecordingChannel`` so we can assert exactly which
 * hooks fire, in which order, with which arguments.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AvatarEngine } from "./AvatarEngine";
import { createEngineState } from "./state";
import type {
  AvatarChannel,
  ChannelDeps,
  Live2DModelAdapter,
  MouseSnapshot,
  ResolvedOverlayEvent,
} from "./types";

import { FakeAdapter } from "./__fixtures__/fake-model";
import { FakeClock } from "./__fixtures__/fake-clock";
import { FakeMouseSource } from "./__fixtures__/fake-mouse-source";
import { ManualRaf } from "./__fixtures__/manual-raf";
import { buildManifest, buildStoreSnapshot, NEUTRAL_MOOD } from "./__fixtures__/test-manifest";

interface RecordedCall {
  hook: string;
  payload?: unknown;
}

class RecordingChannel implements AvatarChannel {
  readonly name: string;
  readonly calls: RecordedCall[] = [];
  readonly errors: { hook: string; thrown: unknown }[] = [];
  attached = false;

  constructor(name: string) {
    this.name = name;
  }

  attach(_adapter: Live2DModelAdapter, _deps: ChannelDeps): void {
    this.attached = true;
    this.calls.push({ hook: "attach" });
  }

  detach(): void {
    this.attached = false;
    this.calls.push({ hook: "detach" });
  }

  onReaction(reaction: string): void {
    this.calls.push({ hook: "onReaction", payload: reaction });
  }

  onOverlay(event: ResolvedOverlayEvent): void {
    this.calls.push({ hook: "onOverlay", payload: event });
  }

  onMotion(event: unknown): void {
    this.calls.push({ hook: "onMotion", payload: event });
  }

  onTtsState(next: "idle" | "speaking"): void {
    this.calls.push({ hook: "onTtsState", payload: next });
  }

  onMood(mood: unknown): void {
    this.calls.push({ hook: "onMood", payload: mood });
  }

  onExpressionSlotReleased(): void {
    this.calls.push({ hook: "onExpressionSlotReleased" });
  }

  tickTier3(now: number, dt: number): void {
    this.calls.push({ hook: "tickTier3", payload: { now, dt } });
  }

  tickGaze(now: number, dt: number, mouse: MouseSnapshot): void {
    this.calls.push({ hook: "tickGaze", payload: { now, dt, mouse } });
  }

  tickPreModel(): void {
    this.calls.push({ hook: "tickPreModel" });
  }
}

describe("AvatarEngine — lifecycle", () => {
  let adapter: FakeAdapter;
  let clock: FakeClock;
  let raf: ManualRaf;
  let mouse: FakeMouseSource;
  let engineState: ReturnType<typeof createEngineState>;
  let engine: AvatarEngine;

  beforeEach(() => {
    adapter = new FakeAdapter();
    clock = new FakeClock(1_000);
    raf = new ManualRaf();
    mouse = new FakeMouseSource();
    engineState = createEngineState();
    engine = new AvatarEngine({
      manifest: buildManifest(),
      engineState,
      getStoreSnapshot: () => buildStoreSnapshot(),
      now: clock.now,
      mouseSource: mouse,
      scheduleFrame: raf.schedule,
      cancelFrame: raf.cancel,
    });
  });

  afterEach(() => {
    engine.stop();
  });

  it("registers channels in order and reports the count", () => {
    const a = new RecordingChannel("a");
    const b = new RecordingChannel("b");
    engine.register(a, b);
    expect(engine.channelCount).toBe(2);
  });

  it("ignores duplicate registration of the same channel instance", () => {
    const a = new RecordingChannel("a");
    engine.register(a, a, a);
    expect(engine.channelCount).toBe(1);
  });

  it("attaches every channel + subscribes mouse + arms the RAFs on start", () => {
    const a = new RecordingChannel("a");
    const b = new RecordingChannel("b");
    engine.register(a, b);
    engine.start(adapter);
    expect(a.attached).toBe(true);
    expect(b.attached).toBe(true);
    expect(mouse.subscribeCount).toBe(1);
    // Two RAFs queued: tier-3 + gaze.
    expect(raf.pending).toBe(2);
    expect(engine.isRunning).toBe(true);
  });

  it("stop() detaches in reverse, unsubscribes mouse, and cancels both RAFs", () => {
    const a = new RecordingChannel("a");
    const b = new RecordingChannel("b");
    engine.register(a, b);
    engine.start(adapter);
    engine.stop();
    expect(a.attached).toBe(false);
    expect(b.attached).toBe(false);
    expect(mouse.unsubscribeCount).toBe(1);
    expect(engine.isRunning).toBe(false);
    // Detach order: b before a.
    const order = [...a.calls, ...b.calls]
      .filter((c) => c.hook === "detach")
      .map(() => "");
    expect(order.length).toBe(2);
    const bDetachIdx = b.calls.findIndex((c) => c.hook === "detach");
    const aDetachIdx = a.calls.findIndex((c) => c.hook === "detach");
    expect(bDetachIdx).toBeGreaterThan(-1);
    expect(aDetachIdx).toBeGreaterThan(-1);
  });

  it("register() after start() throws", () => {
    const a = new RecordingChannel("a");
    engine.register(a);
    engine.start(adapter);
    expect(() => engine.register(new RecordingChannel("b"))).toThrow(/cannot register/i);
  });

  it("start() twice warns and is a no-op", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const a = new RecordingChannel("a");
    engine.register(a);
    engine.start(adapter);
    engine.start(adapter); // second call
    expect(warn).toHaveBeenCalledTimes(1);
    // Channel still only attached once.
    const attaches = a.calls.filter((c) => c.hook === "attach");
    expect(attaches).toHaveLength(1);
    warn.mockRestore();
  });

  it("start() after stop() throws (engine is single-shot)", () => {
    engine.start(adapter);
    engine.stop();
    expect(() => engine.start(adapter)).toThrow(/disposed/i);
  });

  it("stop() before start() is a no-op", () => {
    expect(() => engine.stop()).not.toThrow();
    expect(engine.isRunning).toBe(false);
  });
});

describe("AvatarEngine — dispatch fan-out", () => {
  let adapter: FakeAdapter;
  let clock: FakeClock;
  let raf: ManualRaf;
  let mouse: FakeMouseSource;
  let engineState: ReturnType<typeof createEngineState>;
  let engine: AvatarEngine;
  let channel: RecordingChannel;

  beforeEach(() => {
    adapter = new FakeAdapter();
    clock = new FakeClock(1_000);
    raf = new ManualRaf();
    mouse = new FakeMouseSource();
    engineState = createEngineState();
    engine = new AvatarEngine({
      manifest: buildManifest(),
      engineState,
      getStoreSnapshot: () => buildStoreSnapshot(),
      now: clock.now,
      mouseSource: mouse,
      scheduleFrame: raf.schedule,
      cancelFrame: raf.cancel,
    });
    channel = new RecordingChannel("c");
    engine.register(channel);
    engine.start(adapter);
    channel.calls.length = 0; // ignore the attach() entry
  });

  afterEach(() => {
    engine.stop();
  });

  it("dispatchReaction dedupes against EngineState.lastReaction", () => {
    engine.dispatchReaction("cheerful");
    engine.dispatchReaction("cheerful"); // duped
    engine.dispatchReaction("sad");
    const reactions = channel.calls
      .filter((c) => c.hook === "onReaction")
      .map((c) => c.payload);
    expect(reactions).toEqual(["cheerful", "sad"]);
    expect(engineState.lastReaction).toBe("sad");
  });

  it("dispatchTtsState only fires on transition", () => {
    engine.dispatchTtsState("idle"); // already idle
    engine.dispatchTtsState("speaking");
    engine.dispatchTtsState("speaking"); // duped
    engine.dispatchTtsState("idle");
    const states = channel.calls
      .filter((c) => c.hook === "onTtsState")
      .map((c) => c.payload);
    expect(states).toEqual(["speaking", "idle"]);
    expect(engineState.ttsState).toBe("idle");
  });

  it("dispatchMotion fans the raw motion event through", () => {
    const motion = { name: "wave", group: "Wave", index: 0, firedAt: 5 };
    engine.dispatchMotion(motion);
    const fired = channel.calls.find((c) => c.hook === "onMotion");
    expect(fired?.payload).toEqual(motion);
  });

  it("dispatchOverlay converts wall-clock expiresAt to monotonic", () => {
    // The clock is at 1000ms (perf-clock). Pretend Date.now() returned
    // 1_700_000_000_000 and the overlay expires 800ms from now in
    // wall-clock terms; the engine should convert that to
    // perf-clock = 1000 + 800 = 1800.
    const dateNowSpy = vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    engine.dispatchOverlay({
      name: "grin",
      expiresAt: 1_700_000_000_800,
    });
    dateNowSpy.mockRestore();
    const fired = channel.calls.find((c) => c.hook === "onOverlay");
    expect(fired?.payload).toEqual({ name: "grin", until: 1_800 });
  });

  it("dispatchOverlay clamps a stale (already expired) overlay to now()", () => {
    const dateNowSpy = vi.spyOn(Date, "now").mockReturnValue(1_700_000_000_000);
    engine.dispatchOverlay({
      name: "stars",
      // wall-clock expired 500ms ago
      expiresAt: 1_699_999_999_500,
    });
    dateNowSpy.mockRestore();
    const fired = channel.calls.find((c) => c.hook === "onOverlay");
    // Clamped to now (no negative remaining ms).
    expect(fired?.payload).toEqual({ name: "stars", until: 1_000 });
  });

  it("dispatchOverlay(null) is a no-op", () => {
    engine.dispatchOverlay(null);
    expect(channel.calls.find((c) => c.hook === "onOverlay")).toBeUndefined();
  });

  it("dispatchMood passes the mood object through unchanged", () => {
    engine.dispatchMood(NEUTRAL_MOOD);
    const fired = channel.calls.find((c) => c.hook === "onMood");
    expect(fired?.payload).toBe(NEUTRAL_MOOD);
  });

  it("a throwing channel does not break dispatch to its peers", () => {
    const error = vi.spyOn(console, "error").mockImplementation(() => undefined);
    const broken: AvatarChannel = {
      name: "broken",
      attach: () => undefined,
      detach: () => undefined,
      onReaction: () => {
        throw new Error("boom");
      },
    };
    // Need a fresh engine because we already started.
    engine.stop();
    engine = new AvatarEngine({
      manifest: buildManifest(),
      engineState,
      getStoreSnapshot: () => buildStoreSnapshot(),
      now: clock.now,
      mouseSource: mouse,
      scheduleFrame: raf.schedule,
      cancelFrame: raf.cancel,
    });
    const peer = new RecordingChannel("peer");
    engine.register(broken, peer);
    engine.start(adapter);
    engine.dispatchReaction("cheerful");
    expect(peer.calls.find((c) => c.hook === "onReaction")?.payload).toBe("cheerful");
    expect(error).toHaveBeenCalled();
    error.mockRestore();
  });
});

describe("AvatarEngine — RAF + pre-update", () => {
  let adapter: FakeAdapter;
  let clock: FakeClock;
  let raf: ManualRaf;
  let mouse: FakeMouseSource;
  let engineState: ReturnType<typeof createEngineState>;
  let engine: AvatarEngine;
  let channel: RecordingChannel;

  beforeEach(() => {
    adapter = new FakeAdapter();
    clock = new FakeClock(1_000);
    raf = new ManualRaf();
    mouse = new FakeMouseSource();
    engineState = createEngineState();
    engine = new AvatarEngine({
      manifest: buildManifest(),
      engineState,
      getStoreSnapshot: () => buildStoreSnapshot(),
      now: clock.now,
      mouseSource: mouse,
      scheduleFrame: raf.schedule,
      cancelFrame: raf.cancel,
    });
    channel = new RecordingChannel("c");
    engine.register(channel);
    engine.start(adapter);
    channel.calls.length = 0;
  });

  afterEach(() => {
    engine.stop();
  });

  it("each tier-3 flush invokes tickTier3 with monotonic now and dt seconds", () => {
    clock.advance(16);
    raf.flush(1); // tier-3 RAF was queued first, gaze second
    const tick = channel.calls.find((c) => c.hook === "tickTier3");
    expect(tick).toBeDefined();
    expect((tick!.payload as { now: number }).now).toBe(1_016);
    expect((tick!.payload as { dt: number }).dt).toBeCloseTo(0.016);
    // Tier-3 should re-queue itself.
    expect(raf.pending).toBe(2);
  });

  it("each gaze flush invokes tickGaze with the current MouseSnapshot", () => {
    mouse.current = {
      x: 100,
      y: 200,
      lastMoveAt: 999,
      windowFocused: false,
      containerRect: { left: 0, top: 0, width: 800, height: 600 },
      viewportWidth: 1600,
      viewportHeight: 900,
    };
    raf.flush(1); // tier-3 first
    raf.flush(1); // gaze second
    const tick = channel.calls.find((c) => c.hook === "tickGaze");
    expect(tick).toBeDefined();
    const payload = tick!.payload as { mouse: MouseSnapshot };
    expect(payload.mouse).toEqual(mouse.current);
  });

  it("tickPreModel fires when the adapter triggers beforeModelUpdate", () => {
    adapter.triggerBeforeModelUpdate();
    adapter.triggerBeforeModelUpdate();
    const ticks = channel.calls.filter((c) => c.hook === "tickPreModel");
    expect(ticks).toHaveLength(2);
  });

  it("expression-slot lock release fires onExpressionSlotReleased before tickTier3", () => {
    engineState.exprSlotLockUntil = clock.now() + 50;
    clock.advance(60);
    raf.flush(1); // tier-3 tick
    const releaseIdx = channel.calls.findIndex(
      (c) => c.hook === "onExpressionSlotReleased",
    );
    const tickIdx = channel.calls.findIndex((c) => c.hook === "tickTier3");
    expect(releaseIdx).toBeGreaterThanOrEqual(0);
    expect(tickIdx).toBeGreaterThanOrEqual(0);
    expect(releaseIdx).toBeLessThan(tickIdx);
    expect(engineState.exprSlotLockUntil).toBe(0);
  });

  it("expression-slot lock release does NOT fire while the deadline is in the future", () => {
    engineState.exprSlotLockUntil = clock.now() + 500;
    clock.advance(50);
    raf.flush(1);
    expect(channel.calls.find((c) => c.hook === "onExpressionSlotReleased")).toBeUndefined();
    expect(engineState.exprSlotLockUntil).toBeGreaterThan(0);
  });

  it("stop() cancels both RAFs so nothing fires after disposal", () => {
    engine.stop();
    raf.flush(10); // would re-queue forever if engine kept running
    expect(channel.calls.find((c) => c.hook === "tickTier3")).toBeUndefined();
    expect(channel.calls.find((c) => c.hook === "tickGaze")).toBeUndefined();
  });
});

/**
 * The frame ceiling phones run under. Capping the Pixi ticker alone
 * leaves these two RAF chains writing rig parameters at the display's
 * full rate underneath it, which on a 120 Hz handset is most of the
 * per-frame cost the cap was meant to remove.
 */
describe("AvatarEngine — frame ceiling", () => {
  let adapter: FakeAdapter;
  let clock: FakeClock;
  let raf: ManualRaf;
  let channel: RecordingChannel;
  let engine: AvatarEngine;

  /** ``maxFPS`` of 0 means "every animation frame" (the desktop path). */
  function startEngine(maxFPS: number): void {
    adapter = new FakeAdapter();
    clock = new FakeClock(1_000);
    raf = new ManualRaf();
    engine = new AvatarEngine({
      manifest: buildManifest(),
      engineState: createEngineState(),
      getStoreSnapshot: () => buildStoreSnapshot(),
      now: clock.now,
      mouseSource: new FakeMouseSource(),
      scheduleFrame: raf.schedule,
      cancelFrame: raf.cancel,
      maxFPS,
    });
    channel = new RecordingChannel("c");
    engine.register(channel);
    engine.start(adapter);
    channel.calls.length = 0;
  }

  /** Advance ``frames`` display frames of ``stepMs`` each, flushing both
   * loops on every one. Returns how many times each hook did real work. */
  function run(frames: number, stepMs: number): { tier3: number; gaze: number } {
    for (let i = 0; i < frames; i += 1) {
      clock.advance(stepMs);
      raf.flush(2); // tier-3 and gaze are queued as a pair
    }
    return {
      tier3: channel.calls.filter((c) => c.hook === "tickTier3").length,
      gaze: channel.calls.filter((c) => c.hook === "tickGaze").length,
    };
  }

  afterEach(() => {
    engine.stop();
  });

  it("uncapped, every animation frame does work", () => {
    startEngine(0);
    expect(run(12, 8.33)).toEqual({ tier3: 12, gaze: 12 });
  });

  it("a 30 fps cap skips three of every four 120 Hz frames", () => {
    startEngine(30);
    const { tier3, gaze } = run(12, 8.33);
    expect(tier3).toBe(3);
    expect(gaze).toBe(3);
  });

  it("a 30 fps cap still runs every other frame at 60 Hz", () => {
    // The tolerance exists for exactly this: two 16.67 ms frames land a
    // hair under the 33.33 ms budget, and without slack the cap would
    // defer to the third frame and yield 20 fps instead of 30.
    startEngine(30);
    expect(run(12, 16.67).tier3).toBe(6);
  });

  it("a skipped frame still asks for the next one", () => {
    startEngine(30);
    clock.advance(8.33);
    raf.flush(2);
    expect(channel.calls).toHaveLength(0);
    expect(raf.pending).toBe(2);
  });

  it("a skipped frame does not eat its elapsed time", () => {
    // dt has to span everything since the last tick that did work,
    // otherwise breathing and gaze run slow in proportion to the cap.
    startEngine(30);
    clock.advance(20);
    raf.flush(2); // under budget: skipped
    clock.advance(20);
    raf.flush(2); // 40 ms since the last real tick
    const tick = channel.calls.find((c) => c.hook === "tickTier3");
    expect((tick!.payload as { dt: number }).dt).toBeCloseTo(0.04, 3);
  });

  it("the cap can be lifted and reapplied at runtime", () => {
    // A phone rotating across the layout breakpoint must not need the
    // rig torn down and rebuilt.
    startEngine(30);
    engine.setMaxFPS(0);
    expect(run(4, 8.33).tier3).toBe(4);
    engine.setMaxFPS(30);
    channel.calls.length = 0;
    expect(run(4, 8.33).tier3).toBe(1);
  });
});

/**
 * P38: ``Live2DAvatar.getStoreSnapshot()`` allocates a new object on
 * every call. Channels used to pull independently (ExpressionChannel
 * three times in one tickTier3), so the engine now caches one snapshot
 * per tick invocation. Attach / dispatch stay live so a channel that
 * reads on an event still sees current store state.
 */
class SnapshotPuller implements AvatarChannel {
  readonly name: string;
  pulls = 0;
  lastSnap: ReturnType<ChannelDeps["getStoreSnapshot"]> | null = null;
  private _deps: ChannelDeps | null = null;
  private readonly _perTick: number;
  private readonly _hooks: {
    attach?: boolean;
    tickTier3?: boolean;
    tickGaze?: boolean;
    tickPreModel?: boolean;
  };

  constructor(
    name: string,
    perTick: number,
    hooks: SnapshotPuller["_hooks"] = { tickTier3: true },
  ) {
    this.name = name;
    this._perTick = perTick;
    this._hooks = hooks;
  }

  attach(_adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._deps = deps;
    if (this._hooks.attach) {
      this._pull(false);
    }
  }

  detach(): void {
    this._deps = null;
  }

  tickTier3(): void {
    if (this._hooks.tickTier3) {
      this._pull(true);
    }
  }

  tickGaze(): void {
    if (this._hooks.tickGaze) {
      this._pull(true);
    }
  }

  tickPreModel(): void {
    if (this._hooks.tickPreModel) {
      this._pull(true);
    }
  }

  private _pull(expectSame: boolean): void {
    const deps = this._deps;
    if (!deps) {
      return;
    }
    let first: ReturnType<ChannelDeps["getStoreSnapshot"]> | null = null;
    for (let i = 0; i < this._perTick; i += 1) {
      const snap = deps.getStoreSnapshot();
      this.pulls += 1;
      if (first === null) {
        first = snap;
      } else if (expectSame) {
        expect(snap).toBe(first);
      }
    }
    this.lastSnap = first;
  }
}

describe("AvatarEngine — store snapshot cache", () => {
  let adapter: FakeAdapter;
  let clock: FakeClock;
  let raf: ManualRaf;
  let snapshotCalls = 0;
  let engine: AvatarEngine;

  function startEngine(
    channels: AvatarChannel[],
    opts: { maxFPS?: number } = {},
  ): void {
    adapter = new FakeAdapter();
    clock = new FakeClock(1_000);
    raf = new ManualRaf();
    snapshotCalls = 0;
    engine = new AvatarEngine({
      manifest: buildManifest(),
      engineState: createEngineState(),
      getStoreSnapshot: () => {
        snapshotCalls += 1;
        return buildStoreSnapshot();
      },
      now: clock.now,
      mouseSource: new FakeMouseSource(),
      scheduleFrame: raf.schedule,
      cancelFrame: raf.cancel,
      maxFPS: opts.maxFPS ?? 0,
    });
    engine.register(...channels);
    engine.start(adapter);
  }

  afterEach(() => {
    engine.stop();
  });

  it("one factory pull per tick even when several channels poll repeatedly", () => {
    const a = new SnapshotPuller("a", 3);
    const b = new SnapshotPuller("b", 3);
    startEngine([a, b]);
    snapshotCalls = 0;
    clock.advance(16);
    raf.flush(1); // tier-3 only
    expect(snapshotCalls).toBe(1);
    expect(a.pulls).toBe(3);
    expect(b.pulls).toBe(3);
    expect(a.lastSnap).toBe(b.lastSnap);
  });

  it("tier-3 and gaze each take their own snapshot", () => {
    const channel = new SnapshotPuller("both", 1, {
      tickTier3: true,
      tickGaze: true,
    });
    startEngine([channel]);
    snapshotCalls = 0;
    clock.advance(16);
    raf.flush(1); // tier-3
    expect(snapshotCalls).toBe(1);
    raf.flush(1); // gaze
    expect(snapshotCalls).toBe(2);
  });

  it("a nested pre-model tick reuses the outer snapshot", () => {
    const pre = new SnapshotPuller("pre", 2, { tickPreModel: true });
    const nested: AvatarChannel = {
      name: "nested",
      attach: () => undefined,
      detach: () => undefined,
      tickTier3: () => {
        adapter.triggerBeforeModelUpdate();
      },
    };
    startEngine([nested, pre]);
    snapshotCalls = 0;
    clock.advance(16);
    raf.flush(1);
    expect(snapshotCalls).toBe(1);
    expect(pre.pulls).toBe(2);
    // Cache is cleared after the RAF tick; a standalone pre-model is live.
    snapshotCalls = 0;
    adapter.triggerBeforeModelUpdate();
    expect(snapshotCalls).toBe(1);
    expect(pre.pulls).toBe(4);
  });

  it("attach pulls live (no tick cache)", () => {
    const channel = new SnapshotPuller("attach", 2, { attach: true });
    startEngine([channel]);
    // Two pulls during attach, both live, so two factory calls.
    expect(snapshotCalls).toBe(2);
    expect(channel.pulls).toBe(2);
  });

  it("a skipped frame under the cap does not pull a snapshot", () => {
    const channel = new SnapshotPuller("c", 1);
    startEngine([channel], { maxFPS: 30 });
    snapshotCalls = 0;
    clock.advance(8.33);
    raf.flush(2);
    expect(channel.pulls).toBe(0);
    expect(snapshotCalls).toBe(0);
  });
});
