import { describe, expect, it } from "vitest";

import {
  expressionNameForClass,
  intensityScale,
} from "./resolver";
import { buildManifest } from "../__fixtures__/test-manifest";

describe("Live behavior resolver aliases", () => {
  it("maps semantic tones onto reaction_mapping aliases", () => {
    const manifest = buildManifest({
      reaction_mapping: {
        amused: "lzx",
        concerned: "worry",
        thoughtful: "think",
      },
    });
    expect(expressionNameForClass("amused", manifest)).toBe("lzx");
    expect(expressionNameForClass("concerned", manifest)).toBe("worry");
    expect(expressionNameForClass("curious", manifest)).toBe("think");
  });

  it("no-ops when the rig has no matching alias", () => {
    const manifest = buildManifest({ reaction_mapping: { neutral: "n" } });
    expect(expressionNameForClass("amused", manifest)).toBeNull();
    expect(expressionNameForClass("none", manifest)).toBeNull();
  });

  it("scales the intensity band without inventing a fourth step", () => {
    expect(intensityScale(undefined)).toBe(1);
    expect(intensityScale("low")).toBe(0.4);
    expect(intensityScale("mid")).toBe(0.7);
    expect(intensityScale("high")).toBe(1);
    expect(intensityScale("max")).toBe(1);
  });
});
