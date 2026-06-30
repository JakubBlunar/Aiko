/**
 * AmbientBodyChannel tests.
 *
 * Behaviour matrix:
 *
 *   - Auto-blush envelope ramps to 1 only on the right mood label
 *     + intensity threshold; decays to 0 otherwise.
 *   - Auto-sweat envelope ramps to 1 on either reaction OR mood
 *     match; decays back to 0.
 *   - Cat-tail sine: arousal-driven baseline; boost factor when
 *     ``engineState.tailWagBoostUntil > now``.
 *   - Body language sums: lean-in, slump, excited bounce, breath,
 *     sass tilt — all gated on capability flags.
 *   - Sass tilt fires only on a true reaction transition (not
 *     every frame the snapshot holds the value).
 *   - Cleanup writes 0 to every owned param on detach.
 */
import { describe, expect, it } from "vitest";

import { AmbientBodyChannel } from "./AmbientBodyChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState, type EngineState } from "../state";
import type { ChannelDeps, ChannelStoreSnapshot } from "../types";

interface DepsBundle {
  deps: ChannelDeps;
  clock: FakeClock;
  engineState: EngineState;
  setSnapshot: (next: Partial<ChannelStoreSnapshot>) => void;
}

function makeDeps(
  capabilities: Record<string, boolean> = {},
  initial: Partial<ChannelStoreSnapshot> = {},
  manifestOverrides: Parameters<typeof buildManifest>[0] = {},
): DepsBundle {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  let snap: ChannelStoreSnapshot = {
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
    circadianPeriod: "",
    ...initial,
  };
  const manifest = buildManifest({
    capabilities,
    overlays: {
      blush: { param_id: "ParamCheek", on_value: 1, decay_ms: 600, label_en: "blush" },
      sweat: { param_id: "ParamSweat", on_value: 1, decay_ms: 1500, label_en: "sweat" },
    },
    ...manifestOverrides,
  });
  return {
    clock,
    engineState,
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snap,
    },
    setSnapshot: (next) => {
      snap = { ...snap, ...next };
    },
  };
}

describe("AmbientBodyChannel — blush", () => {
  it("ramps blush toward 1 when mood label matches with intensity > 0.4", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps } = makeDeps(
      { has_blush: true },
      { mood: { label: "tender", intensity: 0.8, valence: 0.5, arousal: 0.3 } },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 30; i += 1) {
      channel.tickTier3!(0, 0.05);
    }
    expect(channel.blushEnvelope).toBeGreaterThan(0.9);
    expect(adapter.params.get("ParamCheek")).toBeGreaterThan(0.9);
  });

  it("decays back to 0 when mood is no longer a blush trigger", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, setSnapshot } = makeDeps(
      { has_blush: true },
      { mood: { label: "tender", intensity: 0.8, valence: 0.5, arousal: 0.3 } },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 30; i += 1) channel.tickTier3!(0, 0.05);
    expect(channel.blushEnvelope).toBeGreaterThan(0.9);

    setSnapshot({
      mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
    });
    for (let i = 0; i < 60; i += 1) channel.tickTier3!(0, 0.05);
    expect(channel.blushEnvelope).toBeLessThan(0.05);
  });

  it("is gated by has_blush capability", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps } = makeDeps(
      {},
      { mood: { label: "tender", intensity: 0.8, valence: 0.5, arousal: 0.3 } },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 30; i += 1) channel.tickTier3!(0, 0.05);
    expect(adapter.params.has("ParamCheek")).toBe(false);
  });
});

describe("AmbientBodyChannel — sweat", () => {
  it("triggers on reaction match even with neutral mood", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps } = makeDeps({ has_sweat: true }, { reaction: "concerned" });
    channel.attach(adapter, deps);
    for (let i = 0; i < 60; i += 1) channel.tickTier3!(0, 0.1);
    expect(channel.sweatEnvelope).toBeGreaterThan(0.9);
  });

  it("triggers on mood match even with neutral reaction", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps } = makeDeps(
      { has_sweat: true },
      { mood: { label: "frustrated", intensity: 0.7, valence: -0.3, arousal: 0.5 } },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 60; i += 1) channel.tickTier3!(0, 0.1);
    expect(channel.sweatEnvelope).toBeGreaterThan(0.9);
  });
});

