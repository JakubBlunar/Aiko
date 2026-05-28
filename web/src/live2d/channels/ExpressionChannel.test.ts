/**
 * ExpressionChannel tests.
 *
 * Behaviour matrix:
 *
 *   - Reaction priority: ``onReaction`` writes ``adapter.expression(name)``
 *     when the slot is free.
 *   - Slot lock: ``engineState.exprSlotLockUntil > now`` defers writes;
 *     ``onExpressionSlotReleased`` flushes them.
 *   - Voice mode: listening/thinking override the persistent reaction
 *     (highest priority while a real expression slot lock isn't active).
 *   - Backchannel: applies a transient expression and restores after
 *     the 1.8s window. Newer hints cancel the previous restore.
 *   - Empty reaction: ``adapter.resetExpression`` is called instead of
 *     leaving the previous expression on the rig (the regression-fix).
 */
import { describe, expect, it, vi } from "vitest";

import { ExpressionChannel, _REACTION_NEIGHBOURS } from "./ExpressionChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest } from "../__fixtures__/test-manifest";
import { createEngineState, type EngineState } from "../state";
import type { ChannelDeps, ChannelStoreSnapshot } from "../types";

interface ManualTimer {
  schedule: (cb: () => void, ms: number) => unknown;
  cancel: (handle: unknown) => void;
  /** Run the scheduled callback if one is pending. */
  flush(): void;
  pending(): boolean;
}

function makeManualTimer(): ManualTimer {
  let entry: { id: number; cb: () => void } | null = null;
  let nextId = 1;
  return {
    schedule: (cb) => {
      const id = nextId++;
      entry = { id, cb };
      return id;
    },
    cancel: (handle) => {
      if (entry && entry.id === handle) {
        entry = null;
      }
    },
    flush: () => {
      if (!entry) {
        return;
      }
      const cb = entry.cb;
      entry = null;
      cb();
    },
    pending: () => entry !== null,
  };
}

interface DepsBundle {
  deps: ChannelDeps;
  clock: FakeClock;
  engineState: EngineState;
  setSnapshot: (next: Partial<ChannelStoreSnapshot>) => void;
}

function makeDeps(initial: Partial<ChannelStoreSnapshot> = {}): DepsBundle {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  let snapshot: ChannelStoreSnapshot = {
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
  const manifest = buildManifest({
    reaction_mapping: {
      neutral: "ExprNeutral",
      cheerful: "ExprCheerful",
      sad: "ExprSad",
      thoughtful: "ExprThoughtful",
      surprised: "ExprSurprised",
    },
    expressions: [
      { name: "ExprNeutral", file: "n.exp3.json" },
      { name: "ExprCheerful", file: "c.exp3.json" },
      { name: "ExprSad", file: "s.exp3.json" },
      { name: "ExprThoughtful", file: "t.exp3.json" },
      { name: "ExprSurprised", file: "su.exp3.json" },
    ],
  });
  return {
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    },
    clock,
    engineState,
    setSnapshot: (next) => {
      snapshot = { ...snapshot, ...next };
    },
  };
}

describe("ExpressionChannel — attach + initial reaction", () => {
  it("applies the initial reaction expression on attach", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "cheerful" });
    channel.attach(adapter, deps);
    expect(adapter.expressionCalls).toEqual(["ExprCheerful"]);
  });

  it("calls adapter.resetExpression when the initial reaction is empty", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "" });
    channel.attach(adapter, deps);
    expect(adapter.expressionCalls).toEqual([]);
    expect(adapter.resetExpressionCount).toBe(1);
  });

  it("calls adapter.resetExpression when the reaction has no mapping at all", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "xyz_unmapped" });
    channel.attach(adapter, deps);
    expect(adapter.resetExpressionCount).toBe(1);
  });
});

