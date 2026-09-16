/**
 * IdleLifeChannel — H10 world activity / Live plan embodiment.
 */
import { describe, expect, it } from "vitest";

import { IdleLifeChannel } from "./IdleLifeChannel";
import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest, buildStoreSnapshot } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type { ChannelDeps, ChannelStoreSnapshot } from "../types";

function setup(
  snapshot: Partial<ChannelStoreSnapshot> = {},
  capabilities: Record<string, boolean> = {
    has_body_angle_y: true,
    has_body_angle_z: true,
    has_breath: true,
  },
) {
  const adapter = new FakeAdapter();
  const clock = new FakeClock(1_000);
  let snap = buildStoreSnapshot(snapshot) as ChannelStoreSnapshot;
  const channel = new IdleLifeChannel();
  const deps: ChannelDeps = {
    now: clock.now,
    manifest: buildManifest({
      capabilities,
      parameters: [
        { id: "ParamBodyAngleY", name: "y" },
        { id: "ParamBodyAngleZ", name: "z" },
        { id: "ParamBreath", name: "breath" },
      ],
    }),
    engineState: createEngineState(),
    getStoreSnapshot: () => snap,
  };
  channel.attach(adapter, deps);
  return {
    adapter,
    channel,
    clock,
    setSnapshot: (next: Partial<ChannelStoreSnapshot>) => {
      snap = { ...snap, ...next };
    },
    tick: (n = 40) => {
      for (let i = 0; i < n; i += 1) {
        clock.advance(16);
        channel.tickPreModel!();
      }
    },
  };
}