describe("AmbientBodyChannel — cat-tail sine", () => {
  it("writes a sine to every cat-tail segment with arousal-driven amp", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock } = makeDeps(
      { has_cat_tail: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 } },
      { cat_tail_param_ids: ["Tail1", "Tail2", "Tail3"] },
    );
    channel.attach(adapter, deps);
    let maxSeen = 0;
    for (let i = 0; i < 200; i += 1) {
      channel.tickTier3!(clock.advance(20), 0.02);
      const v = Math.abs(adapter.params.get("Tail1") ?? 0);
      if (v > maxSeen) maxSeen = v;
    }
    // Baseline amp at arousal=0.5 is 4 + 12*0.5 = 10. The peak of
    // a sine sweep over 4 seconds easily reaches the amp.
    expect(maxSeen).toBeGreaterThan(7);
    // Each segment got at least one write.
    expect(adapter.params.has("Tail2")).toBe(true);
    expect(adapter.params.has("Tail3")).toBe(true);
  });

  it("multiplies amp by 1.5x while engineState.tailWagBoostUntil is in the future", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock, engineState } = makeDeps(
      { has_cat_tail: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 } },
      { cat_tail_param_ids: ["Tail1"] },
    );
    channel.attach(adapter, deps);

    // Without boost — track peak.
    let baseMax = 0;
    for (let i = 0; i < 200; i += 1) {
      channel.tickTier3!(clock.advance(20), 0.02);
      const v = Math.abs(adapter.params.get("Tail1") ?? 0);
      if (v > baseMax) baseMax = v;
    }

    // With boost active, run a fresh window.
    engineState.tailWagBoostUntil = clock.now() + 5_000;
    let boostMax = 0;
    for (let i = 0; i < 200; i += 1) {
      channel.tickTier3!(clock.advance(20), 0.02);
      const v = Math.abs(adapter.params.get("Tail1") ?? 0);
      if (v > boostMax) boostMax = v;
    }

    expect(boostMax).toBeGreaterThan(baseMax * 1.2);
  });
});

describe("AmbientBodyChannel — body language", () => {
  it("listening voice mode raises ParamBodyAngleY (lean-in)", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps } = makeDeps(
      { has_body_angle_y: true },
      { voiceMode: "listening" },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 50; i += 1) channel.tickTier3!(0, 0.05);
    expect(channel.leanInEnvelope).toBeGreaterThan(0.9);
    // Lean-in alone contributes +6 to Y; idle contributions add up
    // to a small breathing modulation on Z, not Y.
    expect(adapter.params.get("ParamBodyAngleY")).toBeGreaterThan(5.5);
  });

  it("B8: typed composing also raises ParamBodyAngleY (lean-in), then relaxes when it clears", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, setSnapshot } = makeDeps(
      { has_body_angle_y: true },
      { voiceMode: "off", composing: true },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 50; i += 1) channel.tickTier3!(0, 0.05);
    expect(channel.leanInEnvelope).toBeGreaterThan(0.9);
    expect(adapter.params.get("ParamBodyAngleY")).toBeGreaterThan(5.5);

    // User stopped typing — the lean relaxes back out.
    setSnapshot({ composing: false });
    for (let i = 0; i < 60; i += 1) channel.tickTier3!(0, 0.05);
    expect(channel.leanInEnvelope).toBeLessThan(0.05);
  });

  it("late-night + low arousal triggers slump (negative Y contribution)", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps } = makeDeps(
      { has_body_angle_y: true },
      {
        circadianPeriod: "late_night",
        mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.1 },
      },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 100; i += 1) channel.tickTier3!(0, 0.05);
    expect(channel.slumpEnvelope).toBeGreaterThan(0.9);
    expect(adapter.params.get("ParamBodyAngleY")).toBeLessThan(-2.5);
  });

  it("sass tilt fires only on a true reaction transition", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock } = makeDeps({ has_body_angle_z: true }, { reaction: "neutral" });
    channel.attach(adapter, deps);

    // Fire two ticks at neutral — no sass.
    channel.tickTier3!(clock.now(), 0);
    expect(channel.sassTriggeredAt).toBe(-Infinity);

    // Reaction transitions to amused — sass burst arms.
    channel.onReaction!("amused");
    expect(channel.sassTriggeredAt).toBeGreaterThan(0);
    const firedAt = channel.sassTriggeredAt;

    // Same reaction again — does NOT re-arm.
    channel.onReaction!("amused");
    expect(channel.sassTriggeredAt).toBe(firedAt);
  });
});

