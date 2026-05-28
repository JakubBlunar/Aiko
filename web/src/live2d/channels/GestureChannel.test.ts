/**
 * GestureChannel tests.
 *
 * Behaviour matrix:
 *
 *   - Wink: ParamEyeLOpen/ROpen clamped to 0 while alive; on
 *     expiry the channel releases to 1 EXACTLY ONCE (not every
 *     frame thereafter). Capability gating drops the gesture
 *     when ``has_wink`` is false.
 *
 *   - Ear-wiggle: sine on every ear param while alive; snap to 0
 *     once on expiry. Capability gating drops without
 *     ``has_ear_wiggle``. Empty ``cat_ear_param_ids`` is a no-op
 *     even when the cap is set.
 *
 *   - Tail-wag: writes ``engineState.tailWagBoostUntil`` rather
 *     than driving params directly (AmbientBodyChannel reads it).
 *     Stale deadlines self-clear once expired so the read becomes
 *     a single ``> 0`` check.
 */
import { describe, expect, it } from "vitest";

import { GestureChannel } from "./GestureChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState, type EngineState } from "../state";
import type { ChannelDeps } from "../types";

interface DepsBundle {
  deps: ChannelDeps;
  clock: FakeClock;
  engineState: EngineState;
}

function makeDeps(
  capabilities: Record<string, boolean>,
  manifestOverrides: Parameters<typeof buildManifest>[0] = {},
): DepsBundle {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  const manifest = buildManifest({ capabilities, ...manifestOverrides });
  return {
    clock,
    engineState,
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => ({
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
      }),
    },
  };
}

describe("GestureChannel — wink", () => {
  it("clamps ParamEyeLOpen to 0 while alive, releases to 1 once on expiry", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps({ has_wink: true });
    channel.attach(adapter, deps);

    channel.onOverlay!({ name: "wink_left", until: clock.now() + 100 });
    expect(channel.isAnyActive).toBe(true);

    // Frame 1 — alive.
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.params.get("ParamEyeLOpen")).toBe(0);

    // Frame 2 — still alive.
    channel.tickTier3!(clock.advance(50), 0.05);
    expect(adapter.params.get("ParamEyeLOpen")).toBe(0);

    // Frame 3 — past the deadline; release to 1 exactly once.
    channel.tickTier3!(clock.advance(60), 0.06);
    expect(adapter.params.get("ParamEyeLOpen")).toBe(1);
    expect(channel.isAnyActive).toBe(false);

    // Frame 4+ — no further writes.
    const beforeFrame4 = adapter.setParamHistory.length;
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.setParamHistory.length).toBe(beforeFrame4);
  });

  it("wink_right targets ParamEyeROpen", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps({ has_wink: true });
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "wink_right", until: clock.now() + 100 });
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.params.get("ParamEyeROpen")).toBe(0);
    expect(adapter.params.has("ParamEyeLOpen")).toBe(false);
  });

  it("is dropped silently when has_wink is missing", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps({});
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "wink_left", until: clock.now() + 100 });
    expect(channel.isAnyActive).toBe(false);
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.setParamHistory).toHaveLength(0);
  });
});

describe("GestureChannel — ear_wiggle (tickTier3 fallback)", () => {
  it("writes a 4 Hz sine on every ear segment, then 0 on expiry", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      { has_ear_wiggle: true },
      { cat_ear_param_ids: ["EarL1", "EarL2", "EarR1"] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 200 });

    channel.tickTier3!(clock.advance(16), 0.016);
    // Each ear param got the same sine value.
    const v1 = adapter.params.get("EarL1");
    expect(v1).toBeDefined();
    expect(adapter.params.get("EarL2")).toBe(v1);
    expect(adapter.params.get("EarR1")).toBe(v1);
    expect(Math.abs(v1!)).toBeLessThanOrEqual(15 + 1e-9);

    // Past the deadline — every ear param snaps back to 0 in
    // tickTier3.
    channel.tickTier3!(clock.advance(220), 0.22);
    expect(adapter.params.get("EarL1")).toBe(0);
    expect(adapter.params.get("EarL2")).toBe(0);
    expect(adapter.params.get("EarR1")).toBe(0);
    // Slot lifecycle moved to tickPreModel (so the post-physics
    // rest-snap on physics-driven rigs is the LAST write of the
    // expiry frame). tickPreModel completes the cycle and nulls
    // the slot.
    channel.tickPreModel!();
    expect(channel.isAnyActive).toBe(false);
  });

  it("is gated by has_ear_wiggle (no cap = no fire)", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      {},
      { cat_ear_param_ids: ["EarL1"] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 100 });
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("is a no-op when cat_ear_param_ids is empty even with has_ear_wiggle", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps({ has_ear_wiggle: true }, { cat_ear_param_ids: [] });
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 100 });
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.setParamHistory).toHaveLength(0);
  });
});

