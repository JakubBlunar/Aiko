/**
 * Tests for ``StoreBridge`` -- the glue between the Zustand store
 * and the ``AvatarEngine``. The bridge has two responsibilities:
 *
 * 1. Dispatch the *initial* state once on ``start()`` so channels
 *    don't have to special-case "we haven't seen reaction X yet".
 * 2. Subscribe to discrete store slices and forward each change as
 *    a single dispatch on the engine.
 *
 * The continuous-expressiveness pass added a new ``expressiveness``
 * field that channels read from ``getStoreSnapshot()`` rather than
 * via dispatch. The bridge type signature must accept it without
 * complaint, and channels reading the snapshot must see the slider
 * value verbatim. We assert both here with a tiny in-memory store
 * that mimics Zustand's ``getState`` / ``subscribe`` shape.
 */
import { afterEach, describe, expect, it } from "vitest";

import { AvatarEngine } from "./AvatarEngine";
import { createEngineState } from "./state";
import { StoreBridge, type BridgedState, type BridgedStore } from "./StoreBridge";
import type {
  AvatarChannel,
  ChannelDeps,
  ChannelStoreSnapshot,
  Live2DModelAdapter,
} from "./types";

import { FakeAdapter } from "./__fixtures__/fake-model";
import { FakeClock } from "./__fixtures__/fake-clock";
import { FakeMouseSource } from "./__fixtures__/fake-mouse-source";
import { ManualRaf } from "./__fixtures__/manual-raf";
import { buildManifest, NEUTRAL_MOOD } from "./__fixtures__/test-manifest";

type MiniStoreListener = (state: BridgedState, prevState: BridgedState) => void;

interface MiniStore {
  getState(): BridgedState;
  getInitialState(): BridgedState;
  setState(next: Partial<BridgedState>): void;
  subscribe(listener: MiniStoreListener): () => void;
}

function createMiniStore(initial: BridgedState): MiniStore {
  let state = initial;
  const initialSnapshot = initial;
  const listeners = new Set<MiniStoreListener>();
  return {
    getState: () => state,
    getInitialState: () => initialSnapshot,
    setState: (next) => {
      const prev = state;
      state = { ...state, ...next };
      for (const fn of listeners) {
        fn(state, prev);
      }
    },
    subscribe: (listener) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
  };
}

class CapturingChannel implements AvatarChannel {
  readonly name = "capturing";
  reactions: string[] = [];
  voiceModes: string[] = [];
  backchannels: string[] = [];
  expressiveness: number | undefined = undefined;
  private _deps: ChannelDeps | null = null;

  attach(_adapter: Live2DModelAdapter, deps: ChannelDeps): void {
    this._deps = deps;
  }
  detach(): void {
    this._deps = null;
  }
  onReaction(reaction: string): void {
    this.reactions.push(reaction);
  }
  onVoiceMode(mode: string): void {
    this.voiceModes.push(mode);
  }
  onBackchannel(hint: string): void {
    this.backchannels.push(hint);
  }
  /** Sample whatever expressiveness value the snapshot reports right
   * now. Used by tests to verify the snapshot accessor surfaces the
   * slider value. */
  sample(): void {
    if (!this._deps) return;
    const snap: ChannelStoreSnapshot = this._deps.getStoreSnapshot();
    this.expressiveness = snap.expressiveness;
  }
}

const BASE_STATE: BridgedState = {
  reaction: "neutral",
  ttsState: "idle",
  voiceMode: "off",
  audioAmplitude: 0,
  avatarOverlay: null,
  avatarMotion: null,
  avatar: null,
  mood: NEUTRAL_MOOD,
  backchannelHint: null,
  backchannelAt: 0,
};

interface TestRig {
  engine: AvatarEngine;
  bridge: StoreBridge;
  channel: CapturingChannel;
  adapter: FakeAdapter;
  store: MiniStore;
  /** Override the snapshot ``expressiveness`` returned by the
   * engine's ``getStoreSnapshot``. The bridge itself doesn't carry
   * ``expressiveness`` (it's not a discrete dispatch); channels read
   * it from the snapshot accessor wired in ``Live2DAvatar.tsx``. */
  setExpressiveness: (value: number | undefined) => void;
}

function makeRig(initial: Partial<BridgedState> = {}): TestRig {
  const adapter = new FakeAdapter();
  const clock = new FakeClock(1_000);
  const raf = new ManualRaf();
  const mouse = new FakeMouseSource();
  const engineState = createEngineState();
  const store = createMiniStore({ ...BASE_STATE, ...initial });
  let expressiveness: number | undefined = 1;

  const engine = new AvatarEngine({
    manifest: buildManifest(),
    engineState,
    getStoreSnapshot: () => ({
      reaction: store.getState().reaction,
      ttsState: store.getState().ttsState,
      voiceMode: store.getState().voiceMode,
      turnInProgress: false,
      audioAmplitude: store.getState().audioAmplitude,
      avatarOverlay: store.getState().avatarOverlay,
      avatarMotion: store.getState().avatarMotion,
      mood: store.getState().mood,
      resolvedOutfit: "",
      backchannelHint: store.getState().backchannelHint ?? "",
      circadianPeriod: "",
      expressiveness,
    }),
    now: clock.now,
    mouseSource: mouse,
    scheduleFrame: raf.schedule,
    cancelFrame: raf.cancel,
  });
  const channel = new CapturingChannel();
  engine.register(channel);
  engine.start(adapter);
  // ``MiniStore`` is intentionally a structural subset of zustand's
  // ``StoreApi`` — we only consume the slice ``StoreBridge`` actually
  // touches. The double cast avoids dragging the full
  // ``StoreApi``-overloaded ``setState`` signature into the test
  // harness.
  const bridge = new StoreBridge(engine, store as unknown as BridgedStore);
  return {
    engine,
    bridge,
    channel,
    adapter,
    store,
    setExpressiveness: (value) => {
      expressiveness = value;
    },
  };
}