describe("AmbientBodyChannel — lifecycle", () => {
  it("detach() resets every owned param to 0", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps } = makeDeps(
      {
        has_blush: true,
        has_sweat: true,
        has_body_angle_y: true,
        has_body_angle_z: true,
      },
      { mood: { label: "tender", intensity: 0.8, valence: 0.5, arousal: 0.7 } },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 30; i += 1) channel.tickTier3!(0, 0.05);
    channel.detach();
    expect(adapter.params.get("ParamCheek")).toBe(0);
    expect(adapter.params.get("ParamSweat")).toBe(0);
    expect(adapter.params.get("ParamBodyAngleY")).toBe(0);
    expect(adapter.params.get("ParamBodyAngleZ")).toBe(0);
  });
});

/** Count zero-crossings on a sampled scalar wave — the crude but
 * test-friendly way to derive the dominant frequency without
 * pulling in an FFT. We compare two arousal regimes; the higher
 * arousal must produce strictly more crossings over the same
 * sampling window. */
function countZeroCrossings(samples: number[], baseline: number): number {
  let crossings = 0;
  for (let i = 1; i < samples.length; i += 1) {
    const a = samples[i - 1] - baseline;
    const b = samples[i] - baseline;
    if ((a < 0 && b >= 0) || (a > 0 && b <= 0)) {
      crossings += 1;
    }
  }
  return crossings;
}

/** Step the channel's ``tickPreModel`` ``frames`` times, advancing
 * the clock by ``dtSec`` seconds each tick. Returns the wave that
 * was written to ``ParamBreath`` over the run. The valence-tilt
 * test layers on the same fixture but reads ``ParamBodyAngleY``. */
function samplePreModel(
  channel: AmbientBodyChannel,
  adapter: FakeAdapter,
  clock: FakeClock,
  paramId: string,
  frames: number,
  dtSec: number,
): number[] {
  const out: number[] = [];
  for (let i = 0; i < frames; i += 1) {
    clock.advance(dtSec * 1000);
    channel.tickPreModel!();
    out.push(adapter.params.get(paramId) ?? 0);
  }
  return out;
}