describe("GestureChannel — ear_wiggle (tickPreModel post-physics path)", () => {
  // tickPreModel runs after ``physics.evaluate`` (see
  // ``docs/alexia-model-notes.md`` §5), so writes here win on
  // physics-driven rigs where the ear params are downstream of
  // ``ParamEyeR/LOpen`` via PhysicsSetting13/14. Without this path
  // the tickTier3 sine is silently overwritten by physics on every
  // frame and ``[[overlay:ear_wiggle]]`` produces no visible motion
  // on Alexia.

  it("writes a 4 Hz sine on every ear segment in tickPreModel while alive", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      { has_ear_wiggle: true },
      { cat_ear_param_ids: ["EarA", "EarB", "EarC", "EarD"] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 600 });

    clock.advance(16);
    channel.tickPreModel!();
    const v1 = adapter.params.get("EarA");
    expect(v1).toBeDefined();
    // All four params share the same sine value (single-phase
    // write — the rig groups them under one curve so phase offsets
    // would fight the rigging).
    expect(adapter.params.get("EarB")).toBe(v1);
    expect(adapter.params.get("EarC")).toBe(v1);
    expect(adapter.params.get("EarD")).toBe(v1);
    expect(Math.abs(v1!)).toBeLessThanOrEqual(15 + 1e-9);
  });

  it("writes 0 to every ear segment exactly once on expiry, then nulls the slot", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      { has_ear_wiggle: true },
      { cat_ear_param_ids: ["EarA", "EarB"] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 100 });

    // Burn through the gesture window, then tick past expiry.
    clock.advance(200);
    channel.tickPreModel!();
    expect(adapter.params.get("EarA")).toBe(0);
    expect(adapter.params.get("EarB")).toBe(0);
    expect(channel.isAnyActive).toBe(false);

    // Subsequent tickPreModel calls are pure no-ops.
    const beforeCount = adapter.setParamHistory.length;
    clock.advance(16);
    channel.tickPreModel!();
    expect(adapter.setParamHistory.length).toBe(beforeCount);
  });

  it("rest-snap is the LAST write of the expiry frame (wins over post-tickTier3 writes)", () => {
    // Simulates the production frame order: tickTier3 → physics →
    // tickPreModel. We don't actually run physics here, but we
    // verify that on a frame where tickTier3 also wrote (sine OR
    // rest-snap), the tickPreModel rest-snap is still the last
    // visible write — that's the property that wins on Alexia.
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      { has_ear_wiggle: true },
      { cat_ear_param_ids: ["EarA"] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 100 });

    clock.advance(200); // past the deadline
    // tickTier3 first.
    channel.tickTier3!(clock.now(), 0.2);
    // Pretend physics ran here and clobbered the rest write with
    // an arbitrary non-zero value (this is what
    // PhysicsSetting13 / 14 do on Alexia from ParamEyeR/LOpen).
    adapter.setParam("EarA", 0.42);
    // Now tickPreModel runs and must clobber the physics output
    // with the rest-snap.
    channel.tickPreModel!();
    expect(adapter.params.get("EarA")).toBe(0);
  });

  it("is a no-op in tickPreModel when has_ear_wiggle is missing", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      {},
      { cat_ear_param_ids: ["EarA"] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 100 });
    clock.advance(16);
    channel.tickPreModel!();
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("retires the slot on expiry even when cat_ear_param_ids is empty", () => {
    // No ear segments to write — the channel must still retire the
    // gesture so a leaked slot doesn't keep ``isAnyActive`` stuck
    // true forever.
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      { has_ear_wiggle: true },
      { cat_ear_param_ids: [] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 100 });
    expect(channel.isAnyActive).toBe(true);
    clock.advance(200);
    channel.tickPreModel!();
    expect(channel.isAnyActive).toBe(false);
  });
});

describe("GestureChannel — tail_wag boost", () => {
  it("writes engineState.tailWagBoostUntil and self-clears on expiry", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock, engineState } = makeDeps({ has_tail_wag: true });
    channel.attach(adapter, deps);

    const deadline = clock.now() + 500;
    channel.onOverlay!({ name: "tail_wag", until: deadline });
    expect(engineState.tailWagBoostUntil).toBe(deadline);

    // Tick past the deadline — gesture clears the stale deadline.
    channel.tickTier3!(clock.advance(600), 0.6);
    expect(engineState.tailWagBoostUntil).toBe(0);
  });

  it("does NOT drive any param itself (AmbientBody owns the sine)", () => {
    // The whole point of the split is that GestureChannel is silent
    // on the cat-tail params; AmbientBodyChannel reads the boost
    // deadline and applies it. This test guards against accidentally
    // moving the sine into GestureChannel.
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      { has_tail_wag: true, has_cat_tail: true },
      { cat_tail_param_ids: ["Tail1", "Tail2"] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "tail_wag", until: clock.now() + 1_000 });
    for (let i = 0; i < 30; i += 1) {
      channel.tickTier3!(clock.advance(16), 0.016);
    }
    expect(adapter.params.has("Tail1")).toBe(false);
    expect(adapter.params.has("Tail2")).toBe(false);
  });

  it("is gated by has_tail_wag (no cap = no boost)", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock, engineState } = makeDeps({});
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "tail_wag", until: clock.now() + 500 });
    expect(engineState.tailWagBoostUntil).toBe(0);
  });
});

describe("GestureChannel — lifecycle", () => {
  it("detach() releases winks and zeros ear params", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps(
      { has_wink: true, has_ear_wiggle: true, has_tail_wag: true },
      { cat_ear_param_ids: ["EarL1"] },
    );
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "wink_left", until: clock.now() + 1_000 });
    channel.onOverlay!({ name: "wink_right", until: clock.now() + 1_000 });
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 1_000 });

    channel.detach();
    // Detach should have released both eye params + zeroed the ears.
    expect(adapter.params.get("ParamEyeLOpen")).toBe(1);
    expect(adapter.params.get("ParamEyeROpen")).toBe(1);
    expect(adapter.params.get("EarL1")).toBe(0);
  });

  it("non-gesture overlays are ignored entirely", () => {
    const adapter = new FakeAdapter();
    const channel = new GestureChannel();
    const { deps, clock } = makeDeps({
      has_wink: true,
      has_tail_wag: true,
      has_ear_wiggle: true,
    });
    channel.attach(adapter, deps);
    channel.onOverlay!({ name: "blush", until: clock.now() + 500 });
    channel.onOverlay!({ name: "stars", until: clock.now() + 500 });
    expect(channel.isAnyActive).toBe(false);
  });
});