describe("ExpressionChannel — onReaction", () => {
  it("writes adapter.expression for the new reaction when slot is free", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onReaction!("cheerful");
    expect(adapter.expressionCalls).toEqual(["ExprCheerful"]);
  });

  it("defers writes while exprSlotLockUntil > now (overlay owns the slot)", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    engineState.exprSlotLockUntil = clock.now() + 500;
    channel.onReaction!("sad");
    expect(adapter.expressionCalls).toEqual([]);

    // Engine notifies us when the deadline passes.
    channel.onExpressionSlotReleased!();
    expect(adapter.expressionCalls).toEqual(["ExprSad"]);
  });

  it("re-applies via reset when the deferred reaction has empty mapping", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;
    adapter.resetExpressionCount = 0;

    engineState.exprSlotLockUntil = clock.now() + 500;
    channel.onReaction!("");
    expect(adapter.resetExpressionCount).toBe(0);
    channel.onExpressionSlotReleased!();
    expect(adapter.resetExpressionCount).toBe(1);
  });
});

describe("ExpressionChannel — voice mode override", () => {
  it("applies the listening expression on voiceMode listening", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onVoiceMode!("listening");
    expect(adapter.expressionCalls).toEqual(["ExprThoughtful"]);
  });

  it("applies the thinking expression on voiceMode thinking", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onVoiceMode!("thinking");
    expect(adapter.expressionCalls).toEqual(["ExprThoughtful"]);
  });

  it("restores the persistent reaction when leaving listening/thinking", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "cheerful" });
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onVoiceMode!("listening");
    channel.onVoiceMode!("off");
    expect(adapter.expressionCalls).toEqual(["ExprThoughtful", "ExprCheerful"]);
  });

  it("ignores reaction changes while voiceMode is listening (mode wins)", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    channel.onVoiceMode!("listening");
    adapter.expressionCalls.length = 0;

    channel.onReaction!("cheerful");
    // No new expression call — voice mode owns the slot.
    expect(adapter.expressionCalls).toEqual([]);

    // ...but when the mode leaves, the *latest* reaction is applied.
    channel.onVoiceMode!("off");
    expect(adapter.expressionCalls).toEqual(["ExprCheerful"]);
  });
});

describe("ExpressionChannel — backchannel hints", () => {
  it("applies the backchannel expression and arms a restore timer", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps } = makeDeps({ reaction: "neutral" });
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onBackchannel!("agreement");
    expect(adapter.expressionCalls).toEqual(["ExprCheerful"]);
    expect(channel.backchannelRestoreArmed).toBe(true);
    expect(timer.pending()).toBe(true);

    // Restore fires.
    timer.flush();
    expect(adapter.expressionCalls).toEqual(["ExprCheerful", "ExprNeutral"]);
  });

  it("a fresh backchannel cancels the prior restore timer", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps } = makeDeps({ reaction: "neutral" });
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onBackchannel!("agreement");
    expect(timer.pending()).toBe(true);
    channel.onBackchannel!("surprise");
    // Prior timer was cancelled; new one armed.
    expect(timer.pending()).toBe(true);
    expect(adapter.expressionCalls).toEqual(["ExprCheerful", "ExprSurprised"]);
  });

  it("ignores backchannel hints while exprSlotLockUntil > now", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps, engineState, clock } = makeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    engineState.exprSlotLockUntil = clock.now() + 1_000;
    channel.onBackchannel!("agreement");
    expect(adapter.expressionCalls).toEqual([]);
    expect(timer.pending()).toBe(false);
  });
});

