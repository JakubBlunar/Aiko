/**
 * OutfitChannel tests — the goals here are:
 *
 * 1. Lock in the additive-per-param-id sum invariant. The original
 *    bug was that ``Param16`` (shared by ``pajamas`` and
 *    ``pajamas_hooded``) collapsed during the crossfade because
 *    sequential writes overwrote each other.
 * 2. Verify the crossfade moves in the right direction.
 * 3. Verify capability gating: a binding with no capability flag
 *    contributes nothing.
 * 4. Verify the channel is a total no-op on rigs with no outfit
 *    capability at all (``has_pajamas`` / ``has_pajamas_hooded`` /
 *    ``has_day_clothes`` all false).
 */
import { describe, expect, it } from "vitest";

import { OutfitChannel } from "./OutfitChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type { AvatarManifest, ChannelDeps, ChannelStoreSnapshot } from "../types";
import type { OutfitBinding } from "../../types";

/** Alexia-style outfit shape: pajamas + pajamas_hooded share Param16=30,
 * pajamas_hooded adds Param17=30, day_clothes adds ParamDayCloth=15. */
function alexiaLikeOutfits(): Record<string, OutfitBinding> {
  return {
    pajamas: {
      label_en: "Pajamas",
      mutex_with: ["pajamas_hooded", "day_clothes"],
      params: [{ param_id: "Param16", on_value: 30 }],
    },
    pajamas_hooded: {
      label_en: "Pajamas (hooded)",
      mutex_with: ["pajamas", "day_clothes"],
      params: [
        { param_id: "Param16", on_value: 30 },
        { param_id: "Param17", on_value: 30 },
      ],
    },
    day_clothes: {
      label_en: "Day clothes",
      mutex_with: ["pajamas", "pajamas_hooded"],
      params: [{ param_id: "ParamDayCloth", on_value: 15 }],
    },
  };
}

function alexiaLikeManifest(): AvatarManifest {
  return buildManifest({
    capabilities: {
      has_pajamas: true,
      has_pajamas_hooded: true,
      has_day_clothes: true,
    },
    outfits: alexiaLikeOutfits(),
  });
}

interface SnapshotOptions {
  resolvedOutfit?: string;
}

function makeDeps(
  manifest: AvatarManifest,
  options: SnapshotOptions = {},
  clock = new FakeClock(),
): ChannelDeps {
  const snapshot: ChannelStoreSnapshot = {
    reaction: "neutral",
    ttsState: "idle",
    voiceMode: "off",
    turnInProgress: false,
    audioAmplitude: 0,
    avatarOverlay: null,
    avatarMotion: null,
    mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
    resolvedOutfit: (options.resolvedOutfit ?? "") as ChannelStoreSnapshot["resolvedOutfit"],
    backchannelHint: "",
  };
  return {
    now: clock.now,
    manifest,
    engineState: createEngineState(),
    getStoreSnapshot: () => snapshot,
  };
}

/** Drive the channel for ``totalSeconds`` simulating a 60Hz tick rate. */
function advance(channel: OutfitChannel, clock: FakeClock, totalSeconds: number) {
  const stepMs = 1000 / 60;
  const stepSec = stepMs / 1000;
  const steps = Math.floor((totalSeconds * 1000) / stepMs);
  for (let i = 0; i < steps; i += 1) {
    channel.tickTier3!(clock.advance(stepMs), stepSec);
  }
}

/** Last value written for ``paramId`` in the adapter's history, or
 * ``undefined`` if it was never written. */
function lastWrite(adapter: FakeAdapter, paramId: string): number | undefined {
  for (let i = adapter.setParamHistory.length - 1; i >= 0; i -= 1) {
    if (adapter.setParamHistory[i].paramId === paramId) {
      return adapter.setParamHistory[i].value;
    }
  }
  return undefined;
}

/** Find the value of ``paramId`` recorded at frame index ``frame`` of
 * the *crossfade window*, where frame 0 is the first frame after a
 * given clock state. The fake adapter records every write — we pick
 * the latest write that was the SUM for that frame. Each tick writes
 * one value per active param, so we walk backwards and pull the
 * matching paramId after each batch. */
function paramValuesPerFrame(adapter: FakeAdapter, paramId: string): number[] {
  return adapter.setParamHistory
    .filter((r) => r.paramId === paramId)
    .map((r) => r.value);
}

describe("OutfitChannel — capability gating", () => {
  it("is a total no-op when no outfit capabilities are set", () => {
    const adapter = new FakeAdapter();
    const channel = new OutfitChannel();
    const manifest = buildManifest({
      capabilities: {},
      outfits: alexiaLikeOutfits(),
    });
    channel.attach(adapter, makeDeps(manifest, { resolvedOutfit: "pajamas" }));
    const clock = new FakeClock();
    for (let i = 0; i < 60; i += 1) {
      channel.tickTier3!(clock.advance(16), 0.016);
    }
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("only writes params for capabilities that are set", () => {
    const adapter = new FakeAdapter();
    const channel = new OutfitChannel();
    // Only ``has_pajamas`` set — no day_clothes contribution.
    const manifest = buildManifest({
      capabilities: { has_pajamas: true },
      outfits: alexiaLikeOutfits(),
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "pajamas" }, clock),
    );
    // Crossfade is ~800ms; 4s settles to ~99.3%.
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "ParamDayCloth")).toBeUndefined();
    expect(lastWrite(adapter, "Param17")).toBeUndefined();
    // Param16 should have ramped up close to its on_value of 30.
    expect(lastWrite(adapter, "Param16")!).toBeGreaterThan(29.5);
  });
});

