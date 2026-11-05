import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { BeliefCard } from "./BeliefsPanel";
import type { Belief } from "../../../types";

function reading(overrides: Partial<Belief> = {}): Belief {
  return {
    id: 7, user_id: "sample", kind: "mood", topic: "Quiet company",
    predicted_state: "They may welcome company without a demanding conversation.",
    confidence: 0.95, valence: null, arousal: null, source: "worker",
    source_message_id: 3, observed_at: "2026-11-05T09:00:00Z", last_checked_at: null,
    status: "active", gap_seen_at: null,
    metadata: {
      interpretation_version: 2, reason: "They asked Aiko to stay.",
      situation: "Winding down before sleep.", uncertainty: "How much conversation they want.",
      revise_when: "They request another subject.", expires_at: "2099-01-01T00:00:00Z",
      evidence: [{ message_id: 3, quote: "I am tired, but stay with me.", created_at: "2026-11-05" }],
    }, ...overrides,
  };
}

function card(belief: Belief) {
  return renderToStaticMarkup(<BeliefCard belief={belief} onConfirm={() => {}}
    onContradict={() => {}} onDelete={() => {}} onCorrect={async () => {}} />);
}

describe("Situation-specific belief cards", () => {
  it("shows the complete interpretation, reason, scope, unknown and actual supporting words", () => {
    const b = reading();
    const html = card(b);
    expect(html).toContain(b.predicted_state);
    expect(html).toContain("They asked Aiko to stay.");
    expect(html).toContain("Winding down before sleep.");
    expect(html).toContain("How much conversation they want.");
    expect(html).toContain("I am tired, but stay with me.");
    expect(html).toContain("they do not prove it");
    expect(html).toContain("Tentative interpretation");
    expect(html).toContain("Correct wording or scope");
    expect(html).not.toContain("seen 2");
  });

  it("distinguishes a user's confirmation from the old automatic label", () => {
    expect(card(reading({ status: "confirmed" }))).not.toContain("You confirmed");
    const b = reading();
    b.status = "confirmed";
    b.metadata.confirmation_basis = "user";
    expect(card(b)).toContain("You confirmed");
  });

  it("does not display a confirmed expired feeling as current", () => {
    const b = reading();
    b.status = "confirmed";
    b.metadata.confirmation_basis = "user";
    b.metadata.expires_at = "2000-01-01T00:00:00Z";
    expect(card(b)).toContain("Out of date");
    expect(card(b)).not.toContain("You confirmed");
  });

  it("identifies legacy fragments honestly and escapes source text", () => {
    const b = reading({ metadata: {} });
    expect(card(b)).toContain("Earlier reading");
    const quoted = reading();
    quoted.metadata.evidence = [{ message_id: 3, quote: "<script>unsafe()</script>" }];
    const html = card(quoted);
    expect(html).toContain("&lt;script&gt;");
    expect(html).not.toContain("<script>");
  });
});