describe("ExpressionChannel — auto-cascade avoids heavy expressions", () => {
  // Regression guard for the "cheerful turn, Aiko visibly cried" bug.
  //
  // The thinking / backchannel cascades walk a list of candidate
  // reactions until one is mapped on the rig. Alexia's
  // ``reaction_mapping["thoughtful"] = ""`` (empty), so the cascade
  // used to fall through to ``concerned`` — which on Alexia maps to
  // ``k`` (Param59 tear streaks). The 2-4s tool-call window with
  // voice mode = ``thinking`` then painted tears on a perfectly
  // cheerful turn. Same for the ``concern`` backchannel.
  //
  // Fix: drop ``concerned`` / ``sad`` from the auto-cascade candidate
  // lists. Explicit ``[[reaction:concerned]]`` from the LLM still
  // resolves to the rig's mapping (intentional empathy beat).
  function makeAlexiaLikeDeps(
    initialSnap: Partial<ChannelStoreSnapshot> = {},
  ): DepsBundle {
    const clock = new FakeClock(1_000);
    const engineState = createEngineState();
    let snapshot: ChannelStoreSnapshot = {
      reaction: "cheerful",
      ttsState: "idle",
      voiceMode: "off",
      turnInProgress: false,
      audioAmplitude: 0,
      avatarOverlay: null,
      avatarMotion: null,
      mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.4 },
      resolvedOutfit: "",
      backchannelHint: "",
      ...initialSnap,
    };
    // Mirror Alexia's reaction_mapping shape: ``concerned`` /
    // ``sad`` route to ExprCry, ``thoughtful`` is unmapped, so the
    // pre-fix cascade would cry; the post-fix cascade must not.
    const manifest = buildManifest({
      reaction_mapping: {
        cheerful: "ExprGrin",
        concerned: "ExprCry",
        sad: "ExprCry",
        calm: "",
        thoughtful: "",
        gentle: "ExprBlush",
        warm: "ExprBlush",
        tender: "ExprBlush",
        serious: "",
        neutral: "",
      },
      expressions: [
        { name: "ExprGrin", file: "g.exp3.json" },
        { name: "ExprCry", file: "k.exp3.json" },
        { name: "ExprBlush", file: "lh.exp3.json" },
      ],
    });
    return {
      deps: {
        now: clock.now,
        manifest,
        engineState,
        getStoreSnapshot: () => snapshot,
      },
      clock,
      engineState,
      setSnapshot: (next) => {
        snapshot = { ...snapshot, ...next };
      },
    };
  }

  it("thinking voice mode never falls through to a cry-mapped reaction", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeAlexiaLikeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onVoiceMode!("thinking");
    // The cascade walked through ``thoughtful`` (empty), then must
    // skip ``concerned`` (the bug), and either land on a soft option
    // or no-op. The post-fix candidate list
    // ``["thoughtful", "calm", "serious", "neutral"]`` has nothing
    // mapped on this rig — expected: no expression write at all.
    // Crucially, ``ExprCry`` MUST NOT appear.
    expect(adapter.expressionCalls).not.toContain("ExprCry");
  });

  it("concern backchannel routes to soft warmth, not tears", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps } = makeAlexiaLikeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onBackchannel!("concern");
    // Post-fix candidate list is ``["gentle", "tender", "warm",
    // "thoughtful"]`` — first hit on this rig is ``gentle`` →
    // ``ExprBlush``. The cry expression MUST NOT appear.
    expect(adapter.expressionCalls).toEqual(["ExprBlush"]);
    expect(adapter.expressionCalls).not.toContain("ExprCry");
  });

  it("disagreement backchannel never falls through to a cry-mapped reaction", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps } = makeAlexiaLikeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onBackchannel!("disagreement");
    // Post-fix candidates ``["serious", "thoughtful", "neutral"]`` —
    // none mapped on this rig, expected no write. Importantly, no
    // ``ExprCry`` write.
    expect(adapter.expressionCalls).not.toContain("ExprCry");
  });

  it("explicit [[reaction:concerned]] still resolves to the rig's mapping", () => {
    // The fix MUST NOT block the LLM's intentional concerned beat.
    // When a turn explicitly emits ``[[reaction:concerned]]`` the
    // channel still applies ``manifest.reaction_mapping["concerned"]``
    // (Alexia's ``k`` cry). That's a deliberate narrative choice and
    // stays untouched — only the AUTO-cascades are filtered.
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeAlexiaLikeDeps();
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;

    channel.onReaction!("concerned");
    expect(adapter.expressionCalls).toEqual(["ExprCry"]);
  });
});

describe("ExpressionChannel — onExpressionSlotReleased", () => {
  it("re-applies whatever the current target is (regression: stuck grin)", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeDeps({ reaction: "neutral" });
    channel.attach(adapter, deps);
    adapter.expressionCalls.length = 0;
    adapter.resetExpressionCount = 0;

    // Overlay grabbed the slot mid-turn (e.g. ``[[overlay:grin]]``).
    engineState.exprSlotLockUntil = clock.now() + 500;
    // Reaction stayed neutral throughout the overlay; engine fans
    // ``onExpressionSlotReleased`` when the deadline passes. We
    // should re-apply the persistent reaction (or reset, in the
    // empty-mapping case).
    channel.onExpressionSlotReleased!();
    expect(adapter.resetExpressionCount + adapter.expressionCalls.length).toBe(1);
  });
});

