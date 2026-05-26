/**
 * Smoke test for the Vitest harness + the two test fixtures.
 *
 * If this file doesn't run cleanly, every channel test below it is
 * dead in the water. Keep it dead-simple: the assertions only exercise
 * the fixtures themselves so a future channel-shape change can't
 * accidentally invalidate the smoke check.
 */
import { describe, expect, it } from "vitest";

import { FakeAdapter } from "./fake-model";
import { FakeClock } from "./fake-clock";

describe("test infrastructure smoke", () => {
  it("FakeClock.advance returns the new monotonic timestamp", () => {
    const clock = new FakeClock(1_000);
    expect(clock.now()).toBe(1_000);
    expect(clock.advance(50)).toBe(1_050);
    expect(clock.advance(0)).toBe(1_050);
  });

  it("FakeAdapter records param writes with monotonic seq", () => {
    const adapter = new FakeAdapter();
    adapter.setParam("ParamX", 1);
    adapter.setParam("ParamY", 2);
    adapter.setParam("ParamX", 3);
    expect(adapter.getParam("ParamX")).toBe(3);
    expect(adapter.getParam("ParamY")).toBe(2);
    expect(adapter.setParamHistory.map((r) => r.seq)).toEqual([0, 1, 2]);
  });

  it("FakeAdapter triggerBeforeModelUpdate fires registered listeners", () => {
    const adapter = new FakeAdapter();
    let calls = 0;
    const off = adapter.onBeforeModelUpdate(() => {
      calls += 1;
    });
    adapter.triggerBeforeModelUpdate();
    adapter.triggerBeforeModelUpdate();
    expect(calls).toBe(2);
    off();
    adapter.triggerBeforeModelUpdate();
    expect(calls).toBe(2);
  });

  it("FakeAdapter expression / motion / resetExpression are recorded", () => {
    const adapter = new FakeAdapter();
    adapter.expression("grin");
    adapter.expression("stars");
    adapter.motion("Idle", 0);
    adapter.motion("Talk", 2, 1);
    adapter.resetExpression();
    expect(adapter.expressionCalls).toEqual(["grin", "stars"]);
    expect(adapter.motionCalls).toEqual([
      { group: "Idle", index: 0, priority: undefined },
      { group: "Talk", index: 2, priority: 1 },
    ]);
    expect(adapter.resetExpressionCount).toBe(1);
  });
});
