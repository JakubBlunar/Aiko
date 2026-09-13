/**
 * GazeChannel tests.
 *
 * Behaviour matrix:
 *
 *   - **Cursor follow** (default): writes ``adapter.focus`` with a
 *     normalised + clamped cursor position.
 *   - **Conversation lock**: ``listening`` / ``transcribing`` /
 *     ``speaking`` ignore the cursor and lock to ``(0, 0.2)``.
 *   - **Thinking**: a slow wander unrelated to the cursor.
 *   - **Idle break**: window blur OR cursor stale > 1500ms decays the
 *     target back toward centre.
 *   - **Saccade**: micro-jitter fires at the configured interval and
 *     decays each frame.
 *   - **Capability**: a rig with no focusController still receives
 *     focus calls (the adapter swallows them) — channel is
 *     unconditional.
 */
import { describe, expect, it } from "vitest";

import { GazeChannel } from "./GazeChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type { ChannelDeps, ChannelStoreSnapshot, MouseSnapshot } from "../types";

interface DepsBundle {
  deps: ChannelDeps;
  clock: FakeClock;
  setSnapshot: (next: Partial<ChannelStoreSnapshot>) => void;
}

function makeDeps(initial: Partial<ChannelStoreSnapshot> = {}): DepsBundle {
  const clock = new FakeClock(1_000);
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
    ...initial,
  };
  return {
    clock,
    setSnapshot: (next) => {
      snap = { ...snap, ...next };
    },
    deps: {
      now: clock.now,
      manifest: buildManifest(),
      engineState: createEngineState(),
      getStoreSnapshot: () => snap,
    },
  };
}

const baseRect = { left: 100, top: 100, width: 400, height: 600 };

function mouseAt(
  x: number,
  y: number,
  lastMoveAt: number,
  overrides: Partial<MouseSnapshot> = {},
): MouseSnapshot {
  return {
    x,
    y,
    lastMoveAt,
    windowFocused: true,
    containerRect: baseRect,
    viewportWidth: 1600,
    viewportHeight: 900,
    ...overrides,
  };
}

const noopRandom = () => 0.5;

describe("GazeChannel — cursor follow", () => {
  it("normalises cursor offset against half-viewport and clamps to comfort range", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps();
    channel.attach(adapter, deps);

    // Cursor at the right edge of the viewport — with viewport=1600
    // and centre at containerRect midpoint (300), the offset is 1300/800
    // which exceeds the 0.7 clamp.
    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, clock.now()));
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(last.x).toBeCloseTo(0.7, 5);
  });

  it("flips Y so cursor below the centre yields negative Y in gaze space", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps();
    channel.attach(adapter, deps);

    // Cursor below the container centre (cy = 100 + 300 = 400). Far
    // below means negative gaze Y (since the channel flips screen Y).
    channel.tickGaze!(clock.now(), 0.016, mouseAt(300, 800, clock.now()));
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(last.y).toBeLessThan(0);
  });

  it("holds previous target when cursor data hasn't arrived yet", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps();
    channel.attach(adapter, deps);

    channel.tickGaze!(clock.now(), 0.016, {
      x: null,
      y: null,
      lastMoveAt: 0,
      windowFocused: true,
      containerRect: baseRect,
      viewportWidth: 1600,
      viewportHeight: 900,
    });
    expect(adapter.focusCalls.length).toBe(1);
    const last = adapter.focusCalls[0];
    expect(Math.abs(last.x)).toBeLessThan(0.1);
    expect(Math.abs(last.y)).toBeLessThan(0.1);
  });
});

describe("GazeChannel — conversation lock", () => {
  it("locks to (0, 0.2) while listening regardless of cursor", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps({ voiceMode: "listening" });
    channel.attach(adapter, deps);

    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, clock.now()));
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    // Saccade adds (0, 0)*0.92 since random=0.5 → centred (with decay).
    expect(Math.abs(last.x)).toBeLessThan(0.05);
    expect(last.y).toBeGreaterThan(0.15);
    expect(last.y).toBeLessThan(0.25);
  });

  it("locks while speaking too", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps({ ttsState: "speaking" });
    channel.attach(adapter, deps);
    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, clock.now()));
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(last.y).toBeGreaterThan(0.15);
  });
});