describe("ExpressionChannel — lifecycle", () => {
  it("detach() cancels any pending restore timer", () => {
    const adapter = new FakeAdapter();
    const timer = makeManualTimer();
    const channel = new ExpressionChannel({
      schedule: timer.schedule,
      cancel: timer.cancel,
    });
    const { deps } = makeDeps();
    channel.attach(adapter, deps);
    channel.onBackchannel!("agreement");
    expect(timer.pending()).toBe(true);
    channel.detach();
    expect(timer.pending()).toBe(false);
    expect(channel.backchannelRestoreArmed).toBe(false);
  });
});

/** Build a manifest with a ``cheerful`` reaction mapped to the
 * ``lzx`` expression and a single ``Param54: 30`` binding — the
 * smallest shape that exercises the arousal-scale write path. */
function makeAmplitudeManifestDeps(
  initialSnap: Partial<ChannelStoreSnapshot> = {},
) {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  let snapshot: ChannelStoreSnapshot = {
    reaction: "cheerful",
    ttsState: "idle",
    voiceMode: "off",
    turnInProgress: false,
    audioAmplitude: 0,
    avatarOverlay: null,
    avatarMotion: null,
    mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 },
    resolvedOutfit: "",
    backchannelHint: "",
    expressiveness: 1,
    ...initialSnap,
  };
  const manifest = buildManifest({
    reaction_mapping: { cheerful: "lzx", neutral: "n" },
    expressions: [
      { name: "lzx", file: "lzx.exp3.json" },
      { name: "n", file: "n.exp3.json" },
    ],
    expression_params: {
      lzx: [{ param_id: "Param54", on_value: 30 }],
    },
  });
  return {
    clock,
    engineState,
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    },
    setSnapshot: (next: Partial<ChannelStoreSnapshot>) => {
      snapshot = { ...snapshot, ...next };
    },
  };
}

/** Run ``tickPreModel`` ``frames`` times with ``dt`` seconds between
 * ticks. Returns the final ``Param54`` value once the smoothing has
 * had time to converge. */
function runPreModel(
  channel: ExpressionChannel,
  adapter: FakeAdapter,
  clock: FakeClock,
  frames: number,
  dt: number,
): number {
  for (let i = 0; i < frames; i += 1) {
    clock.advance(dt * 1000);
    channel.tickPreModel!();
  }
  return adapter.params.get("Param54") ?? 0;
}