describe("StoreBridge — initial dispatch", () => {
  let rig: TestRig | null = null;

  afterEach(() => {
    rig?.bridge.stop();
    rig?.engine.stop();
    rig = null;
  });

  it("dispatches the initial reaction + voiceMode on start()", () => {
    rig = makeRig({ reaction: "cheerful", voiceMode: "listening" });
    rig.bridge.start();
    expect(rig.channel.reactions).toEqual(["cheerful"]);
    expect(rig.channel.voiceModes).toEqual(["listening"]);
  });

  it("does not dispatch a backchannel when there is no initial hint", () => {
    rig = makeRig();
    rig.bridge.start();
    // No backchannel dispatched on start; only on subsequent
    // ``backchannelAt`` increments.
    expect(rig.channel.backchannels).toEqual([]);
  });
});

describe("StoreBridge — change dispatch", () => {
  let rig: TestRig | null = null;

  afterEach(() => {
    rig?.bridge.stop();
    rig?.engine.stop();
    rig = null;
  });

  it("forwards reaction changes through to the channel", () => {
    rig = makeRig();
    rig.bridge.start();
    rig.channel.reactions.length = 0;
    rig.store.setState({ reaction: "sad" });
    rig.store.setState({ reaction: "cheerful" });
    expect(rig.channel.reactions).toEqual(["sad", "cheerful"]);
  });

  it("forwards voiceMode changes through to the channel", () => {
    rig = makeRig();
    rig.bridge.start();
    rig.channel.voiceModes.length = 0;
    rig.store.setState({ voiceMode: "listening" });
    rig.store.setState({ voiceMode: "off" });
    expect(rig.channel.voiceModes).toEqual(["listening", "off"]);
  });

  it("dispatches a backchannel each time backchannelAt increments", () => {
    rig = makeRig();
    rig.bridge.start();
    rig.channel.backchannels.length = 0;
    rig.store.setState({
      backchannelHint: "agreement",
      backchannelAt: 1,
    });
    rig.store.setState({
      backchannelHint: "thinking",
      backchannelAt: 2,
    });
    expect(rig.channel.backchannels).toEqual(["agreement", "thinking"]);
  });

  it("stop() unsubscribes so later store changes are dropped", () => {
    rig = makeRig();
    rig.bridge.start();
    rig.bridge.stop();
    rig.channel.reactions.length = 0;
    rig.store.setState({ reaction: "angry" });
    expect(rig.channel.reactions).toEqual([]);
  });

  it("stop() is idempotent", () => {
    rig = makeRig();
    rig.bridge.start();
    expect(() => {
      rig!.bridge.stop();
      rig!.bridge.stop();
    }).not.toThrow();
  });
});

describe("StoreBridge — expressiveness reaches the channel snapshot", () => {
  let rig: TestRig | null = null;

  afterEach(() => {
    rig?.bridge.stop();
    rig?.engine.stop();
    rig = null;
  });

  it("default expressiveness of 1.0 round-trips into the snapshot", () => {
    rig = makeRig();
    rig.bridge.start();
    rig.channel.sample();
    expect(rig.channel.expressiveness).toBeCloseTo(1);
  });

  it("setting expressiveness to 0 surfaces in the channel's snapshot", () => {
    rig = makeRig();
    rig.bridge.start();
    rig.setExpressiveness(0);
    rig.channel.sample();
    expect(rig.channel.expressiveness).toBe(0);
  });

  it("setting expressiveness to 1.5 surfaces in the channel's snapshot", () => {
    rig = makeRig();
    rig.bridge.start();
    rig.setExpressiveness(1.5);
    rig.channel.sample();
    expect(rig.channel.expressiveness).toBe(1.5);
  });

  it("an undefined expressiveness leaves the snapshot field undefined for legacy callers", () => {
    rig = makeRig();
    rig.bridge.start();
    rig.setExpressiveness(undefined);
    rig.channel.sample();
    expect(rig.channel.expressiveness).toBeUndefined();
  });
});

describe("StoreBridge — error tolerance", () => {
  it("a throwing unsubscribe in stop() does not propagate", () => {
    const adapter = new FakeAdapter();
    const clock = new FakeClock(1_000);
    const raf = new ManualRaf();
    const engineState = createEngineState();
    const store = createMiniStore(BASE_STATE);
    const engine = new AvatarEngine({
      manifest: buildManifest(),
      engineState,
      getStoreSnapshot: () => ({
        reaction: store.getState().reaction,
        ttsState: store.getState().ttsState,
        voiceMode: store.getState().voiceMode,
        turnInProgress: false,
        audioAmplitude: 0,
        avatarOverlay: null,
        avatarMotion: null,
        mood: NEUTRAL_MOOD,
        resolvedOutfit: "",
        backchannelHint: "",
        circadianPeriod: "",
        expressiveness: 1,
      }),
      now: clock.now,
      mouseSource: new FakeMouseSource(),
      scheduleFrame: raf.schedule,
      cancelFrame: raf.cancel,
    });
    engine.register(new CapturingChannel());
    engine.start(adapter);

    // Wrap the store's subscribe so the returned unsubscribe throws
    // on stop -- the bridge swallows that.
    const originalSubscribe = store.subscribe;
    store.subscribe = (listener) => {
      const off = originalSubscribe(listener);
      return () => {
        off();
        throw new Error("simulated unsubscribe failure");
      };
    };
    const bridge = new StoreBridge(engine, store as unknown as BridgedStore);
    bridge.start();
    expect(() => bridge.stop()).not.toThrow();
    engine.stop();
  });
});
