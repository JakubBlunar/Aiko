import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * L17e: the Memory tab's ConceptEvolutionPanel is the history-of-thought
 * browser. It fetches ``GET /api/concepts/learning`` on mount, renders
 * one card per recorded change (old wording struck through, new wording,
 * and the "because"), and expands into a per-belief provenance
 * drill-down backed by ``GET /api/concepts/{id}/provenance``.
 *
 * Vitest runs under Node without jsdom (see ``vitest.config.ts``), so we
 * lock in the wiring with source checks rather than rendering.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(
  resolve(here, "ConceptEvolutionPanel.tsx"),
  "utf-8",
);
const memoryTabSource = readFileSync(
  resolve(here, "..", "MemoryTab.tsx"),
  "utf-8",
);
const apiSource = readFileSync(
  resolve(here, "..", "..", "..", "api.ts"),
  "utf-8",
);
const typesSource = readFileSync(
  resolve(here, "..", "..", "..", "types.ts"),
  "utf-8",
);

describe("ConceptEvolutionPanel wiring", () => {
  it("declares the panel function", () => {
    expect(panelSource).toMatch(/function\s+ConceptEvolutionPanel\s*\(/);
  });

  it("is mounted in the Memory tab as an Evolution sub-tab", () => {
    expect(memoryTabSource).toMatch(/<ConceptEvolutionPanel\s*\/>/);
    expect(memoryTabSource).toMatch(/id:\s*"evolution"/);
  });

  it("fetches the learning feed and the provenance drill-down", () => {
    expect(panelSource).toMatch(/api\.getConceptLearning\s*\(/);
    expect(panelSource).toMatch(/api\.getConceptProvenance\s*\(/);
  });

  it("renders the change itself: old, new, and because", () => {
    expect(panelSource).toMatch(/old_label/);
    expect(panelSource).toMatch(/new_label/);
    expect(panelSource).toMatch(/because/);
    expect(panelSource).toMatch(/whitespace-pre-wrap break-words/);
    expect(panelSource).not.toMatch(/\btruncate\b/);
    expect(panelSource).not.toMatch(/line-clamp/);
  });

  it("offers shape and subject filters", () => {
    expect(panelSource).toMatch(/shapeFilter/);
    expect(panelSource).toMatch(/subjectFilter/);
  });

  it("shows the wordings a belief has worn and its merge chain", () => {
    expect(panelSource).toMatch(/prior_labels/);
    expect(panelSource).toMatch(/resolved_id/);
    expect(panelSource).toMatch(/absorbed/);
  });

  it("handles a concept that no longer exists", () => {
    expect(panelSource).toMatch(/no longer exists/);
  });

  it("never deletes: history is append-only", () => {
    expect(panelSource).not.toMatch(/api\.deleteConcept/);
  });

  it("api module exposes the L17 endpoints", () => {
    expect(apiSource).toMatch(/getConceptLearning/);
    expect(apiSource).toMatch(/getConceptProvenance/);
    expect(apiSource).toMatch(/getConceptDriftState/);
    expect(apiSource).toMatch(/runConceptDrift/);
    expect(apiSource).toMatch(/\/api\/concepts\/learning/);
    expect(apiSource).toMatch(/\/provenance/);
  });

  it("types module declares the learning shapes", () => {
    expect(typesSource).toMatch(/interface ConceptLearningEvent/);
    expect(typesSource).toMatch(/interface ConceptLearningFeed/);
    expect(typesSource).toMatch(/interface ConceptProvenance/);
    expect(typesSource).toMatch(/interface ConceptDriftState/);
  });
});