describe("GazeChannel — typed-listening (B8)", () => {
  it("settles gaze on the user (eye-contact Y, small X) while composing, ignoring the cursor", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps({ voiceMode: "off", composing: true });
    channel.attach(adapter, deps);

    // Cursor parked at the far edge — composing must override cursor follow.
    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 800, clock.now()));
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    // Sway X is tiny (<= 0.08); Y holds the upward eye-contact bias.
    expect(Math.abs(last.x)).toBeLessThan(0.12);
    expect(last.y).toBeGreaterThan(0.12);
    expect(last.y).toBeLessThan(0.3);
  });

  it("outranks thinking drift so typing wins over a wander", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps({
      voiceMode: "off",
      composing: true,
      turnInProgress: true,
    });
    channel.attach(adapter, deps);
    const xs: number[] = [];
    for (let i = 0; i < 60; i += 1) {
      clock.advance(50);
      channel.tickGaze!(clock.now(), 0.05, mouseAt(1600, 100, clock.now()));
      xs.push(adapter.focusCalls[adapter.focusCalls.length - 1].x);
    }
    // Thinking drift would sweep to ~0.35; composing sway is capped near 0.08.
    expect(Math.max(...xs.map(Math.abs))).toBeLessThan(0.15);
  });
});

describe("GazeChannel — idle break", () => {
  it("decays the target toward 0 when the window has lost focus", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps();
    channel.attach(adapter, deps);

    // Frame 1 — cursor follow lifts the target.
    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, clock.now()));
    const firstX = adapter.focusCalls[0].x;
    expect(Math.abs(firstX)).toBeGreaterThan(0.5);

    // Frame 2+ — window blurs, target decays toward 0.
    for (let i = 0; i < 100; i += 1) {
      clock.advance(16);
      channel.tickGaze!(
        clock.now(),
        0.016,
        mouseAt(1600, 100, 0, { windowFocused: false }),
      );
    }
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(Math.abs(last.x)).toBeLessThan(0.01);
  });

  it("decays when the cursor hasn't moved for >1500ms even if focused", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps();
    channel.attach(adapter, deps);

    const moveAt = clock.now();
    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, moveAt));
    const firstX = adapter.focusCalls[0].x;
    expect(Math.abs(firstX)).toBeGreaterThan(0.5);

    // Advance well past the idle threshold and let decay take hold.
    for (let i = 0; i < 200; i += 1) {
      clock.advance(20);
      channel.tickGaze!(clock.now(), 0.02, mouseAt(1600, 100, moveAt));
    }
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(Math.abs(last.x)).toBeLessThan(0.001);
  });
});

describe("GazeChannel — thinking drift", () => {
  it("uses sin/cos drift instead of cursor-derived target", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps({
      voiceMode: "off",
      turnInProgress: true,
      ttsState: "idle",
    });
    channel.attach(adapter, deps);

    const xs: number[] = [];
    for (let i = 0; i < 60; i += 1) {
      clock.advance(50);
      channel.tickGaze!(clock.now(), 0.05, mouseAt(1600, 100, clock.now()));
      xs.push(adapter.focusCalls[adapter.focusCalls.length - 1].x);
    }
    // Drift is *not* clamped to the 0.7 cursor cap; sweep is bounded
    // by the sin amplitude (0.35).
    const max = Math.max(...xs.map(Math.abs));
    expect(max).toBeLessThan(0.4);
    expect(max).toBeGreaterThan(0.2);
  });
});

