import { describe, expect, it } from "vitest";

import { TOUCH_GESTURE_LABELS, USER_REACTION_KINDS } from "@/types";
import { REACTION_DESCRIPTIONS, TOUCH_DESCRIPTIONS } from "./ReactionLegend";

describe("ReactionLegend — reaction description coverage", () => {
  it("has a description for every reaction kind in the taxonomy", () => {
    for (const r of USER_REACTION_KINDS) {
      expect(
        REACTION_DESCRIPTIONS[r.kind],
        `missing description for reaction kind "${r.kind}"`,
      ).toBeTruthy();
    }
  });

  it("has no descriptions for kinds outside the taxonomy", () => {
    const known = new Set(USER_REACTION_KINDS.map((r) => r.kind));
    for (const kind of Object.keys(REACTION_DESCRIPTIONS)) {
      expect(known.has(kind), `stale description for "${kind}"`).toBe(true);
    }
  });
});

describe("ReactionLegend — touch-gesture description coverage", () => {
  it("has a description for every touch gesture in the taxonomy", () => {
    for (const kind of Object.keys(TOUCH_GESTURE_LABELS)) {
      expect(
        TOUCH_DESCRIPTIONS[kind],
        `missing description for touch gesture "${kind}"`,
      ).toBeTruthy();
    }
  });

  it("has no descriptions for gestures outside the taxonomy", () => {
    const known = new Set(Object.keys(TOUCH_GESTURE_LABELS));
    for (const kind of Object.keys(TOUCH_DESCRIPTIONS)) {
      expect(known.has(kind), `stale gesture description for "${kind}"`).toBe(true);
    }
  });
});