describe("ExpressionChannel — tickPreModel arousal scaling", () => {
  it("low arousal writes a quieter param value", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.1 },
    });
    channel.attach(adapter, deps);
    const value = runPreModel(channel, adapter, clock, 240, 1 / 60);
    // arousal=0.1 -> scale ~0.46; expressiveness=1 -> ~30 * 0.46 = 13.8.
    expect(value).toBeGreaterThan(11);
    expect(value).toBeLessThan(17);
  });

  it("high arousal writes close to the rig's authored on_value", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    const value = runPreModel(channel, adapter, clock, 240, 1 / 60);
    // arousal=0.9 -> scale ~0.94; expressiveness=1 -> ~28.2.
    expect(value).toBeGreaterThan(25);
    expect(value).toBeLessThanOrEqual(30);
  });

  it("expressiveness 0 mutes the write to zero", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      expressiveness: 0,
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    const value = runPreModel(channel, adapter, clock, 240, 1 / 60);
    expect(Math.abs(value)).toBeLessThan(0.01);
  });

  it("expressiveness 1.5 amplifies the write but caps at the on_value", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      expressiveness: 1.5,
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    const value = runPreModel(channel, adapter, clock, 240, 1 / 60);
    // arousal scale capped at 1.0; expressiveness 1.5 -> 30 * 1 * 1.5 = 45.
    // We document this as "capped by the rig's natural on_value" but
    // the actual write doesn't auto-clamp — pixi-live2d-display will
    // clamp at the param's authored max. We still want the test to
    // assert proportional scaling above the default-1.0 case.
    const adapterDefault = new FakeAdapter();
    const channelDefault = new ExpressionChannel();
    const def = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channelDefault.attach(adapterDefault, def.deps);
    const defaultValue = runPreModel(
      channelDefault,
      adapterDefault,
      def.clock,
      240,
      1 / 60,
    );
    expect(value).toBeGreaterThan(defaultValue * 1.4);
  });

  it("does not write while exprSlotLockUntil is in the future", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    // Reset our smoothed amplitude back to zero by holding the slot
    // lock, then ticking. The tickPreModel must skip the write so
    // Param54 stays at whatever the FakeAdapter has (untouched).
    engineState.exprSlotLockUntil = clock.now() + 5_000;
    for (let i = 0; i < 120; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(adapter.params.has("Param54")).toBe(false);
    expect(channel.amplitudeScale).toBe(0);
  });

  it("resumes writing after the slot lock expires", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, engineState, clock } = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    engineState.exprSlotLockUntil = clock.now() + 1_000;
    for (let i = 0; i < 30; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(adapter.params.has("Param54")).toBe(false);

    // Drop the lock; tickPreModel must resume writing.
    engineState.exprSlotLockUntil = 0;
    for (let i = 0; i < 240; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    const value = adapter.params.get("Param54") ?? 0;
    expect(value).toBeGreaterThan(20);
  });

  it("does nothing when the active expression has no expression_params binding", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    // Manifest with no expression_params for the active expression —
    // legacy / minimal rig path.
    const clock = new FakeClock(1_000);
    const engineState = createEngineState();
    const snapshot: ChannelStoreSnapshot = {
      reaction: "cheerful",
      ttsState: "idle",
      voiceMode: "off",
      turnInProgress: false,
      audioAmplitude: 0,
      avatarOverlay: null,
      avatarMotion: null,
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
      resolvedOutfit: "",
      backchannelHint: "",
      expressiveness: 1,
    };
    const manifest = buildManifest({
      reaction_mapping: { cheerful: "lzx" },
      expressions: [{ name: "lzx", file: "lzx.exp3.json" }],
      // No expression_params at all.
    });
    const deps: ChannelDeps = {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    };
    channel.attach(adapter, deps);
    for (let i = 0; i < 60; i += 1) {
      clock.advance(1000 / 60);
      channel.tickPreModel!();
    }
    expect(adapter.params.has("Param54")).toBe(false);
  });

  it("smooths amplitude across an arousal flip instead of snapping", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock, setSnapshot } = makeAmplitudeManifestDeps({
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const settled = adapter.params.get("Param54") ?? 0;

    setSnapshot({
      mood: { label: "calm", intensity: 0.4, valence: 0, arousal: 0.05 },
    });
    // A single tick after a flip cannot snap the amplitude all the
    // way down to the new target. The smoothing must keep us above
    // the eventual converged low-arousal value for at least one
    // frame.
    clock.advance(1000 / 60);
    channel.tickPreModel!();
    const oneTickAfter = adapter.params.get("Param54") ?? 0;
    expect(oneTickAfter).toBeLessThan(settled);
    expect(oneTickAfter).toBeGreaterThan(11);
  });
});

/** Variant of ``makeAmplitudeManifestDeps`` that registers
 * ``Param54`` as a mouth-overlay so the lip-sync suppression branch
 * activates. Mirrors the Alexia rig where ``lzx`` paints a static
 * toothy grin via ``Param54`` independent of the lip-synced jaw. */
