/**
 * LipsyncChannel tests.
 *
 * The behaviour we lock in:
 *
 *   - Writes ONLY on ``tickPreModel`` — never on a tier-3 RAF tick.
 *     If we ever accidentally moved the lipsync write to ``tickTier3``
 *     it would race with motion-manager mouth keyframes (see the
 *     module-level docstring).
 *
 *   - Smoothing eases the smoothed value toward the broadcast target
 *     by ``0.35`` per frame: the per-frame motion is exactly
 *     ``factor * (target - prev)``.
 *
 *   - ``audioAmplitude == 0`` writes ``0`` (no residual leakage).
 *
 *   - Multiple lip-sync ids in the manifest get the same value
 *     written (e.g. a rig that drives two mouth params from the
 *     same source).
 *
 *   - Cubism-version fallback: no declared ids -> default per
 *     cubism_version.
 */
import { describe, expect, it } from "vitest";

import { LipsyncChannel } from "./LipsyncChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type { ChannelDeps, ChannelStoreSnapshot } from "../types";

function makeDeps(
  amplitude: number,
  manifestOverrides: Parameters<typeof buildManifest>[0] = {},
): { deps: ChannelDeps; setAmplitude: (next: number) => void } {
  let amp = amplitude;
  const snapshot: ChannelStoreSnapshot = {
    reaction: "neutral",
    ttsState: "speaking",
    voiceMode: "off",
    turnInProgress: false,
    audioAmplitude: amp,
    avatarOverlay: null,
    avatarMotion: null,
    mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
    resolvedOutfit: "",
    backchannelHint: "",
  };
  const clock = new FakeClock();
  const deps: ChannelDeps = {
    now: clock.now,
    manifest: buildManifest(manifestOverrides),
    engineState: createEngineState(),
    getStoreSnapshot: () => ({ ...snapshot, audioAmplitude: amp }),
  };
  return {
    deps,
    setAmplitude: (next: number) => {
      amp = next;
    },
  };
}

describe("LipsyncChannel — write hook discipline", () => {
  it("writes ONLY on tickPreModel — never on tier-3 / gaze ticks", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps } = makeDeps(0.5);
    channel.attach(adapter, deps);

    // The channel deliberately does NOT implement tickTier3 or
    // tickGaze. The engine's fan-out skips channels that don't
    // implement a hook, so this is the contract we lock in.
    expect((channel as unknown as { tickTier3?: unknown }).tickTier3).toBeUndefined();
    expect((channel as unknown as { tickGaze?: unknown }).tickGaze).toBeUndefined();
    expect(adapter.setParamHistory).toHaveLength(0);

    // tickPreModel writes once per call.
    channel.tickPreModel!();
    expect(adapter.setParamHistory).toHaveLength(1);
  });
});

describe("LipsyncChannel — smoothing", () => {
  it("the first frame moves SMOOTH_FACTOR * target toward target", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps } = makeDeps(1.0);
    channel.attach(adapter, deps);

    channel.tickPreModel!();
    expect(channel.smoothedAmplitude).toBeCloseTo(0.35, 5);
    expect(adapter.params.get("ParamMouthOpenY")).toBeCloseTo(0.35, 5);
  });

  it("repeated frames asymptote toward the target value", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps } = makeDeps(1.0);
    channel.attach(adapter, deps);

    for (let i = 0; i < 30; i += 1) {
      channel.tickPreModel!();
    }
    expect(channel.smoothedAmplitude).toBeGreaterThan(0.99);
  });

  it("amplitude=0 writes 0 (after the smoothed value has decayed)", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps, setAmplitude } = makeDeps(1.0);
    channel.attach(adapter, deps);

    // Fully ramp up.
    for (let i = 0; i < 30; i += 1) {
      channel.tickPreModel!();
    }
    expect(channel.smoothedAmplitude).toBeGreaterThan(0.99);

    // Drop amplitude to 0; let it decay back.
    setAmplitude(0);
    for (let i = 0; i < 30; i += 1) {
      channel.tickPreModel!();
    }
    expect(channel.smoothedAmplitude).toBeLessThan(0.001);
    expect(adapter.params.get("ParamMouthOpenY")).toBeLessThan(0.001);
  });

  it("clamps the smoothed value to [0, 1]", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps } = makeDeps(2.0); // out-of-range
    channel.attach(adapter, deps);
    for (let i = 0; i < 50; i += 1) {
      channel.tickPreModel!();
    }
    expect(channel.smoothedAmplitude).toBeLessThanOrEqual(1);
    expect(adapter.params.get("ParamMouthOpenY")).toBeLessThanOrEqual(1);
  });
});

describe("LipsyncChannel — param targeting", () => {
  it("writes to every id declared in manifest.lip_sync_ids", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps } = makeDeps(1.0, {
      lip_sync_ids: ["ParamMouthOpenY", "ParamMouthOpenY2"],
    });
    channel.attach(adapter, deps);

    channel.tickPreModel!();
    expect(adapter.params.get("ParamMouthOpenY")).toBeCloseTo(0.35, 5);
    expect(adapter.params.get("ParamMouthOpenY2")).toBeCloseTo(0.35, 5);
  });

  it("falls back to ParamMouthOpenY on Cubism 4 when no ids declared", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps } = makeDeps(1.0, { lip_sync_ids: [], cubism_version: 3 });
    channel.attach(adapter, deps);

    channel.tickPreModel!();
    expect(adapter.params.get("ParamMouthOpenY")).toBeCloseTo(0.35, 5);
  });

  it("falls back to PARAM_MOUTH_OPEN_Y on Cubism 2 when no ids declared", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps } = makeDeps(1.0, { lip_sync_ids: [], cubism_version: 2 });
    channel.attach(adapter, deps);

    channel.tickPreModel!();
    expect(adapter.params.get("PARAM_MOUTH_OPEN_Y")).toBeCloseTo(0.35, 5);
    expect(adapter.params.has("ParamMouthOpenY")).toBe(false);
  });
});

describe("LipsyncChannel — lifecycle", () => {
  it("detach() resets smoothed amplitude so a re-attach starts at 0", () => {
    const adapter = new FakeAdapter();
    const channel = new LipsyncChannel();
    const { deps } = makeDeps(1.0);
    channel.attach(adapter, deps);
    for (let i = 0; i < 30; i += 1) {
      channel.tickPreModel!();
    }
    expect(channel.smoothedAmplitude).toBeGreaterThan(0.99);
    channel.detach();
    expect(channel.smoothedAmplitude).toBe(0);

    channel.attach(adapter, deps);
    expect(channel.smoothedAmplitude).toBe(0);
  });

  it("tickPreModel before attach is a no-op (no exception)", () => {
    const channel = new LipsyncChannel();
    expect(() => channel.tickPreModel!()).not.toThrow();
  });
});
