/**
 * ReachChannel tests — K31 soft physicality.
 *
 * Covers:
 *
 *   - the lean-in animation writes ``ParamBodyAngleY`` (and, when
 *     gated, ``ParamAngleY``) on a symmetric ease-out / ease-in
 *     curve that peaks near the midpoint
 *   - read-modify-write composes additively on top of an already-
 *     written AmbientBody value (no clobber)
 *   - the channel goes inactive once ``until`` passes and stops
 *     writing on subsequent ticks
 *   - a fresh ``onTouch`` mid-animation restarts the timeline
 *   - capability gating: body-angle missing => no body write;
 *     head-angle missing => no head write; both missing => bail
 *     before storing the active slot
 *   - lifecycle: ``detach`` clears the slot + leaves params at 0
 */
import { describe, expect, it } from "vitest";

import { ReachChannel } from "./ReachChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest, buildStoreSnapshot } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type {
  AvatarManifest,
  ChannelDeps,
  ResolvedTouchEvent,
} from "../types";

function makeDeps(
  manifest: AvatarManifest,
  clock = new FakeClock(),
): ChannelDeps {
  return {
    now: clock.now,
    manifest,
    engineState: createEngineState(),
    getStoreSnapshot: () => buildStoreSnapshot(),
  };
}

function fullRig(): AvatarManifest {
  return buildManifest({
    capabilities: { has_body_angle_y: true, has_head_angle_y: true },
  });
}

function bodyOnlyRig(): AvatarManifest {
  return buildManifest({
    capabilities: { has_body_angle_y: true, has_head_angle_y: false },
  });
}

function emptyRig(): AvatarManifest {
  return buildManifest({
    capabilities: { has_body_angle_y: false, has_head_angle_y: false },
  });
}

function hug(clock: FakeClock, durationMs = 600): ResolvedTouchEvent {
  return {
    kind: "hug",
    label: "Aiko gave you a hug",
    emoji: "🫂",
    until: clock.now() + durationMs,
    leanAmount: 6,
    durationMs,
  };
}

