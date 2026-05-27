/**
 * AccessoryChannel tests (Phase 4 expression overhaul).
 *
 * Goals:
 *   1. Toggle accessory drives the rig's expression_params for the
 *      backing expression stem (lollipop → bbt params, etc.).
 *   2. Outfit-gated accessory zeros out when the gate fails (zs1 /
 *      crossed_arms vs. pajamas).
 *   3. Eye-color enum routes to the right halves (yjys1 / yjys2).
 *   4. The channel is a total no-op on rigs that don't advertise
 *      any accessory capability — guards minimum-viable future
 *      rigs from accidental param writes.
 *   5. Capability gating per-row: ``has_lollipop`` False keeps the
 *      lollipop write silent even if the user toggled it on.
 */
import { describe, expect, it } from "vitest";

import { AccessoryChannel } from "./AccessoryChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type {
  AvatarManifest,
  ChannelDeps,
  ChannelStoreSnapshot,
} from "../types";
import type { ExpressionParam } from "../../types";

/** Alexia-style expression-param bindings for the accessory stems.
 * The numeric param ids match the actual rig's parameter list so
 * the test stays anchored to ``docs/alexia-model-notes.md``. */
function accessoryBindings(): Record<string, ExpressionParam[]> {
  return {
    bbt: [{ param_id: "Param48", on_value: 30 }],
    dyj: [{ param_id: "Param52", on_value: 30 }],
    mj: [{ param_id: "Param53", on_value: 30 }],
    zs1: [{ param_id: "Param61", on_value: 30 }],
    yjys1: [{ param_id: "Param62", on_value: 30 }],
    yjys2: [{ param_id: "Param63", on_value: 30 }],
  };
}

function fullCapabilities(): Record<string, boolean> {
  return {
    has_lollipop: true,
    has_eyeglasses: true,
    has_head_sunglasses: true,
    has_crossed_arms: true,
    has_eye_color_a: true,
    has_eye_color_b: true,
    has_day_clothes: true,
  };
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
function advance(
  channel: AccessoryChannel,
  clock: FakeClock,
  totalSeconds: number,
) {
  const stepMs = 1000 / 60;
  const stepSec = stepMs / 1000;
  const steps = Math.floor((totalSeconds * 1000) / stepMs);
  for (let i = 0; i < steps; i += 1) {
    channel.tickTier3!(clock.advance(stepMs), stepSec);
  }
}

function lastWrite(adapter: FakeAdapter, paramId: string): number | undefined {
  for (let i = adapter.setParamHistory.length - 1; i >= 0; i -= 1) {
    if (adapter.setParamHistory[i].paramId === paramId) {
      return adapter.setParamHistory[i].value;
    }
  }
  return undefined;
}

describe("AccessoryChannel — capability gating", () => {
  it("is a total no-op when no accessory capabilities are set", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: {},
      expression_params: accessoryBindings(),
      settings: {
        scale_multiplier: 1,
        auto_outfit: "auto",
        expressiveness: 1,
        accessory_state: { lollipop: true },
      },
    });
    channel.attach(adapter, makeDeps(manifest, { resolvedOutfit: "day" }));
    const clock = new FakeClock();
    for (let i = 0; i < 60; i += 1) {
      channel.tickTier3!(clock.advance(16), 0.016);
    }
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("skips an accessory whose has_<key> is false", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: { has_eyeglasses: true },
      expression_params: accessoryBindings(),
      settings: {
        scale_multiplier: 1,
        auto_outfit: "auto",
        expressiveness: 1,
        accessory_state: { lollipop: true, eyeglasses: true },
      },
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "day" }, clock),
    );
    advance(channel, clock, 4.0);
    // ``has_eyeglasses`` is set, so its param ramps up; ``has_lollipop``
    // is missing so its bound param is never written.
    expect(lastWrite(adapter, "Param48")).toBeUndefined();
    expect(lastWrite(adapter, "Param52")!).toBeGreaterThan(29.5);
  });
});

