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

/** Build a manifest with a ``cheerful`` reaction mapped to the
 * ``lzx`` expression and a single ``Param54: 30`` binding — the
 * smallest shape that exercises the arousal-scale write path. */
function makeAmplitudeManifestDeps(
  initialSnap: Partial<ChannelStoreSnapshot> = {},
) {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  let snapshot: ChannelStoreSnapshot = {
    reaction: "cheerful",
    ttsState: "idle",
    voiceMode: "off",
    turnInProgress: false,
    audioAmplitude: 0,
    avatarOverlay: null,
    avatarMotion: null,
    mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 },
    resolvedOutfit: "",
    backchannelHint: "",
    expressiveness: 1,
    ...initialSnap,
  };
  const manifest = buildManifest({
    reaction_mapping: { cheerful: "lzx", neutral: "n" },
    expressions: [
      { name: "lzx", file: "lzx.exp3.json" },
      { name: "n", file: "n.exp3.json" },
    ],
    expression_params: {
      lzx: [{ param_id: "Param54", on_value: 30 }],
    },
  });
  return {
    clock,
    engineState,
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    },
    setSnapshot: (next: Partial<ChannelStoreSnapshot>) => {
      snapshot = { ...snapshot, ...next };
    },
  };
}

/** Run ``tickPreModel`` ``frames`` times with ``dt`` seconds between
 * ticks. Returns the final ``Param54`` value once the smoothing has
 * had time to converge. */
function runPreModel(
  channel: ExpressionChannel,
  adapter: FakeAdapter,
  clock: FakeClock,
  frames: number,
  dt: number,
): number {
  for (let i = 0; i < frames; i += 1) {
    clock.advance(dt * 1000);
    channel.tickPreModel!();
  }
  return adapter.params.get("Param54") ?? 0;
}

describe("ExpressionChannel — tickPreModel arousal scaling", () => {
  it("low arousal writes a quieter param value", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.1 },
    });
    channel.attach(adapter, deps);
    const value = runPreModel(channel, adapter, clock, 240, 1 / 60);
    // arousal=0.1 -> scale ~0.46; expressiveness=1 -> ~30 * 0.46 = 13.8.
    expect(value).toBeGreaterThan(11);
    expect(value).toBeLessThan(17);
  });

  it("high arousal writes close to the rig's authored on_value", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    const value = runPreModel(channel, adapter, clock, 240, 1 / 60);
    // arousal=0.9 -> scale ~0.94; expressiveness=1 -> ~28.2.
    expect(value).toBeGreaterThan(25);
    expect(value).toBeLessThanOrEqual(30);
  });

  it("expressiveness 0 mutes the write to zero", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      expressiveness: 0,
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    const value = runPreModel(channel, adapter, clock, 240, 1 / 60);
    expect(Math.abs(value)).toBeLessThan(0.01);
  });

  it("expressiveness 1.5 amplifies the write but caps at the on_value", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      expressiveness: 1.5,
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    const value = runPreModel(channel, adapter, clock, 240, 1 / 60);
    // arousal scale capped at 1.0; expressiveness 1.5 -> 30 * 1 * 1.5 = 45.
    // We document this as "capped by the rig's natural on_value" but
    // the actual write doesn't auto-clamp — pixi-live2d-display will
    // clamp at the param's authored max. We still want the test to
    // assert proportional scaling above the default-1.0 case.
    const adapterDefault = new FakeAdapter();
    const channelDefault = new ExpressionChannel();
    const def = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channelDefault.attach(adapterDefault, def.deps);
    const defaultValue = runPreModel(
      channelDefault,
      adapterDefault,
      def.clock,
      240,
      1 / 60,
    );
    expect(value).toBeGreaterThan(defaultValue * 1.4);
  });

  it("does not write while exprSlotLockUntil is in the future", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    // Reset our smoothed amplitude back to zero by holding the slot
    // lock, then ticking. The tickPreModel must skip the write so
    // Param54 stays at whatever the FakeAdapter has (untouched).
    engineState.exprSlotLockUntil = clock.now() + 5_000;
    for (let i = 0; i < 120; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(adapter.params.has("Param54")).toBe(false);
    expect(channel.amplitudeScale).toBe(0);
  });

  it("resumes writing after the slot lock expires", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    engineState.exprSlotLockUntil = clock.now() + 1_000;
    for (let i = 0; i < 30; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(adapter.params.has("Param54")).toBe(false);

    // Drop the lock; tickPreModel must resume writing.
    engineState.exprSlotLockUntil = 0;
    for (let i = 0; i < 240; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    const value = adapter.params.get("Param54") ?? 0;
    expect(value).toBeGreaterThan(20);
  });

  it("does nothing when the active expression has no expression_params binding", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    // Manifest with no expression_params for the active expression —
    // legacy / minimal rig path.
    const clock = new FakeClock(1_000);
    const engineState = createEngineState();
    const snapshot: ChannelStoreSnapshot = {
      reaction: "cheerful",
      ttsState: "idle",
      voiceMode: "off",
      turnInProgress: false,
      audioAmplitude: 0,
      avatarOverlay: null,
      avatarMotion: null,
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
      resolvedOutfit: "",
      backchannelHint: "",
      expressiveness: 1,
    };
    const manifest = buildManifest({
      reaction_mapping: { cheerful: "lzx" },
      expressions: [{ name: "lzx", file: "lzx.exp3.json" }],
      // No expression_params at all.
    });
    const deps: ChannelDeps = {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    };
    channel.attach(adapter, deps);
    for (let i = 0; i < 60; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(adapter.params.has("Param54")).toBe(false);
  });

  it("smooths amplitude across an arousal flip instead of snapping", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock, setSnapshot } = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const settled = adapter.params.get("Param54") ?? 0;

    setSnapshot({
      mood: { label: "calm", intensity: 0.4, valence: 0, arousal: 0.05 },
    });
    // A single tick after a flip cannot snap the amplitude all the
    // way down to the new target. The smoothing must keep us above
    // the eventual converged low-arousal value for at least one
    // frame.
    clock.advance(1000 / 60);
    channel.tickPreModel!();
    const oneTickAfter = adapter.params.get("Param54") ?? 0;
    expect(oneTickAfter).toBeLessThan(settled);
    expect(oneTickAfter).toBeGreaterThan(11);
  });
});