describe("GazeChannel — asleep (no cursor follow, resting droop)", () => {
  it("does not track the cursor while asleep and eases the head to the rest pose", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps({ sleepStatus: "asleep" });
    channel.attach(adapter, deps);

    // The cursor is parked hard at the right edge — cursor-follow would
    // lift the gaze to ~+0.7 X. Asleep, the head must instead ease to the
    // resting droop (x -> 0, y -> negative).
    for (let i = 0; i < 120; i += 1) {
      clock.advance(16);
      channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, clock.now()));
    }
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(Math.abs(last.x)).toBeLessThan(0.05);
    expect(last.y).toBeLessThan(0);
    expect(last.y).toBeGreaterThanOrEqual(-0.25);
  });

  it("suppresses saccades while asleep", () => {
    let randomCalls = 0;
    const random = () => {
      randomCalls += 1;
      return 0.5;
    };
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random });
    const { deps, clock } = makeDeps({ sleepStatus: "asleep" });
    channel.attach(adapter, deps);
    // attach() rolls the initial saccade interval exactly once.
    expect(randomCalls).toBe(1);
    // Run well past several saccade intervals. While asleep no new
    // interval roll or saccade offset may fire, so the counter stays put.
    for (let i = 0; i < 200; i += 1) {
      clock.advance(16);
      channel.tickGaze!(clock.now(), 0.016, mouseAt(0, 0, clock.now()));
    }
    expect(randomCalls).toBe(1);
  });

  it("resumes cursor follow after waking", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const bundle = makeDeps({ sleepStatus: "asleep" });
    channel.attach(adapter, bundle.deps);
    // Asleep: cursor parked hard at the right edge, but the head still
    // eases to the resting droop (no cursor follow).
    for (let i = 0; i < 60; i += 1) {
      bundle.clock.advance(16);
      channel.tickGaze!(bundle.clock.now(), 0.016, mouseAt(1600, 100, bundle.clock.now()));
    }
    expect(Math.abs(adapter.focusCalls[adapter.focusCalls.length - 1].x)).toBeLessThan(0.05);
    // Wake her: cursor follow should now lift the gaze to the right edge.
    bundle.setSnapshot({ sleepStatus: "awake" });
    for (let i = 0; i < 120; i += 1) {
      bundle.clock.advance(16);
      channel.tickGaze!(bundle.clock.now(), 0.016, mouseAt(1600, 100, bundle.clock.now()));
    }
    expect(adapter.focusCalls[adapter.focusCalls.length - 1].x).toBeGreaterThan(0.5);
  });
});

describe("GazeChannel — saccades", () => {
  it("re-rolls the saccade when the interval elapses, then decays each frame", () => {
    let calls = 0;
    const random = () => {
      calls += 1;
      // First two calls roll the next interval; subsequent calls
      // produce the saccade offsets.
      if (calls === 1) return 0; // initial interval = 1500ms
      if (calls === 2) return 0; // re-roll next interval = 1500ms
      return 1; // saccade offset = +0.05 / +0.03
    };
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random });
    const { deps, clock } = makeDeps();
    channel.attach(adapter, deps);

    // Tick at 16ms steps until past the first saccade interval.
    for (let i = 0; i < 100; i += 1) {
      clock.advance(16);
      channel.tickGaze!(clock.now(), 0.016, mouseAt(0, 0, clock.now()));
    }
    // The most recent focus.x should reflect a non-zero saccade
    // contribution (decayed from +0.05 + clamped target at ~0).
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(Math.abs(last.x)).toBeGreaterThan(0);
  });
});

describe("GazeChannel — lifecycle", () => {
  it("detach() clears state so a re-attach starts at (0, 0)", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps();
    channel.attach(adapter, deps);
    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, clock.now()));
    channel.detach();
    expect(channel.target).toEqual({ x: 0, y: 0 });

    channel.attach(adapter, deps);
    expect(channel.target).toEqual({ x: 0, y: 0 });
  });

  it("tickGaze before attach is a no-op (no exception)", () => {
    const channel = new GazeChannel();
    const fakeMouse = mouseAt(0, 0, 0);
    expect(() => channel.tickGaze!(0, 0, fakeMouse)).not.toThrow();
  });
});

describe("GazeChannel — held Live gaze", () => {
  it("ignores cursor jitter during a shared-activity hold", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps({
      liveAttentionTarget: "shared_activity",
      liveEmbodiment: {
        intent: "remain_present",
        attention_target: "shared_activity",
        gaze_class: "rest",
        body_class: "settle",
        breath_class: "slow",
        expression_class: "content",
        motion_class: "none",
        degrade_to_sleep: false,
        hold_attention: true,
      },
    });
    channel.attach(adapter, deps);
    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, clock.now()));
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(Math.abs(last.x)).toBeLessThan(0.2);
  });

  it("conversation lock still outranks a held Live gaze", () => {
    const adapter = new FakeAdapter();
    const channel = new GazeChannel({ random: noopRandom });
    const { deps, clock } = makeDeps({
      voiceMode: "listening",
      liveAttentionTarget: "shared_activity",
      liveEmbodiment: {
        intent: "remain_present",
        attention_target: "shared_activity",
        gaze_class: "rest",
        body_class: "settle",
        breath_class: "slow",
        expression_class: "none",
        motion_class: "none",
        degrade_to_sleep: false,
      },
    });
    channel.attach(adapter, deps);
    channel.tickGaze!(clock.now(), 0.016, mouseAt(1600, 100, clock.now()));
    const last = adapter.focusCalls[adapter.focusCalls.length - 1];
    expect(last.x).toBeCloseTo(0, 5);
    expect(last.y).toBeCloseTo(0.2, 5);
  });
});