function makeMouthOverlayDeps(initialSnap: Partial<ChannelStoreSnapshot> = {}) {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  let snapshot: ChannelStoreSnapshot = {
    reaction: "cheerful",
    ttsState: "idle",
    voiceMode: "off",
    turnInProgress: false,
    audioAmplitude: 0,
    avatarOverlay: null,
    avatarMotion: null,
    mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    resolvedOutfit: "",
    backchannelHint: "",
    expressiveness: 1,
    ...initialSnap,
  };
  const manifest = buildManifest({
    reaction_mapping: { cheerful: "lzx", neutral: "n" },
    expressions: [
      { name: "lzx", file: "lzx.exp3.json" },
      { name: "n", file: "n.exp3.json" },
    ],
    expression_params: {
      // Realistic lzx shape: the grin overlay on Param54, plus a
      // companion non-mouth param (eye squint, hypothetical Param80)
      // so we can prove the suppression is targeted — non-mouth
      // bindings on the SAME expression must keep their amplitude.
      lzx: [
        { param_id: "Param54", on_value: 30 },
        { param_id: "Param80", on_value: 30 },
      ],
    },
    mouth_overlay_param_ids: ["Param54"],
  });
  return {
    clock,
    engineState,
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    },
    setSnapshot: (next: Partial<ChannelStoreSnapshot>) => {
      snapshot = { ...snapshot, ...next };
    },
  };
}

describe("ExpressionChannel — mouth-overlay lip-sync suppression", () => {
  it("silent audio writes the grin param at the full arousal-scaled value", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeMouthOverlayDeps({ audioAmplitude: 0 });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const grin = adapter.params.get("Param54") ?? 0;
    // High arousal -> ~28; we should see basically the full value
    // because suppression is zero.
    expect(grin).toBeGreaterThan(25);
    expect(grin).toBeLessThanOrEqual(30);
  });

  it("active lip-sync amplitude tapers the grin overlay toward zero", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeMouthOverlayDeps({
      // Mid-amplitude TTS chunk; with gain=6 this fully saturates
      // the suppression factor.
      audioAmplitude: 0.3,
    });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const grin = adapter.params.get("Param54") ?? 0;
    // Suppressed grin should be effectively zero — well below any
    // visible threshold on the rig.
    expect(grin).toBeLessThan(1);
  });

  it("non-mouth bindings on the same expression are unaffected by lipsync", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeMouthOverlayDeps({ audioAmplitude: 0.3 });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    // Param80 is on the same lzx expression but NOT in
    // mouth_overlay_param_ids — its arousal-scaled write must
    // survive the suppression intact.
    const param80 = adapter.params.get("Param80") ?? 0;
    expect(param80).toBeGreaterThan(25);
    expect(param80).toBeLessThanOrEqual(30);
  });

  it("grin recovers smoothly when audio falls back to silence", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock, setSnapshot } = makeMouthOverlayDeps({
      audioAmplitude: 0.3,
    });
    channel.attach(adapter, deps);
    // Settle in fully suppressed state.
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const suppressed = adapter.params.get("Param54") ?? 0;
    expect(suppressed).toBeLessThan(1);

    // Speech ends — audio drops back to zero. The suppression
    // factor must decay so the grin re-emerges.
    setSnapshot({ audioAmplitude: 0 });
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const recovered = adapter.params.get("Param54") ?? 0;
    expect(recovered).toBeGreaterThan(20);
  });

  it("does not allocate or fight when no mouth overlay ids are declared", () => {
    // Sanity: the original path (lzx with NO mouth_overlay_param_ids)
    // must keep behaving exactly like the pre-fix arousal-scaled
    // write. Asserts we didn't regress non-Alexia rigs.
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, clock } = makeAmplitudeManifestDeps({
      audioAmplitude: 0.3,
      mood: { label: "excited", intensity: 0.9, valence: 0.7, arousal: 0.9 },
    });
    channel.attach(adapter, deps);
    runPreModel(channel, adapter, clock, 240, 1 / 60);
    const grin = adapter.params.get("Param54") ?? 0;
    // No mouth_overlay_param_ids => mouthScale stays at 1, so the
    // value is the high-arousal full-amplitude write.
    expect(grin).toBeGreaterThan(25);
    expect(grin).toBeLessThanOrEqual(30);
  });
});

/** Build a manifest where the ``playful`` reaction maps to a
 * day-clothes-only expression (``zs1`` for Alexia) and ``amused``
 * sits next on the neighbour chain. The combination lets us prove
 * the gate flips the resolved expression when the active outfit
 * changes. */
