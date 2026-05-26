/**
 * ExpressionChannel tests.
 *
 * Behaviour matrix:
 *
 *   - Reaction priority: ``onReaction`` writes ``adapter.expression(name)``
 *     when the slot is free.
 *   - Slot lock: ``engineState.exprSlotLockUntil > now`` defers writes;
 *     ``onExpressionSlotReleased`` flushes them.
 *   - Voice mode: listening/thinking override the persistent reaction
 *     (highest priority while a real expression slot lock isn't active).
 *   - Backchannel: applies a transient expression and restores after
 *     the 1.8s window. Newer hints cancel the previous restore.
 *   - Empty reaction: ``adapter.resetExpression`` is called instead of
 *     leaving the previous expression on the rig (the regression-fix).
 */
import { describe, expect, it } from "vitest";

import { ExpressionChannel } from "./ExpressionChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState, type EngineState } from "../state";
import type { ChannelDeps, ChannelStoreSnapshot } from "../types";

interface ManualTimer {
  schedule: (cb: () => void, ms: number) => unknown;
  cancel: (handle: unknown) => void;
  /** Run the scheduled callback if one is pending. */
  flush(): void;
  pending(): boolean;
}

function makeManualTimer(): ManualTimer {
  let entry: { id: number; cb: () => void } | null = null;
  let nextId = 1;
  return {
    schedule: (cb) => {
      const id = nextId++;
      entry = { id, cb };
      return id;
    },
    cancel: (handle) => {
      if (entry && entry.id === handle) {
        entry = null;
      }
    },
    flush: () => {
      if (!entry) {
        return;
      }
      const cb = entry.cb;
      entry = null;
      cb();
    },
    pending: () => entry !== null,
  };
}

interface DepsBundle {
  deps: ChannelDeps;
  clock: FakeClock;
  engineState: EngineState;
  setSnapshot: (next: Partial<ChannelStoreSnapshot>) => void;
}

function makeDeps(initial: Partial<ChannelStoreSnapshot> = {}): DepsBundle {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  let snapshot: ChannelStoreSnapshot = {
    reaction: "neutral",
    ttsState: "idle",
    voiceMode: "off",
    turnInProgress: false,
    audioAmplitude: 0,
    avatarOverlay: null,
    avatarMotion: null,
    mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
    resolvedOutfit: "",
    backchannelHint: "",
    ...initial,
  };
  const manifest = buildManifest({
    reaction_mapping: {
      neutral: "ExprNeutral",
      cheerful: "ExprCheerful",
      sad: "ExprSad",
      thoughtful: "ExprThoughtful",
      surprised: "ExprSurprised",
    },
    expressions: [
      { name: "ExprNeutral", file: "n.exp3.json" },
      { name: "ExprCheerful", file: "c.exp3.json" },
      { name: "ExprSad", file: "s.exp3.json" },
      { name: "ExprThoughtful", file: "t.exp3.json" },
      { name: "ExprSurprised", file: "su.exp3.json" },
    ],
  });
  return {
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    },
    clock,
    engineState,
    setSnapshot: (next) => {
      snapshot = { ...snapshot, ...next };
    },
  };
}

describe("ExpressionChannel — attach + initial reaction", () => {
  it("applies the initial reaction expression on attach", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "cheerful" });
    channel.attach(adapter, deps);
    expect(adapter.expressionCalls).toEqual(["ExprCheerful"]);
  });

  it("calls adapter.resetExpression when the initial reaction is empty", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "" });
    channel.attach(adapter, deps);
    expect(adapter.expressionCalls).toEqual([]);
    expect(adapter.resetExpressionCount).toBe(1);
  });

  it("calls adapter.resetExpression when the reaction has no mapping at all", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "xyz_unmapped" });
    channel.attach(adapter, deps);
    expect(adapter.resetExpressionCount).toBe(1);
  });
});