describe("AmbientBodyChannel — tickPreModel: arousal-scaled breath", () => {
  it("writes a sine to ParamBreath gated on has_breath capability", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock } = makeDeps(
      { has_breath: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 } },
    );
    channel.attach(adapter, deps);
    const wave = samplePreModel(channel, adapter, clock, "ParamBreath", 600, 1 / 60);
    const min = Math.min(...wave);
    const max = Math.max(...wave);
    // Wave should oscillate around 0.5 with non-trivial amplitude.
    expect(min).toBeLessThan(0.2);
    expect(max).toBeGreaterThan(0.8);
  });

  it("higher arousal produces more zero-crossings (faster breath)", () => {
    const adapter = new FakeAdapter();
    const channelLow = new AmbientBodyChannel();
    const lowDeps = makeDeps(
      { has_breath: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.1 } },
    );
    channelLow.attach(adapter, lowDeps.deps);
    const lowWave = samplePreModel(
      channelLow,
      adapter,
      lowDeps.clock,
      "ParamBreath",
      1800, // 30s @ 60fps
      1 / 60,
    );

    const adapter2 = new FakeAdapter();
    const channelHigh = new AmbientBodyChannel();
    const highDeps = makeDeps(
      { has_breath: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.9 } },
    );
    channelHigh.attach(adapter2, highDeps.deps);
    const highWave = samplePreModel(
      channelHigh,
      adapter2,
      highDeps.clock,
      "ParamBreath",
      1800,
      1 / 60,
    );

    const lowCrossings = countZeroCrossings(lowWave, 0.5);
    const highCrossings = countZeroCrossings(highWave, 0.5);
    expect(highCrossings).toBeGreaterThan(lowCrossings);
    // Sanity: arousal-0.9 frequency is ~1.36x arousal-0.1 — assert at
    // least 1.2x to absorb the discrete-sampling slop.
    expect(highCrossings).toBeGreaterThan(lowCrossings * 1.2);
  });

  it("does not touch ParamBreath when has_breath is missing", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock } = makeDeps(
      {},
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 } },
    );
    channel.attach(adapter, deps);
    samplePreModel(channel, adapter, clock, "ParamBreath", 100, 1 / 60);
    expect(adapter.params.has("ParamBreath")).toBe(false);
  });

  it("boosts ParamBreath frequency while tailWagBoostUntil is in the future and has_tail_wag is on", () => {
    // Physics-driven tail rigs (Alexia) read ParamBreath via the
    // physics chain. Boosting freq here is what makes the tail
    // visibly wag faster during ``[[overlay:tail_wag]]``; the
    // ``tickTier3`` direct-sine path only matters for non-physics
    // rigs.
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock, engineState } = makeDeps(
      { has_breath: true, has_tail_wag: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 } },
    );
    channel.attach(adapter, deps);

    // Baseline: no boost active. Sample 30s @ 60fps for a stable
    // zero-crossing count.
    const baseline = samplePreModel(channel, adapter, clock, "ParamBreath", 1800, 1 / 60);

    // Activate the boost. Use a deadline far enough out that all
    // boosted samples land inside it.
    engineState.tailWagBoostUntil = clock.now() + 60_000;
    const boosted = samplePreModel(channel, adapter, clock, "ParamBreath", 1800, 1 / 60);

    const baselineCrossings = countZeroCrossings(baseline, 0.5);
    const boostedCrossings = countZeroCrossings(boosted, 0.5);
    // 2.5x freq mul; allow 1.6x slack for sampling noise.
    expect(boostedCrossings).toBeGreaterThan(baselineCrossings * 1.6);
  });

  it("returns ParamBreath to baseline frequency once tailWagBoostUntil expires", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock, engineState } = makeDeps(
      { has_breath: true, has_tail_wag: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 } },
    );
    channel.attach(adapter, deps);

    // Boost a short window, then sample after expiry.
    engineState.tailWagBoostUntil = clock.now() + 100;
    samplePreModel(channel, adapter, clock, "ParamBreath", 60, 1 / 60); // burn the boost window

    // After expiry, the breath driver should match an unboosted run.
    const afterBoost = samplePreModel(channel, adapter, clock, "ParamBreath", 1800, 1 / 60);

    const adapter2 = new FakeAdapter();
    const channel2 = new AmbientBodyChannel();
    const { deps: deps2, clock: clock2 } = makeDeps(
      { has_breath: true, has_tail_wag: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 } },
    );
    channel2.attach(adapter2, deps2);
    const unboosted = samplePreModel(channel2, adapter2, clock2, "ParamBreath", 1800, 1 / 60);

    const afterCrossings = countZeroCrossings(afterBoost, 0.5);
    const unboostedCrossings = countZeroCrossings(unboosted, 0.5);
    // After the boost expires, frequency should match within ~15%
    // (small drift from clock offset / sampling phase).
    expect(Math.abs(afterCrossings - unboostedCrossings)).toBeLessThan(
      Math.max(unboostedCrossings * 0.15, 4),
    );
  });

  it("does not boost ParamBreath when has_tail_wag is missing", () => {
    // A rig without the cat-tail capability shouldn't accelerate
    // its breath even if some other channel set ``tailWagBoostUntil``.
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock, engineState } = makeDeps(
      { has_breath: true },
      { mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 } },
    );
    channel.attach(adapter, deps);

    const unboosted = samplePreModel(channel, adapter, clock, "ParamBreath", 1800, 1 / 60);
    engineState.tailWagBoostUntil = clock.now() + 60_000;
    const stillUnboosted = samplePreModel(channel, adapter, clock, "ParamBreath", 1800, 1 / 60);

    const unCrossings = countZeroCrossings(unboosted, 0.5);
    const stillCrossings = countZeroCrossings(stillUnboosted, 0.5);
    expect(Math.abs(unCrossings - stillCrossings)).toBeLessThan(
      Math.max(unCrossings * 0.15, 4),
    );
  });

  it("expressiveness 0 collapses ParamBreath to a fixed mid-value", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock } = makeDeps(
      { has_breath: true },
      {
        expressiveness: 0,
        mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 },
      },
    );
    channel.attach(adapter, deps);
    const wave = samplePreModel(channel, adapter, clock, "ParamBreath", 200, 1 / 60);
    // A constant fallback so the rig still looks "alive enough" but
    // the slider has visibly muted the rhythm.
    expect(Math.min(...wave)).toBe(0.5);
    expect(Math.max(...wave)).toBe(0.5);
  });
});

