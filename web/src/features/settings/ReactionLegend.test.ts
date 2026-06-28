import { describe, expect, it } from "vitest";

import { USER_REACTION_KINDS } from "@/types";
import { REACTION_DESCRIPTIONS } from "./ReactionLegend";

describe("ReactionLegend — description coverage", () => {
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