describe("ExpressionChannel — onReaction", () => {
  it("writes adapter.expression for the new reaction when slot is free", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onReaction!("cheerful");
    expect(adapter.expressionCalls).toEqual(["ExprCheerful"]);
  });

  it("defers writes while exprSlotLockUntil > now (overlay owns the slot)", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    engineState.exprSlotLockUntil = clock.now() + 500;
    channel.onReaction!("sad");
    expect(adapter.expressionCalls).toEqual([]);

    // Engine notifies us when the deadline passes.
    channel.onExpressionSlotReleased!();
    expect(adapter.expressionCalls).toEqual(["ExprSad"]);
  });

  it("re-applies via reset when the deferred reaction has empty mapping", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;
    adapter.resetExpressionCount = 0;

    engineState.exprSlotLockUntil = clock.now() + 500;
    channel.onReaction!("");
    expect(adapter.resetExpressionCount).toBe(0);
    channel.onExpressionSlotReleased!();
    expect(adapter.resetExpressionCount).toBe(1);
  });
});

describe("ExpressionChannel — voice mode override", () => {
  it("applies the listening expression on voiceMode listening", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onVoiceMode!("listening");
    expect(adapter.expressionCalls).toEqual(["ExprThoughtful"]);
  });

  it("applies the thinking expression on voiceMode thinking", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onVoiceMode!("thinking");
    expect(adapter.expressionCalls).toEqual(["ExprThoughtful"]);
  });

  it("restores the persistent reaction when leaving listening/thinking", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "cheerful" });
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onVoiceMode!("listening");
    channel.onVoiceMode!("off");
    expect(adapter.expressionCalls).toEqual(["ExprThoughtful", "ExprCheerful"]);
  });

  it("ignores reaction changes while voiceMode is listening (mode wins)", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    channel.onVoiceMode!("listening");
    adapter.expressionCalls.length = 0;

    channel.onReaction!("cheerful");
    // No new expression call — voice mode owns the slot.
    expect(adapter.expressionCalls).toEqual([]);

    // ...but when the mode leaves, the *latest* reaction is applied.
    channel.onVoiceMode!("off");
    expect(adapter.expressionCalls).toEqual(["ExprCheerful"]);
  });
});

describe("ExpressionChannel — backchannel hints", () => {
  it("applies the backchannel expression and arms a restore timer", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps } = makeDeps({ reaction: "neutral" });
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onBackchannel!("agreement");
    expect(adapter.expressionCalls).toEqual(["ExprCheerful"]);
    expect(channel.backchannelRestoreArmed).toBe(true);
    expect(timer.pending()).toBe(true);

    // Restore fires.
    timer.flush();
    expect(adapter.expressionCalls).toEqual(["ExprCheerful", "ExprNeutral"]);
  });

  it("a fresh backchannel cancels the prior restore timer", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps } = makeDeps({ reaction: "neutral" });
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onBackchannel!("agreement");
    expect(timer.pending()).toBe(true);
    channel.onBackchannel!("surprise");
    // Prior timer was cancelled; new one armed.
    expect(timer.pending()).toBe(true);
    expect(adapter.expressionCalls).toEqual(["ExprCheerful", "ExprSurprised"]);
  });

  it("ignores backchannel hints while exprSlotLockUntil > now", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps, engineState, clock } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    engineState.exprSlotLockUntil = clock.now() + 1_000;
    channel.onBackchannel!("agreement");
    expect(adapter.expressionCalls).toEqual([]);
    expect(timer.pending()).toBe(false);
  });
});

describe("ExpressionChannel — onExpressionSlotReleased", () => {
  it("re-applies whatever the current target is (regression: stuck grin)", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeDeps({ reaction: "neutral" });
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;
    adapter.resetExpressionCount = 0;

    // Overlay grabbed the slot mid-turn (e.g. ``[[overlay:grin]]``).
    engineState.exprSlotLockUntil = clock.now() + 500;
    // Reaction stayed neutral throughout the overlay; engine fans
    // ``onExpressionSlotReleased`` when the deadline passes. We
    // should re-apply the persistent reaction (or reset, in the
    // empty-mapping case).
    channel.onExpressionSlotReleased!();
    expect(adapter.resetExpressionCount + adapter.expressionCalls.length).toBe(1);
  });
});

describe("ExpressionChannel — lifecycle", () => {
  it("detach() cancels any pending restore timer", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    channel.onBackchannel!("agreement");
    expect(timer.pending()).toBe(true);
    channel.detach();
    expect(timer.pending()).toBe(false);
    expect(channel.backchannelRestoreArmed).toBe(false);
  });
});
