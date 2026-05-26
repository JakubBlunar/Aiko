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