describe("IdleLifeChannel", () => {
  it("settles the body while the world activity is reading", () => {
    const { adapter, tick } = setup({ worldActivity: "reading" });
    tick();
    expect(adapter.getParam("ParamBodyAngleY") ?? 0).toBeLessThan(0);
  });

  it("writes nothing on a rig without body/breath capabilities", () => {
    const { adapter, tick } = setup({ worldActivity: "reading" }, {});
    tick();
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("degrades to the sleep channel while asleep", () => {
    const { adapter, tick } = setup({
      worldActivity: "reading",
      sleepStatus: "asleep",
    });
    tick();
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("yields composing/listening to existing reflexes", () => {
    const { adapter, tick, setSnapshot } = setup({ worldActivity: "reading" });
    tick();
    const settled = adapter.getParam("ParamBodyAngleY") ?? 0;
    expect(settled).toBeLessThan(0);
    setSnapshot({ composing: true });
    tick(80);
    expect(Math.abs(adapter.getParam("ParamBodyAngleY") ?? 0)).toBeLessThan(
      Math.abs(settled),
    );
  });

  it("cancels motion execution in v1", () => {
    const { channel } = setup();
    expect(channel.motionCancelReason).toBe("no_authored_idle_life_motion");
    channel.detach();
    expect(channel.motionCancelReason).toBe("detach");
  });

  it("applies a Live amused expression when conversation is idle", () => {
    const adapter = new FakeAdapter();
    const clock = new FakeClock(1_000);
    const snap = buildStoreSnapshot({
      reaction: "neutral",
      liveEmbodiment: {
        intent: "react_affectively",
        attention_target: "user",
        gaze_class: "user_eye_contact",
        body_class: "perk",
        breath_class: "normal",
        expression_class: "amused",
        motion_class: "none",
        degrade_to_sleep: false,
        reaction_tone: "amused",
        reaction_intensity: "high",
      },
    }) as ChannelStoreSnapshot;
    const channel = new IdleLifeChannel();
    channel.attach(adapter, {
      now: clock.now,
      manifest: buildManifest({
        capabilities: {
          has_body_angle_y: true,
          has_body_angle_z: true,
          has_breath: true,
        },
        parameters: [
          { id: "ParamBodyAngleY", name: "y" },
          { id: "ParamBodyAngleZ", name: "z" },
          { id: "ParamBreath", name: "breath" },
        ],
        reaction_mapping: { amused: "lzx", playful: "zs1" },
      }),
      engineState: createEngineState(),
      getStoreSnapshot: () => snap,
    });
    clock.advance(16);
    channel.tickPreModel!();
    expect(adapter.expressionCalls).toEqual(["lzx"]);
  });

  it("lets conversation reactions outrank Live tone", () => {
    const adapter = new FakeAdapter();
    const clock = new FakeClock(1_000);
    const snap = buildStoreSnapshot({
      reaction: "playful",
      liveEmbodiment: {
        intent: "react_affectively",
        attention_target: "user",
        gaze_class: "rest",
        body_class: "perk",
        breath_class: "normal",
        expression_class: "amused",
        motion_class: "none",
        degrade_to_sleep: false,
        reaction_tone: "amused",
        reaction_intensity: "high",
      },
    }) as ChannelStoreSnapshot;
    const channel = new IdleLifeChannel();
    channel.attach(adapter, {
      now: clock.now,
      manifest: buildManifest({
        capabilities: { has_body_angle_y: true },
        reaction_mapping: { amused: "lzx", playful: "zs1" },
      }),
      engineState: createEngineState(),
      getStoreSnapshot: () => snap,
    });
    clock.advance(16);
    channel.tickPreModel!();
    expect(adapter.expressionCalls).toEqual([]);
  });

  it("no-ops Live expression when the rig has no alias", () => {
    const adapter = new FakeAdapter();
    const clock = new FakeClock(1_000);
    const snap = buildStoreSnapshot({
      liveEmbodiment: {
        intent: "react_affectively",
        attention_target: "user",
        gaze_class: "rest",
        body_class: "none",
        breath_class: "normal",
        expression_class: "amused",
        motion_class: "none",
        degrade_to_sleep: false,
        reaction_tone: "amused",
        reaction_intensity: "high",
      },
    }) as ChannelStoreSnapshot;
    const channel = new IdleLifeChannel();
    channel.attach(adapter, {
      now: clock.now,
      manifest: buildManifest({
        reaction_mapping: { neutral: "n" },
      }),
      engineState: createEngineState(),
      getStoreSnapshot: () => snap,
    });
    clock.advance(16);
    channel.tickPreModel!();
    expect(adapter.expressionCalls).toEqual([]);
  });

  it("no-ops Live expression when the rig cannot express", () => {
    const { adapter, tick } = setup({
      liveEmbodiment: {
        intent: "react_affectively",
        attention_target: "user",
        gaze_class: "rest",
        body_class: "perk",
        breath_class: "normal",
        expression_class: "amused",
        motion_class: "none",
        degrade_to_sleep: false,
        reaction_tone: "amused",
        reaction_intensity: "high",
      },
    }, {
      has_body_angle_y: true,
      has_body_angle_z: true,
      has_breath: true,
    });
    tick();
    expect(adapter.expressionCalls).toEqual([]);
  });

  it("scales Live body less at low intensity than at high", () => {
    const low = setup({
      liveEmbodiment: {
        intent: "react_affectively",
        attention_target: "user",
        gaze_class: "rest",
        body_class: "perk",
        breath_class: "normal",
        expression_class: "amused",
        motion_class: "none",
        degrade_to_sleep: false,
        reaction_tone: "amused",
        reaction_intensity: "low",
      },
    });
    const high = setup({
      liveEmbodiment: {
        intent: "react_affectively",
        attention_target: "user",
        gaze_class: "rest",
        body_class: "perk",
        breath_class: "normal",
        expression_class: "amused",
        motion_class: "none",
        degrade_to_sleep: false,
        reaction_tone: "amused",
        reaction_intensity: "high",
      },
    });
    low.tick();
    high.tick();
    const lowY = Math.abs(low.adapter.getParam("ParamBodyAngleY") ?? 0);
    const highY = Math.abs(high.adapter.getParam("ParamBodyAngleY") ?? 0);
    expect(lowY).toBeGreaterThan(0);
    expect(lowY).toBeLessThan(highY);
  });

  it("does not shrink Pass 5 body when Live tone is neutral", () => {
    const plan = {
      intent: "remain_present",
      attention_target: "none",
      gaze_class: "rest",
      body_class: "perk",
      breath_class: "normal",
      expression_class: "none",
      motion_class: "none",
      degrade_to_sleep: false,
    };
    const unscaled = setup({ liveEmbodiment: plan });
    const neutralLow = setup({
      liveEmbodiment: {
        ...plan,
        reaction_tone: "neutral",
        reaction_intensity: "low",
      },
    });
    unscaled.tick();
    neutralLow.tick();
    expect(neutralLow.adapter.getParam("ParamBodyAngleY")).toBeCloseTo(
      unscaled.adapter.getParam("ParamBodyAngleY") ?? 0,
    );
  });
});