describe("AmbientBodyChannel — tickPreModel: valence tilt", () => {
  it("positive valence biases ParamBodyAngleY toward positive degrees", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock } = makeDeps(
      { has_body_angle_y: true },
      { mood: { label: "happy", intensity: 0.7, valence: 0.8, arousal: 0.4 } },
    );
    channel.attach(adapter, deps);
    // Run for long enough that approach() converges.
    for (let i = 0; i < 600; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(channel.valenceTiltEnvelope).toBeGreaterThan(0.7);
    expect(adapter.params.get("ParamBodyAngleY") ?? 0).toBeGreaterThan(1.5);
  });

  it("negative valence biases ParamBodyAngleY toward negative degrees", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock } = makeDeps(
      { has_body_angle_y: true },
      { mood: { label: "sad", intensity: 0.7, valence: -0.8, arousal: 0.4 } },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 600; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(channel.valenceTiltEnvelope).toBeLessThan(-0.7);
    expect(adapter.params.get("ParamBodyAngleY") ?? 0).toBeLessThan(-1.5);
  });

  it("smoothing means a single tick after a flip does not snap to the new tilt", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock, setSnapshot } = makeDeps(
      { has_body_angle_y: true },
      { mood: { label: "happy", intensity: 0.7, valence: 0.8, arousal: 0.4 } },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 600; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    const settled = channel.valenceTiltEnvelope;
    expect(settled).toBeGreaterThan(0.7);

    // Flip valence; the next tick must not snap straight to -0.8.
    setSnapshot({
      mood: { label: "sad", intensity: 0.7, valence: -0.8, arousal: 0.4 },
    });
    clock.advance(1000 / 60);
    channel.tickPreModel!();
    expect(channel.valenceTiltEnvelope).toBeGreaterThan(-0.5);
    expect(channel.valenceTiltEnvelope).toBeLessThan(settled);
  });

  it("does not write ParamBodyAngleY when has_body_angle_y is absent", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps, clock } = makeDeps(
      {},
      { mood: { label: "happy", intensity: 0.7, valence: 0.8, arousal: 0.4 } },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 100; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(adapter.params.has("ParamBodyAngleY")).toBe(false);
  });
});