describe("OutfitChannel — crossfade direction", () => {
  it("ramps the pajamas envelope toward 1 and day toward 0", () => {
    const adapter = new FakeAdapter();
    const channel = new OutfitChannel();
    const manifest = alexiaLikeManifest();
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "pajamas" }, clock),
    );
    // Bias: start with day having held the rig (envelope=1) and then
    // crossfade toward pajamas. This is the post-circadian-flip case.
    // We can't write _envelope from outside, so instead start at "day"
    // for 1.5s to fully ramp, then re-attach with the same channel and
    // change resolvedOutfit. The channel resets on attach so use a
    // fresh instance.
    // 4s ≈ 5τ; envelope settles to ~99.3%.
    advance(channel, clock, 4.0);
    expect(channel.envelopeSnapshot.pajamas).toBeGreaterThan(0.99);
    expect(channel.envelopeSnapshot.day).toBeLessThan(0.01);
  });
});

describe("OutfitChannel — pajamas <-> pajamas_hooded crossfade", () => {
  it("Param16 stays at 30 throughout the crossfade (additive sum invariant)", () => {
    const adapter = new FakeAdapter();
    const channel = new OutfitChannel();
    const manifest = alexiaLikeManifest();
    const clock = new FakeClock();

    // Phase 1: fully ramp into "pajamas" so Param16=30 and Param17=0.
    let resolved = "pajamas";
    const deps: ChannelDeps = {
      ...makeDeps(manifest, {}, clock),
      getStoreSnapshot: () => ({
        reaction: "neutral",
        ttsState: "idle",
        voiceMode: "off",
        turnInProgress: false,
        audioAmplitude: 0,
        avatarOverlay: null,
        avatarMotion: null,
        mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
        resolvedOutfit: resolved as ChannelStoreSnapshot["resolvedOutfit"],
        backchannelHint: "",
      }),
    };
    channel.attach(adapter, deps);
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param16")!).toBeGreaterThan(29.5);
    expect(lastWrite(adapter, "Param17")!).toBeLessThan(0.2);

    // Phase 2: crossfade pajamas -> pajamas_hooded. Capture every
    // Param16 write through the crossfade window and assert the
    // additive sum stays close to 30 throughout — the proof of
    // additive invariance is that env_pajamas decays at the same
    // rate as env_pajamas_hooded grows, so their sum is conserved
    // (modulo numerical error).
    adapter.setParamHistory.length = 0;
    resolved = "pajamas_hooded";
    advance(channel, clock, 0.8); // exactly one crossfade duration
    const param16Series = paramValuesPerFrame(adapter, "Param16");
    expect(param16Series.length).toBeGreaterThan(20);
    for (const v of param16Series) {
      // Conservation invariant: sum stays within rounding distance
      // of the value at crossfade entry (~29.8).
      expect(v).toBeGreaterThan(29.5);
      expect(v).toBeLessThanOrEqual(30 + 1e-6);
    }
    // Param17 ramps up from 0 to ~63% of 30 in 1τ.
    expect(lastWrite(adapter, "Param17")!).toBeGreaterThan(15);
  });

  it("day fade: Param16 fades from 30 to ~0 when switching to day", () => {
    const adapter = new FakeAdapter();
    const channel = new OutfitChannel();
    const manifest = alexiaLikeManifest();
    const clock = new FakeClock();
    let resolved: string = "pajamas";
    const snapshot: ChannelStoreSnapshot = {
      reaction: "neutral",
      ttsState: "idle",
      voiceMode: "off",
      turnInProgress: false,
      audioAmplitude: 0,
      avatarOverlay: null,
      avatarMotion: null,
      mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
      resolvedOutfit: resolved as ChannelStoreSnapshot["resolvedOutfit"],
      backchannelHint: "",
    };
    const deps: ChannelDeps = {
      now: clock.now,
      manifest,
      engineState: createEngineState(),
      getStoreSnapshot: () => ({
        ...snapshot,
        resolvedOutfit: resolved as ChannelStoreSnapshot["resolvedOutfit"],
      }),
    };
    channel.attach(adapter, deps);
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param16")!).toBeGreaterThan(29.5);

    adapter.setParamHistory.length = 0;
    resolved = "day";
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param16")!).toBeLessThan(0.5);
    expect(lastWrite(adapter, "Param17")!).toBeLessThan(0.5);
    expect(lastWrite(adapter, "ParamDayCloth")!).toBeGreaterThan(14);
  });
});
