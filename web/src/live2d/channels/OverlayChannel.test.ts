/**
 * OverlayChannel tests — these specifically lock in the regression
 * fixes that motivated the refactor:
 *
 *   - param-pulse expiry actually fires (the pre-fix bug was
 *     comparing wall-clock ``expiresAt`` against monotonic
 *     ``performance.now()``; the engine now converts at ingest, so
 *     this test passes in monotonic units directly)
 *
 *   - ``expr:`` overlays fire ``adapter.expression(name)`` exactly
 *     once and write ``engineState.exprSlotLockUntil`` so the engine
 *     can release the slot via ``onExpressionSlotReleased`` later
 *
 *   - gesture-named overlays (wink/tail_wag/ear_wiggle) are NOT
 *     handled here — they pass through silently for the gesture
 *     channel
 *
 *   - pulses with no manifest binding are silently dropped
 */
import { describe, expect, it } from "vitest";

import { OverlayChannel } from "./OverlayChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type { AvatarManifest, ChannelDeps } from "../types";

function makeDeps(
  manifest: AvatarManifest,
  clock = new FakeClock(),
  engineState = createEngineState(),
): ChannelDeps {
  return {
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
  };
}

function manifestWithOverlays(): AvatarManifest {
  return buildManifest({
    overlays: {
      blush: { param_id: "ParamBlush", on_value: 1.0, decay_ms: 1500, label_en: "Blush" },
      heart_eyes: {
        param_id: "ParamHeart",
        on_value: 1.0,
        decay_ms: 2000,
        label_en: "Heart eyes",
      },
      grin: { param_id: "expr:grin", on_value: 1, decay_ms: 1500, label_en: "Grin" },
    },
  });
}

describe("OverlayChannel — param pulses", () => {
  it("writes on_value while alive and 0 once on expiry", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));

    channel.onOverlay!({ name: "blush", until: clock.now() + 100 });
    expect(channel.pulseCount).toBe(1);

    // While alive — wrote on_value.
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.params.get("ParamBlush")).toBe(1.0);

    channel.tickTier3!(clock.advance(50), 0.05);
    expect(adapter.params.get("ParamBlush")).toBe(1.0);

    // Past the deadline — one decay write to 0.
    channel.tickTier3!(clock.advance(60), 0.06);
    expect(adapter.params.get("ParamBlush")).toBe(0);
    expect(channel.pulseCount).toBe(0);

    // Subsequent ticks do nothing.
    const calls = adapter.setParamHistory.length;
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.setParamHistory.length).toBe(calls);
  });

  it("supports concurrent pulses on different params", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));

    channel.onOverlay!({ name: "blush", until: clock.now() + 200 });
    channel.onOverlay!({ name: "heart_eyes", until: clock.now() + 200 });
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.params.get("ParamBlush")).toBe(1.0);
    expect(adapter.params.get("ParamHeart")).toBe(1.0);
    expect(channel.pulseCount).toBe(2);
  });

  it("a fresh onOverlay for the same name extends the deadline", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));

    channel.onOverlay!({ name: "blush", until: clock.now() + 50 });
    channel.tickTier3!(clock.advance(20), 0.02);
    expect(adapter.params.get("ParamBlush")).toBe(1.0);

    // Push the deadline further out.
    channel.onOverlay!({ name: "blush", until: clock.now() + 1_000 });
    channel.tickTier3!(clock.advance(40), 0.04); // 60ms past original deadline
    // Still alive because deadline is now 1s out.
    expect(adapter.params.get("ParamBlush")).toBe(1.0);
    expect(channel.pulseCount).toBe(1);
  });

  it("silently drops overlays missing from the manifest", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));
    channel.onOverlay!({ name: "ghost", until: clock.now() + 1_000 });
    expect(channel.pulseCount).toBe(0);
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("never handles gesture-named overlays (delegated to GestureChannel)", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));

    channel.onOverlay!({ name: "tail_wag", until: clock.now() + 1_000 });
    channel.onOverlay!({ name: "wink_left", until: clock.now() + 1_000 });
    channel.onOverlay!({ name: "ear_wiggle", until: clock.now() + 1_000 });
    expect(channel.pulseCount).toBe(0);
  });
});

describe("OverlayChannel — expression-bound pulses (regression: stuck grin)", () => {
  it("calls adapter.expression(name) exactly once per pulse", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));

    channel.onOverlay!({ name: "grin", until: clock.now() + 200 });

    // Simulate ~10 ticks worth of frames during the pulse lifetime.
    for (let i = 0; i < 10; i += 1) {
      channel.tickTier3!(clock.advance(16), 0.016);
    }
    expect(adapter.expressionCalls).toEqual(["grin"]);
  });

  it("writes engineState.exprSlotLockUntil so the engine can release later", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    const engineState = createEngineState();
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock, engineState));

    const deadline = clock.now() + 500;
    channel.onOverlay!({ name: "grin", until: deadline });
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(engineState.exprSlotLockUntil).toBe(deadline);
  });

  it("does not write any param value for expr-only overlays", () => {
    // Expression-only overlays bypass the param channel; the
    // expression slot is the only effect. Ensures we don't accidentally
    // write to a synthetic ``"expr:grin"`` param id.
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));

    channel.onOverlay!({ name: "grin", until: clock.now() + 200 });
    for (let i = 0; i < 5; i += 1) {
      channel.tickTier3!(clock.advance(16), 0.016);
    }
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("calls onExprSlotChange mirror with the same deadline (legacy bridge)", () => {
    const adapter = new FakeAdapter();
    const seen: number[] = [];
    const channel = new OverlayChannel({
      onExprSlotChange: (until) => seen.push(until),
    });
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));

    const deadline = clock.now() + 800;
    channel.onOverlay!({ name: "grin", until: deadline });
    // Several frames — the mirror only fires on the one that triggers
    // the expression() call (i.e. the first frame of the pulse).
    channel.tickTier3!(clock.advance(16), 0.016);
    channel.tickTier3!(clock.advance(16), 0.016);
    channel.tickTier3!(clock.advance(16), 0.016);
    expect(seen).toEqual([deadline]);
  });

  it("on expiry, an expression pulse does NOT write 0 (no param to clear)", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));

    channel.onOverlay!({ name: "grin", until: clock.now() + 50 });
    channel.tickTier3!(clock.advance(16), 0.016); // alive
    channel.tickTier3!(clock.advance(60), 0.06); // expired
    expect(adapter.setParamHistory).toHaveLength(0);
    expect(channel.pulseCount).toBe(0);
  });
});

describe("OverlayChannel — lifecycle", () => {
  it("detach() clears all in-flight pulses", () => {
    const adapter = new FakeAdapter();
    const channel = new OverlayChannel();
    const clock = new FakeClock(1_000);
    channel.attach(adapter, makeDeps(manifestWithOverlays(), clock));
    channel.onOverlay!({ name: "blush", until: clock.now() + 1_000 });
    expect(channel.pulseCount).toBe(1);
    channel.detach();
    expect(channel.pulseCount).toBe(0);
  });

  it("onOverlay is a no-op before attach", () => {
    const channel = new OverlayChannel();
    channel.onOverlay!({ name: "blush", until: 5 });
    expect(channel.pulseCount).toBe(0);
  });
});