describe("AmbientBodyChannel — expressiveness scaling", () => {
  it("expressiveness 0 mutes lean-in body contribution", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    const { deps } = makeDeps(
      { has_body_angle_y: true },
      { voiceMode: "listening", expressiveness: 0 },
    );
    channel.attach(adapter, deps);
    for (let i = 0; i < 50; i += 1) channel.tickTier3!(0, 0.05);
    // Envelope still ramps, but its written contribution is gated to 0.
    expect(channel.leanInEnvelope).toBeGreaterThan(0.9);
    expect(Math.abs(adapter.params.get("ParamBodyAngleY") ?? 0)).toBeLessThan(0.001);
  });

  it("expressiveness 1.5 amplifies lean-in body contribution by 1.5x relative to default", () => {
    // Default run.
    const adapterDefault = new FakeAdapter();
    const channelDefault = new AmbientBodyChannel();
    const { deps: depsDefault } = makeDeps(
      { has_body_angle_y: true },
      { voiceMode: "listening" /* expressiveness defaults to 1 */ },
    );
    channelDefault.attach(adapterDefault, depsDefault);
    for (let i = 0; i < 200; i += 1) channelDefault.tickTier3!(0, 0.05);
    const defaultY = adapterDefault.params.get("ParamBodyAngleY") ?? 0;

    // Amplified run.
    const adapterAmp = new FakeAdapter();
    const channelAmp = new AmbientBodyChannel();
    const { deps: depsAmp } = makeDeps(
      { has_body_angle_y: true },
      { voiceMode: "listening", expressiveness: 1.5 },
    );
    channelAmp.attach(adapterAmp, depsAmp);
    for (let i = 0; i < 200; i += 1) channelAmp.tickTier3!(0, 0.05);
    const ampY = adapterAmp.params.get("ParamBodyAngleY") ?? 0;

    expect(ampY).toBeGreaterThan(defaultY * 1.4);
    expect(ampY).toBeLessThanOrEqual(defaultY * 1.6);
  });

  it("expressiveness scales the valence-tilt write proportionally", () => {
    const adapterFull = new FakeAdapter();
    const channelFull = new AmbientBodyChannel();
    const { deps: depsFull, clock: clockFull } = makeDeps(
      { has_body_angle_y: true },
      { mood: { label: "happy", intensity: 0.7, valence: 1.0, arousal: 0.4 } },
    );
    channelFull.attach(adapterFull, depsFull);
    for (let i = 0; i < 600; i += 1) {
      clockFull.advance(1000 / 60);
      channelFull.tickPreModel!();
    }
    const fullValueY = adapterFull.params.get("ParamBodyAngleY") ?? 0;

    const adapterHalf = new FakeAdapter();
    const channelHalf = new AmbientBodyChannel();
    const { deps: depsHalf, clock: clockHalf } = makeDeps(
      { has_body_angle_y: true },
      {
        mood: { label: "happy", intensity: 0.7, valence: 1.0, arousal: 0.4 },
        expressiveness: 0.5,
      },
    );
    channelHalf.attach(adapterHalf, depsHalf);
    for (let i = 0; i < 600; i += 1) {
      clockHalf.advance(1000 / 60);
      channelHalf.tickPreModel!();
    }
    const halfValueY = adapterHalf.params.get("ParamBodyAngleY") ?? 0;

    // halfValueY should be ~half of fullValueY (both share the same
    // converged ``valenceTilt`` since the smoothing is independent
    // of expressiveness).
    expect(halfValueY).toBeGreaterThan(0);
    expect(halfValueY).toBeLessThan(fullValueY);
    // Allow generous tolerance — the bias is added to whatever the
    // adapter reports for ``ParamBodyAngleY`` which itself starts
    // at 0 here.
    expect(halfValueY / fullValueY).toBeGreaterThan(0.4);
    expect(halfValueY / fullValueY).toBeLessThan(0.6);
  });

  it("missing expressiveness in the snapshot defaults to 1.0", () => {
    const adapter = new FakeAdapter();
    const channel = new AmbientBodyChannel();
    // Build a snapshot without expressiveness — channels must
    // tolerate the legacy shape.
    const { deps: baseDeps } = makeDeps(
      { has_body_angle_y: true },
      { voiceMode: "listening" },
    );
    const noExpressiveness: ChannelDeps = {
      ...baseDeps,
      getStoreSnapshot: () => {
        const snap = baseDeps.getStoreSnapshot();
        const cleaned = { ...snap } as ChannelStoreSnapshot;
        delete cleaned.expressiveness;
        return cleaned;
      },
    };
    channel.attach(adapter, noExpressiveness);
    for (let i = 0; i < 100; i += 1) channel.tickTier3!(0, 0.05);
    // Default expressiveness 1 -> lean-in amplitude of 6 fully applied.
    expect(adapter.params.get("ParamBodyAngleY") ?? 0).toBeGreaterThan(5);
  });
});