function makeOutfitGateDeps(initialSnap: Partial<ChannelStoreSnapshot> = {}) {
  const clock = new FakeClock(1_000);
  const engineState = createEngineState();
  let snapshot: ChannelStoreSnapshot = {
    reaction: "playful",
    ttsState: "idle",
    voiceMode: "off",
    turnInProgress: false,
    audioAmplitude: 0,
    avatarOverlay: null,
    avatarMotion: null,
    mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 },
    resolvedOutfit: "day",
    backchannelHint: "",
    expressiveness: 1,
    ...initialSnap,
  };
  const manifest = buildManifest({
    reaction_mapping: {
      playful: "zs1",
      amused: "lzx",
      cheerful: "lzx",
      neutral: "n",
    },
    expressions: [
      { name: "zs1", file: "zs1.exp3.json" },
      { name: "lzx", file: "lzx.exp3.json" },
      { name: "n", file: "n.exp3.json" },
    ],
    outfit_gated_expressions: {
      zs1: ["day_clothes"],
    },
  });
  return {
    clock,
    engineState,
    deps: {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    },
    setSnapshot: (next: Partial<ChannelStoreSnapshot>) => {
      snapshot = { ...snapshot, ...next };
    },
  };
}

describe("ExpressionChannel — outfit gate", () => {
  it("applies the gated expression when the active outfit matches", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeOutfitGateDeps({ resolvedOutfit: "day" });
    channel.attach(adapter, deps);
    // day -> day_clothes capability matches the gate -> ``zs1`` fires.
    expect(adapter.expressionCalls).toEqual(["zs1"]);
  });

  it("falls through to the neighbour chain when the gate fails", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeOutfitGateDeps({ resolvedOutfit: "pajamas" });
    channel.attach(adapter, deps);
    // pajamas is NOT in the gate -> resolver walks
    // ``playful`` -> ``amused`` (which maps to ``lzx``) per the
    // reaction-neighbour chain.
    expect(adapter.expressionCalls).toEqual(["lzx"]);
  });

  it("re-applies the resolved expression on outfit change", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, setSnapshot } = makeOutfitGateDeps({
      resolvedOutfit: "day",
    });
    channel.attach(adapter, deps);
    expect(adapter.expressionCalls).toEqual(["zs1"]);

    // Toggle to pajamas — the snapshot updates first (as StoreBridge
    // does), then the channel receives the hook callback.
    setSnapshot({ resolvedOutfit: "pajamas" });
    channel.onOutfitChange!("pajamas");
    // Gate now fails for zs1 -> resolver falls through to lzx.
    expect(adapter.expressionCalls).toEqual(["zs1", "lzx"]);
  });

  it("treats unknown / empty outfit as permissive", () => {
    // Freshly loaded rig that hasn't reported its outfit yet must
    // not strand every gated reaction — we let the gated expression
    // through and let StoreBridge's later resolved_outfit push
    // tighten the gate.
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeOutfitGateDeps({ resolvedOutfit: "" });
    channel.attach(adapter, deps);
    expect(adapter.expressionCalls).toEqual(["zs1"]);
  });

  it("ignores the gate when outfit_gated_expressions is undefined", () => {
    // Cached profile payload from a pre-feature backend doesn't
    // include the new field. The resolver must treat absent gate as
    // "no constraint" so old payloads keep working.
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const clock = new FakeClock(1_000);
    const engineState = createEngineState();
    const snapshot: ChannelStoreSnapshot = {
      reaction: "playful",
      ttsState: "idle",
      voiceMode: "off",
      turnInProgress: false,
      audioAmplitude: 0,
      avatarOverlay: null,
      avatarMotion: null,
      mood: { label: "content", intensity: 0.5, valence: 0, arousal: 0.5 },
      resolvedOutfit: "pajamas",
      backchannelHint: "",
      expressiveness: 1,
    };
    const manifest = buildManifest({
      reaction_mapping: { playful: "zs1", amused: "lzx" },
      expressions: [
        { name: "zs1", file: "zs1.exp3.json" },
        { name: "lzx", file: "lzx.exp3.json" },
      ],
      // outfit_gated_expressions intentionally omitted.
    });
    const deps: ChannelDeps = {
      now: clock.now,
      manifest,
      engineState,
      getStoreSnapshot: () => snapshot,
    };
    channel.attach(adapter, deps);
    // No gate metadata -> ``zs1`` fires even though we're in
    // pajamas, mirroring legacy behaviour.
    expect(adapter.expressionCalls).toEqual(["zs1"]);
  });
});