/** Variant of ``makeAmplitudeManifestDeps`` that registers
 * ``Param54`` as a mouth-overlay so the lip-sync suppression branch
 * activates. Mirrors the Alexia rig where ``lzx`` paints a static
 * toothy grin via ``Param54`` independent of the lip-synced jaw. */
function makeMouthOverlayDeps(initialSnap: Partial<ChannelStoreSnapshot> = {}) {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  let snapshot: ChannelStoreSnapshot = {
    reaction: "cheerful",
    ttsState: "idle",
    voiceMode: "off",
    turnInProgress: false,
    audioAmplitude: 0,
    avatarOverlay: null,
    avatarMotion: null,
    mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    resolvedOutfit: "",
    backchannelHint: "",
    expressiveness: 1,
    ...initialSnap,
  };
  const manifest = buildManifest({
    reaction_mapping: { cheerful: "lzx", neutral: "n" },
    expressions: [
      { name: "lzx", file: "lzx.exp3.json" },
      { name: "n", file: "n.exp3.json" },
    ],
    expression_params: {
      // Realistic lzx shape: the grin overlay on Param54, plus a
      // companion non-mouth param (eye squint, hypothetical Param80)
      // so we can prove the suppression is targeted — non-mouth
      // bindings on the SAME expression must keep their amplitude.
      lzx: [
        { param_id: "Param54", on_value: 30 },
        { param_id: "Param80", on_value: 30 },
      ],
    },
    mouth_overlay_param_ids: ["Param54"],
  });
  return {
    clock,
    engineState,
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    },
    setSnapshot: (next: Partial<ChannelStoreSnapshot>) => {
      snapshot = { ...snapshot, ...next };
    },
  };
}

describe("ExpressionChannel — mouth-overlay lip-sync suppression", () => {
  it("silent audio writes the grin param at the full arousal-scaled value", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeMouthOverlayDeps({ audioAmplitude: 0 });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const grin = adapter.params.get("Param54") ?? 0;
    // High arousal -> ~28; we should see basically the full value
    // because suppression is zero.
    expect(grin).toBeGreaterThan(25);
    expect(grin).toBeLessThanOrEqual(30);
  });

  it("active lip-sync amplitude tapers the grin overlay toward zero", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeMouthOverlayDeps({
      // Mid-amplitude TTS chunk; with gain=6 this fully saturates
      // the suppression factor.
      audioAmplitude: 0.3,
    });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const grin = adapter.params.get("Param54") ?? 0;
    // Suppressed grin should be effectively zero — well below any
    // visible threshold on the rig.
    expect(grin).toBeLessThan(1);
  });

  it("non-mouth bindings on the same expression are unaffected by lipsync", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeMouthOverlayDeps({ audioAmplitude: 0.3 });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    // Param80 is on the same lzx expression but NOT in
    // mouth_overlay_param_ids — its arousal-scaled write must
    // survive the suppression intact.
    const param80 = adapter.params.get("Param80") ?? 0;
    expect(param80).toBeGreaterThan(25);
    expect(param80).toBeLessThanOrEqual(30);
  });

  it("grin recovers smoothly when audio falls back to silence", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock, setSnapshot } = makeMouthOverlayDeps({
      audioAmplitude: 0.3,
    });
    channel.attach(adapter, deps);
    // Settle in fully suppressed state.
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const suppressed = adapter.params.get("Param54") ?? 0;
    expect(suppressed).toBeLessThan(1);

    // Speech ends — audio drops back to zero. The suppression
    // factor must decay so the grin re-emerges.
    setSnapshot({ audioAmplitude: 0 });
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const recovered = adapter.params.get("Param54") ?? 0;
    expect(recovered).toBeGreaterThan(20);
  });

  it("does not allocate or fight when no mouth overlay ids are declared", () => {
    // Sanity: the original path (lzx with NO mouth_overlay_param_ids)
    // must keep behaving exactly like the pre-fix arousal-scaled
    // write. Asserts we didn't regress non-Alexia rigs.
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      audioAmplitude: 0.3,
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const grin = adapter.params.get("Param54") ?? 0;
    // No mouth_overlay_param_ids => mouthScale stays at 1, so the
    // value is the high-arousal full-amplitude write.
    expect(grin).toBeGreaterThan(25);
    expect(grin).toBeLessThanOrEqual(30);
  });
});

