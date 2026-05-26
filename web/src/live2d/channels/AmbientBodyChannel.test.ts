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
