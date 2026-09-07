import { describe, expect, it } from "vitest";

import { FakeAdapter } from "../__fixtures__/fake-model";
import { FakeClock } from "../__fixtures__/fake-clock";
import { buildManifest, buildStoreSnapshot } from "../__fixtures__/test-manifest";
import { createEngineState } from "../state";
import type { ChannelStoreSnapshot } from "../types";
import { SleepChannel } from "./SleepChannel";

function setup(
  initialStatus: NonNullable<ChannelStoreSnapshot["sleepStatus"]> = "asleep",
  parameters = [
    { id: "ParamEyeLOpen", name: "left eye", min: 0, max: 1, default: 1 },
    { id: "ParamEyeROpen", name: "right eye", min: 0, max: 1, default: 1 },
    { id: "ParamBreath", name: "breath", min: 0, max: 1, default: 0 },
    { id: "ParamBodyAngleY", name: "body y", min: -30, max: 30, default: 0 },
  ],
) {
  const adapter = new FakeAdapter();
  const clock = new FakeClock(1_000);
  let snapshot = buildStoreSnapshot({
    sleepStatus: initialStatus,
  }) as ChannelStoreSnapshot;
  const channel = new SleepChannel();
  channel.attach(adapter, {
    now: clock.now,
    manifest: buildManifest({ parameters }),
    engineState: createEngineState(),
    getStoreSnapshot: () => snapshot,
  });
  return {
    adapter,
    channel,
    setStatus: (
      sleepStatus: NonNullable<ChannelStoreSnapshot["sleepStatus"]>,
    ) => {
      snapshot = { ...snapshot, sleepStatus };
    },
  };
}

describe("SleepChannel", () => {
  it("eases supported eye parameters closed while asleep", () => {
    const { adapter, channel } = setup();
    for (let i = 0; i < 120; i += 1) channel.tickTier3!(i * 50, 0.05);
    expect(adapter.params.get("ParamEyeLOpen")).toBeLessThan(0.05);
    expect(adapter.params.get("ParamEyeROpen")).toBeLessThan(0.05);
    expect(adapter.params.get("ParamBreath")).toBeLessThan(0.2);
  });

  it("releases owned parameters gradually after waking", () => {
    const { adapter, channel, setStatus } = setup();
    for (let i = 0; i < 120; i += 1) channel.tickTier3!(i * 50, 0.05);
    setStatus("woken");
    channel.tickTier3!(2_000, 0.1);
    expect(adapter.params.get("ParamEyeLOpen")).toBeLessThan(1);
    for (let i = 0; i < 300; i += 1) channel.tickTier3!(2_100 + i * 100, 0.1);
    expect(adapter.params.get("ParamEyeLOpen")).toBe(1);
    expect(adapter.params.get("ParamBreath")).toBe(0);
    expect(adapter.params.get("ParamBodyAngleY")).toBe(0);
  });

  it("does not write unsupported rig parameters", () => {
    const { adapter, channel } = setup("asleep", []);
    channel.tickTier3!(1_000, 1);
    expect(adapter.setParamHistory).toHaveLength(0);
  });

  it("restores parameters on detach", () => {
    const { adapter, channel } = setup();
    channel.tickTier3!(1_000, 1);
    channel.detach();
    expect(adapter.params.get("ParamEyeLOpen")).toBe(1);
    expect(adapter.params.get("ParamEyeROpen")).toBe(1);
    expect(adapter.params.get("ParamBreath")).toBe(0);
  });
});