describe("ReachChannel — lean-in animation", () => {
  it("writes both body and head params during the pulse", () => {
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(fullRig(), clock));

    channel.onTouch!(hug(clock, 600));
    expect(channel.isActive).toBe(true);

    // Mid-animation: peak hits at t=0.5 (sin(pi*0.5) = 1).
    channel.tickTier3!(clock.advance(300), 0.3);
    const bodyMid = adapter.params.get("ParamBodyAngleY") ?? 0;
    const headMid = adapter.params.get("ParamAngleY") ?? 0;
    expect(bodyMid).toBeCloseTo(6, 5); // leanAmount, full peak
    // HEAD_LEAN_RATIO = 0.6 in the channel.
    expect(headMid).toBeCloseTo(3.6, 5);
  });

  it("peaks near the midpoint of the window", () => {
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(fullRig(), clock));

    channel.onTouch!(hug(clock, 600));

    // Quarter-point: sin(pi*0.25) ≈ 0.7071, so body ≈ 6 * 0.7071.
    // We zero the baseline before each tick to simulate AmbientBody
    // resetting the param every frame (which it does in production).
    adapter.setParam("ParamBodyAngleY", 0);
    channel.tickTier3!(clock.advance(150), 0.15);
    const q1Body = adapter.params.get("ParamBodyAngleY") ?? 0;
    expect(q1Body).toBeGreaterThan(0);
    expect(q1Body).toBeLessThan(6); // not yet at peak

    // Midpoint: full peak.
    adapter.setParam("ParamBodyAngleY", 0);
    channel.tickTier3!(clock.advance(150), 0.15);
    const midBody = adapter.params.get("ParamBodyAngleY") ?? 0;
    expect(midBody).toBeCloseTo(6, 5);
  });

  it("composes additively on top of AmbientBody's pre-written value", () => {
    // AmbientBody writes first in the registration order. ReachChannel
    // must read-modify-write rather than overwrite.
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(fullRig(), clock));

    // Pretend AmbientBody just wrote a -2 valence-tilt on this frame.
    adapter.setParam("ParamBodyAngleY", -2);

    channel.onTouch!(hug(clock, 600));
    channel.tickTier3!(clock.advance(300), 0.3); // peak
    // Layered value: -2 (ambient) + 6 (reach peak) = 4.
    expect(adapter.params.get("ParamBodyAngleY")).toBeCloseTo(4, 5);
  });

  it("goes inactive once the deadline passes and stops writing", () => {
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(fullRig(), clock));

    channel.onTouch!(hug(clock, 200));
    channel.tickTier3!(clock.advance(100), 0.1); // alive
    channel.tickTier3!(clock.advance(250), 0.25); // expired (frame writes rest once)
    expect(channel.isActive).toBe(false);

    const callsAfterExpiry = adapter.setParamHistory.length;
    channel.tickTier3!(clock.advance(16), 0.016);
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.setParamHistory.length).toBe(callsAfterExpiry);
  });

  it("a fresh onTouch mid-animation restarts the timeline", () => {
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(fullRig(), clock));

    channel.onTouch!(hug(clock, 200));
    channel.tickTier3!(clock.advance(180), 0.18); // ~90% through

    // Restart with a fresh 600ms gesture.
    channel.onTouch!(hug(clock, 600));
    // Right after restart: t≈0, easing≈0 -> body delta ≈0.
    channel.tickTier3!(clock.advance(1), 0.001);
    const justRestarted = adapter.params.get("ParamBodyAngleY") ?? 0;
    // Channel writes baseline + 0 (=baseline) on the very first frame
    // post-restart. Since AmbientBody isn't writing in this test the
    // baseline is whatever the prior peak left behind; we only care
    // that the channel is active again.
    expect(channel.isActive).toBe(true);
    expect(justRestarted).not.toBeNaN();
  });

  it("does not write past ``until`` even with large dt jumps", () => {
    // Mimics a tab-throttled frame where dt is huge -- the channel
    // must release cleanly on the first post-deadline tick.
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(fullRig(), clock));

    channel.onTouch!(hug(clock, 200));
    channel.tickTier3!(clock.advance(5_000), 5); // way past deadline
    expect(channel.isActive).toBe(false);
  });
});

describe("ReachChannel — capability gating", () => {
  it("writes head only when has_head_angle_y is true", () => {
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(bodyOnlyRig(), clock));

    channel.onTouch!(hug(clock, 600));
    channel.tickTier3!(clock.advance(300), 0.3);

    expect(adapter.params.get("ParamBodyAngleY")).toBeCloseTo(6, 5);
    // No head write at all on a body-only rig.
    expect(adapter.params.has("ParamAngleY")).toBe(false);
  });

  it("bails early when neither body nor head angle exist", () => {
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(emptyRig(), clock));

    channel.onTouch!(hug(clock, 600));
    expect(channel.isActive).toBe(false);

    channel.tickTier3!(clock.advance(300), 0.3);
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("drops events whose deadline is already in the past", () => {
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(fullRig(), clock));

    // Force ``until`` to be the *current* clock value — durationMs
    // computed inside ``onTouch`` will be 0.
    channel.onTouch!({
      kind: "hug",
      label: "Aiko gave you a hug",
      emoji: "🫂",
      until: clock.now(),
      leanAmount: 6,
      durationMs: 0,
    });
    expect(channel.isActive).toBe(false);
  });
});

describe("ReachChannel — lifecycle", () => {
  it("detach clears the active slot", () => {
    const adapter = new FakeAdapter();
    const channel = new ReachChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(fullRig(), clock));

    channel.onTouch!(hug(clock, 600));
    expect(channel.isActive).toBe(true);
    channel.detach();
    expect(channel.isActive).toBe(false);
  });

  it("onTouch before attach is a no-op", () => {
    const channel = new ReachChannel();
    channel.onTouch!(hug(new FakeClock(0), 600));
    expect(channel.isActive).toBe(false);
  });
});
