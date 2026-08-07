import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * L30: the Hypotheses panel is the only place the whole shelf is visible.
 * The Aiko-facing read (``GET /api/concepts/hypotheses``) drops closed and
 * linked rows on purpose, so the panel reads ``/hypothesis-shelf`` instead
 * — and if it ever gets pointed back at the narrow read, the four
 * "nothing is happening" states become indistinguishable again.
 *
 * Vitest runs under Node without jsdom (see ``vitest.config.ts``), so we
 * lock in the wiring with source checks rather than rendering.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(
  resolve(here, "HypothesesPanel.tsx"),
  "utf-8",
);
const cardSource = readFileSync(resolve(here, "HypothesisCard.tsx"), "utf-8");
const memoryTabSource = readFileSync(
  resolve(here, "..", "MemoryTab.tsx"),
  "utf-8",
);
const apiSource = readFileSync(
  resolve(here, "..", "..", "..", "api.ts"),
  "utf-8",
);

describe("HypothesesPanel wiring", () => {
  it("declares the panel function", () => {
    expect(panelSource).toMatch(/function\s+HypothesesPanel\s*\(/);
  });

  it("is mounted in the Memory tab", () => {
    expect(memoryTabSource).toMatch(/<HypothesesPanel\s*\/>/);
    expect(memoryTabSource).toMatch(/id:\s*"hypotheses"/);
  });

  it("reads the debug shelf, not the Aiko-facing list", () => {
    expect(panelSource).toMatch(/api\.getHypothesisShelf\s*\(/);
    expect(panelSource).not.toMatch(/api\.getHypotheses\b/);
  });

  it("wires the two run buttons and both write actions", () => {
    expect(panelSource).toMatch(/api\.runHypothesisProposer\s*\(/);
    expect(panelSource).toMatch(/api\.runHypothesisAsk\s*\(/);
    expect(panelSource).toMatch(/api\.forceHypothesisVerdict\s*\(/);
    expect(panelSource).toMatch(/api\.deleteHypothesis\s*\(/);
  });

  it("offers the three forceable verdicts and not unclear", () => {
    expect(cardSource).toMatch(/"confirm", "correct", "deny"/);
    expect(cardSource).not.toMatch(/"unclear"/);
  });

  /**
   * A confirm without text writes no answer memory, and a graduated
   * concept inherits exactly those memories as its evidence — so the
   * apply button has to stay blocked rather than quietly minting a
   * concept that rests on nothing.
   */
  it("blocks a confirm with no answer text", () => {
    expect(cardSource).toMatch(/confirmBlocked/);
    expect(cardSource).toMatch(/disabled=\{busy \|\| confirmBlocked\}/);
  });

  it("puts delete behind a confirm and says what it does not touch", () => {
    expect(panelSource).toMatch(/window\.confirm/);
    expect(panelSource).toMatch(/No memory or concept is/);
  });

  it("wraps text instead of truncating", () => {
    expect(panelSource).toMatch(/whitespace-pre-wrap break-words|break-words/);
    expect(cardSource).toMatch(/whitespace-pre-wrap break-words/);
    for (const source of [panelSource, cardSource]) {
      expect(source).not.toMatch(/\btruncate\b/);
      expect(source).not.toMatch(/line-clamp/);
    }
  });

  /**
   * ``hypothesis_max_open`` is 12 and the rows carry no resolved source
   * text, so the whole shelf is one small fetch. Paging here would be
   * machinery with nothing to divide.
   */
  it("fetches the shelf whole, without paging", () => {
    expect(panelSource).not.toMatch(/PAGE_SIZE/);
    expect(panelSource).not.toMatch(/pageCount/);
  });

  it("reports the state against the caps", () => {
    expect(panelSource).toMatch(/state\.max_open/);
    expect(panelSource).toMatch(/state\.by_status/);
    expect(panelSource).toMatch(/state\.linked/);
  });

  /**
   * Both worker ``enabled_provider``s re-read ``settings.agent`` every
   * tick, so these two take effect live. Everything else numeric is
   * captured at worker construction, which is why the panel says so
   * rather than offering a control that would appear to work.
   */
  it("toggles only the two switches that take effect live", () => {
    expect(panelSource).toMatch(/hypothesis_invention_enabled/);
    expect(panelSource).toMatch(/concept_hypothesis_ask_enabled/);
    expect(panelSource).toMatch(/api\.patchSettings\s*\(/);
    expect(panelSource).toMatch(/restart to change/);
  });

  it("keeps the grounded half read-only", () => {
    expect(panelSource).toMatch(/read-only/);
    expect(cardSource).toMatch(/function\s+GroundedCard\s*\(/);
    // No verdict or delete control on a row that has no row of its own.
    expect(cardSource).not.toMatch(
      /GroundedCard[\s\S]*api\.forceHypothesisVerdict/,
    );
  });

  it("api module exposes the five hypothesis endpoints", () => {
    expect(apiSource).toMatch(/getHypothesisShelf/);
    expect(apiSource).toMatch(/runHypothesisProposer/);
    expect(apiSource).toMatch(/runHypothesisAsk/);
    expect(apiSource).toMatch(/forceHypothesisVerdict/);
    expect(apiSource).toMatch(/deleteHypothesis/);
    expect(apiSource).toMatch(/\/api\/concepts\/hypothesis-shelf/);
    expect(apiSource).toMatch(/\/api\/concepts\/hypotheses\/ask/);
    expect(apiSource).toMatch(/verdict/);
  });
});
