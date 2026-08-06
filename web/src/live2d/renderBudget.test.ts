/**
 * The render budget is a policy, not a mechanism, so it is worth pinning
 * precisely: the whole point is that phones get throttled and desktop
 * does not notice. A regression here is invisible until someone's laptop
 * avatar starts running at 30 fps, or someone's phone gets hot again.
 */
import { describe, expect, it } from "vitest";

import {
  MOBILE_MAX_FPS,
  MOBILE_MAX_RESOLUTION,
  resolveRenderBudget,
} from "./renderBudget";

describe("resolveRenderBudget", () => {
  it("leaves desktop exactly as it was", () => {
    const budget = resolveRenderBudget({
      mobile: false,
      devicePixelRatio: 2,
    });
    expect(budget).toEqual({
      maxFPS: 0, // uncapped, Pixi's own default
      resolution: 2, // native, untouched
      powerPreference: "default",
    });
  });

  it("caps the frame rate and asks for the low-power GPU on a phone", () => {
    const budget = resolveRenderBudget({
      mobile: true,
      devicePixelRatio: 2,
    });
    expect(budget.maxFPS).toBe(MOBILE_MAX_FPS);
    expect(budget.powerPreference).toBe("low-power");
  });

  it("clamps a 3x phone to the pixel-ratio ceiling", () => {
    // The expensive case: 3x turns a small floating-persona box into a
    // ~570x750 backing buffer redrawn at the display rate.
    const budget = resolveRenderBudget({
      mobile: true,
      devicePixelRatio: 3,
    });
    expect(budget.resolution).toBe(MOBILE_MAX_RESOLUTION);
  });

  it("does not upscale a phone that is already below the ceiling", () => {
    const budget = resolveRenderBudget({
      mobile: true,
      devicePixelRatio: 1.5,
    });
    expect(budget.resolution).toBe(1.5);
  });

  it("treats a missing or nonsense pixel ratio as 1x", () => {
    expect(
      resolveRenderBudget({ mobile: false, devicePixelRatio: 0 }).resolution,
    ).toBe(1);
    expect(
      resolveRenderBudget({ mobile: true, devicePixelRatio: 0 }).resolution,
    ).toBe(1);
  });
});