describe("ExpressionChannel — debug instrumentation", () => {
  it("calls deps.debug with the chosen expression on reaction apply", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "neutral", resolvedOutfit: "day" });
    const debug = vi.fn();
    channel.attach(adapter, { ...deps, debug });

    debug.mockClear();
    channel.onReaction!("cheerful");

    // ``debug`` is called via the central ``_applyTarget`` path. The
    // payload is the cry-bug forensic tuple: reaction + outfit +
    // chosen expression name. This is the contract the
    // ``app.log`` consumer relies on when diagnosing the "she cried
    // when I said cheerful" report.
    const applyCall = debug.mock.calls.find(
      (call) => call[1] === "applyReaction",
    );
    expect(applyCall).toBeDefined();
    expect(applyCall![0]).toBe("channel.expression");
    expect(applyCall![2]).toMatchObject({
      reaction: "cheerful",
      expression: "ExprCheerful",
    });
  });

  it("calls deps.debug for voice-mode-driven expression swaps", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps, setSnapshot } = makeDeps({ reaction: "neutral" });
    const debug = vi.fn();
    channel.attach(adapter, { ...deps, debug });

    debug.mockClear();
    setSnapshot({ voiceMode: "thinking" });
    channel.onVoiceMode!("thinking");

    const modeCall = debug.mock.calls.find((call) => call[1] === "applyMode");
    expect(modeCall).toBeDefined();
    expect(modeCall![2]).toMatchObject({ mode: "thinking" });
  });

  it("works without a debug hook (no-op when undefined)", () => {
    const adapter = new FakeAdapter();
    const channel = new ExpressionChannel();
    const { deps } = makeDeps({ reaction: "cheerful" });
    // Explicitly omit ``debug`` — production attaches it but tests
    // and older callers may not. The channel must not throw.
    expect(() => {
      channel.attach(adapter, deps);
      channel.onReaction!("sad");
    }).not.toThrow();
  });
});

// Hardcoded mirror of ``REACTIONS`` from ``app/core/reactions.py``.
// If the Python source grows a new reaction, this list must grow too
// AND ``_REACTION_NEIGHBOURS`` must gain a chain for it (see B5
// cry-cascade safety). Drift on either side will break the parity
// test below.
const PYTHON_REACTIONS = [
  "neutral",
  "cheerful",
  "excited",
  "enthusiastic",
  "amused",
  "playful",
  "surprised",
  "curious",
  "friendly",
  "warm",
  "tender",
  "thoughtful",
  "wistful",
  "calm",
  "serious",
  "concerned",
  "sad",
  "melancholy",
  "cry",
  "tired",
  "gentle",
  "angry",
  "frustrated",
  "confused",
  "embarrassed",
  "nervous",
  "defiant",
] as const;

describe("ExpressionChannel — _REACTION_NEIGHBOURS parity with Python", () => {
  it("has a non-empty neighbour chain for every Python reaction", () => {
    const missing: string[] = [];
    for (const reaction of PYTHON_REACTIONS) {
      const chain = _REACTION_NEIGHBOURS[reaction];
      if (!chain || chain.length === 0) {
        missing.push(reaction);
      }
    }
    expect(missing).toEqual([]);
  });

  it("only references reactions Python knows about (neighbours stay canonical)", () => {
    const knownReactions = new Set<string>(PYTHON_REACTIONS);
    const offenders: Array<[string, string]> = [];
    for (const [reaction, chain] of Object.entries(_REACTION_NEIGHBOURS)) {
      for (const neighbour of chain) {
        if (!knownReactions.has(neighbour)) {
          offenders.push([reaction, neighbour]);
        }
      }
    }
    expect(offenders).toEqual([]);
  });
});
