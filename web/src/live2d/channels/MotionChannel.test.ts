/**
 * Tests for MotionChannel:
 *   - LLM ``[[motion:X]]`` events fire ``adapter.motion(group, idx)``.
 *   - Unknown / missing groups are silently dropped (no Pixi exception
 *     bubbles up — matches the legacy useEffect's stance).
 *   - Talk motion fires ONCE per ``idle -> speaking`` transition,
 *     never on the reverse transition.
 *   - Capability gating: missing ``talk_motion_group`` means no talk
 *     motion ever fires.
 */
import { beforeEach, describe, expect, it } from "vitest";

import { MOTION_PRIORITY, MotionChannel } from "./MotionChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type { ChannelDeps } from "../types";

function makeDeps(manifest = buildManifest(), clock = new FakeClock()): ChannelDeps {
  return {
    now: clock.now,
    manifest,
    engineState: createEngineState(),
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

describe("MotionChannel — LLM motion dispatch", () => {
  let adapter: FakeAdapter;
  let channel: MotionChannel;

  beforeEach(() => {
    adapter = new FakeAdapter();
    channel = new MotionChannel();
  });

  it("forwards a known motion group/index to the adapter at NORMAL priority", () => {
    const manifest = buildManifest({
      motions: { Wave: [{ name: "wave", file: "wave.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({ name: "wave", group: "Wave", index: 0, firedAt: 100 });
    expect(adapter.motionCalls).toEqual([
      { group: "Wave", index: 0, priority: MOTION_PRIORITY.NORMAL },
    ]);
  });

  it("silently no-ops when the group is missing from the manifest", () => {
    const manifest = buildManifest({ motions: {} });
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({ name: "ghost", group: "Ghost", index: 0, firedAt: 1 });
    expect(adapter.motionCalls).toHaveLength(0);
  });

  it("silently no-ops when the group is the empty string", () => {
    const manifest = buildManifest({
      motions: { Wave: [{ name: "wave", file: "wave.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({ name: "wave", group: "", index: 0, firedAt: 1 });
    expect(adapter.motionCalls).toHaveLength(0);
  });

  it("dedupes by firedAt: the same firedAt does not fire twice", () => {
    const manifest = buildManifest({
      motions: { Wave: [{ name: "wave", file: "wave.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    const event = { name: "wave", group: "Wave", index: 0, firedAt: 42 };
    channel.onMotion!(event);
    channel.onMotion!(event); // exact same firedAt
    expect(adapter.motionCalls).toHaveLength(1);
  });

  it("fires again when a fresh firedAt arrives", () => {
    const manifest = buildManifest({
      motions: { Wave: [{ name: "wave", file: "wave.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({ name: "wave", group: "Wave", index: 0, firedAt: 1 });
    channel.onMotion!({ name: "wave", group: "Wave", index: 0, firedAt: 2 });
    expect(adapter.motionCalls).toHaveLength(2);
  });

  it("detach() resets dedup state so a re-attach restarts cleanly", () => {
    const manifest = buildManifest({
      motions: { Wave: [{ name: "wave", file: "wave.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({ name: "wave", group: "Wave", index: 0, firedAt: 5 });
    channel.detach();
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({ name: "wave", group: "Wave", index: 0, firedAt: 5 });
    expect(adapter.motionCalls).toHaveLength(2);
  });
});

describe("MotionChannel — talk-motion auto-start", () => {
  let adapter: FakeAdapter;
  let channel: MotionChannel;

  beforeEach(() => {
    adapter = new FakeAdapter();
    channel = new MotionChannel();
  });

  it("fires the talk-motion group on idle -> speaking transition", () => {
    const manifest = buildManifest({
      talk_motion_group: "Talk",
      motions: { Talk: [{ name: "talk1", file: "t.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onTtsState!("speaking");
    expect(adapter.motionCalls).toEqual([
      { group: "Talk", index: undefined, priority: MOTION_PRIORITY.NORMAL },
    ]);
  });

  it("does NOT fire on speaking -> idle transition", () => {
    const manifest = buildManifest({
      talk_motion_group: "Talk",
      motions: { Talk: [{ name: "talk1", file: "t.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onTtsState!("idle");
    expect(adapter.motionCalls).toHaveLength(0);
  });

  it("is gated by manifest.talk_motion_group (no group => no fire)", () => {
    const manifest = buildManifest({ talk_motion_group: null });
    channel.attach(adapter, makeDeps(manifest));
    channel.onTtsState!("speaking");
    expect(adapter.motionCalls).toHaveLength(0);
  });

  it("is gated by the group existing in manifest.motions", () => {
    const manifest = buildManifest({
      talk_motion_group: "TalkButMissing",
      motions: {},
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onTtsState!("speaking");
    expect(adapter.motionCalls).toHaveLength(0);
  });

  it("fires once per transition: speaking, speaking, speaking still triggers each call", () => {
    // The engine de-dupes idle/speaking transitions in
    // ``dispatchTtsState`` — this test makes the contract explicit:
    // if the engine somehow forwards two ``"speaking"`` calls in a
    // row, the channel itself does NOT internally dedupe (deferring
    // that responsibility to the engine keeps the channel logic
    // single-purpose).
    const manifest = buildManifest({
      talk_motion_group: "Talk",
      motions: { Talk: [{ name: "talk1", file: "t.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onTtsState!("speaking");
    channel.onTtsState!("speaking");
    expect(adapter.motionCalls).toHaveLength(2);
  });
});

describe("MotionChannel — priority lane routing (Phase B2)", () => {
  // Backchannel-driven motions ship with ``priority: "idle"`` so a
  // regular reaction motion fired during the same listening window
  // pre-empts them via pixi-live2d-display's MotionPriority.IDLE
  // contract. The channel translates the optional string lane into
  // the numeric priority the adapter consumes.
  let adapter: FakeAdapter;
  let channel: MotionChannel;

  beforeEach(() => {
    adapter = new FakeAdapter();
    channel = new MotionChannel();
  });

  it("routes priority='idle' to MOTION_PRIORITY.IDLE", () => {
    const manifest = buildManifest({
      motions: {
        Backchannel: [
          { name: "tilt_left", file: "tilt_left.motion3.json" },
        ],
      },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({
      name: "tilt_left",
      group: "Backchannel",
      index: 0,
      firedAt: 100,
      priority: "idle",
    });
    expect(adapter.motionCalls).toEqual([
      { group: "Backchannel", index: 0, priority: MOTION_PRIORITY.IDLE },
    ]);
  });

  it("routes priority='force' to MOTION_PRIORITY.FORCE", () => {
    const manifest = buildManifest({
      motions: { Tap: [{ name: "nod", file: "nod.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({
      name: "nod",
      group: "Tap",
      index: 0,
      firedAt: 1,
      priority: "force",
    });
    expect(adapter.motionCalls).toEqual([
      { group: "Tap", index: 0, priority: MOTION_PRIORITY.FORCE },
    ]);
  });

  it("treats priority='normal' identically to a missing field", () => {
    const manifest = buildManifest({
      motions: { Tap: [{ name: "nod", file: "nod.motion3.json" }] },
    });
    channel.attach(adapter, makeDeps(manifest));
    channel.onMotion!({
      name: "nod",
      group: "Tap",
      index: 0,
      firedAt: 1,
      priority: "normal",
    });
    channel.onMotion!({
      name: "nod",
      group: "Tap",
      index: 0,
      firedAt: 2,
    });
    expect(adapter.motionCalls).toEqual([
      { group: "Tap", index: 0, priority: MOTION_PRIORITY.NORMAL },
      { group: "Tap", index: 0, priority: MOTION_PRIORITY.NORMAL },
    ]);
  });
});