describe("AccessoryChannel — toggles drive backing params", () => {
  it("lollipop=true ramps Param48 toward 30", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: fullCapabilities(),
      expression_params: accessoryBindings(),
      settings: {
        scale_multiplier: 1,
        auto_outfit: "auto",
        expressiveness: 1,
        accessory_state: { lollipop: true },
      },
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "day" }, clock),
    );
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param48")!).toBeGreaterThan(29.5);
    expect(channel.envelopeSnapshot.lollipop).toBeGreaterThan(0.99);
  });

  it("lollipop=false leaves Param48 at 0", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: fullCapabilities(),
      expression_params: accessoryBindings(),
      settings: {
        scale_multiplier: 1,
        auto_outfit: "auto",
        expressiveness: 1,
        accessory_state: {},
      },
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "day" }, clock),
    );
    advance(channel, clock, 4.0);
    // Envelope is exactly 0, so the contribution is 0 and no
    // setParam is emitted for Param48.
    expect(lastWrite(adapter, "Param48")).toBeUndefined();
  });
});

describe("AccessoryChannel — outfit gate", () => {
  it("crossed_arms applies under day_clothes", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: fullCapabilities(),
      expression_params: accessoryBindings(),
      outfit_gated_expressions: { zs1: ["day_clothes"] },
      settings: {
        scale_multiplier: 1,
        auto_outfit: "day",
        expressiveness: 1,
        accessory_state: { crossed_arms: true },
      },
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "day" }, clock),
    );
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param61")!).toBeGreaterThan(29.5);
  });

  it("crossed_arms zeros out under pajamas", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: fullCapabilities(),
      expression_params: accessoryBindings(),
      outfit_gated_expressions: { zs1: ["day_clothes"] },
      settings: {
        scale_multiplier: 1,
        auto_outfit: "pajamas",
        expressiveness: 1,
        accessory_state: { crossed_arms: true },
      },
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "pajamas" }, clock),
    );
    advance(channel, clock, 4.0);
    // Gate fails -> envelope stays at 0 -> Param61 is never written.
    expect(lastWrite(adapter, "Param61")).toBeUndefined();
    expect(channel.envelopeSnapshot.crossed_arms).toBeLessThan(0.01);
  });

  it("permissive when resolvedOutfit is unknown / empty", () => {
    // First-frame race: the WS ``avatar`` event hasn't arrived yet
    // so resolvedOutfit is still ``""``. We should NOT punish the
    // user by zeroing every gated accessory — render it and let
    // the next tick correct.
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: fullCapabilities(),
      expression_params: accessoryBindings(),
      outfit_gated_expressions: { zs1: ["day_clothes"] },
      settings: {
        scale_multiplier: 1,
        auto_outfit: "auto",
        expressiveness: 1,
        accessory_state: { crossed_arms: true },
      },
    });
    const clock = new FakeClock();
    channel.attach(adapter, makeDeps(manifest, { resolvedOutfit: "" }, clock));
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param61")!).toBeGreaterThan(29.5);
  });
});

describe("AccessoryChannel — eye_color enum", () => {
  it("both_purple activates yjys1 and yjys2", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: fullCapabilities(),
      expression_params: accessoryBindings(),
      settings: {
        scale_multiplier: 1,
        auto_outfit: "auto",
        expressiveness: 1,
        accessory_state: { eye_color: "both_purple" },
      },
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "day" }, clock),
    );
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param62")!).toBeGreaterThan(29.5);
    expect(lastWrite(adapter, "Param63")!).toBeGreaterThan(29.5);
  });

  it("left_purple activates yjys1 only", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: fullCapabilities(),
      expression_params: accessoryBindings(),
      settings: {
        scale_multiplier: 1,
        auto_outfit: "auto",
        expressiveness: 1,
        accessory_state: { eye_color: "left_purple" },
      },
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "day" }, clock),
    );
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param62")!).toBeGreaterThan(29.5);
    expect(lastWrite(adapter, "Param63")).toBeUndefined();
  });

  it("default leaves both halves at 0", () => {
    const adapter = new FakeAdapter();
    const channel = new AccessoryChannel();
    const manifest = buildManifest({
      capabilities: fullCapabilities(),
      expression_params: accessoryBindings(),
      settings: {
        scale_multiplier: 1,
        auto_outfit: "auto",
        expressiveness: 1,
        accessory_state: { eye_color: "default" },
      },
    });
    const clock = new FakeClock();
    channel.attach(
      adapter,
      makeDeps(manifest, { resolvedOutfit: "day" }, clock),
    );
    advance(channel, clock, 4.0);
    expect(lastWrite(adapter, "Param62")).toBeUndefined();
    expect(lastWrite(adapter, "Param63")).toBeUndefined();
  });
});
