import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * L1/L2: the Memory tab's ConceptsPanel is a debug browser over the
 * higher-order concept layer. It fetches ``GET /api/concepts`` on mount,
 * renders expandable concept rows (with evidence resolved to readable
 * labels), and offers "run synthesis" + per-concept delete.
 *
 * Vitest runs under Node without jsdom (see ``vitest.config.ts``), so we
 * lock in the wiring with source checks rather than rendering.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(
  resolve(here, "ConceptsPanel.tsx"),
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

describe("ConceptsPanel wiring", () => {
  it("declares the panel function", () => {
    expect(panelSource).toMatch(/function\s+ConceptsPanel\s*\(/);
  });

  it("is mounted in the Memory tab", () => {
    expect(memoryTabSource).toMatch(/<ConceptsPanel\s*\/>/);
    expect(memoryTabSource).toMatch(/id:\s*"concepts"/);
  });

  it("fetches concepts and wires run + delete", () => {
    expect(panelSource).toMatch(/api\.getConcepts\s*\(/);
    expect(panelSource).toMatch(/api\.runConceptSynthesis\s*\(/);
    expect(panelSource).toMatch(/api\.deleteConcept\s*\(/);
  });

  it("wraps text instead of truncating", () => {
    expect(panelSource).toMatch(/whitespace-pre-wrap break-words/);
    expect(panelSource).not.toMatch(/\btruncate\b/);
    expect(panelSource).not.toMatch(/line-clamp/);
  });

  it("confirms delete is concept-only (memories untouched)", () => {
    expect(panelSource).toMatch(/window\.confirm/);
    expect(panelSource).toMatch(/left untouched/);
  });

  it("api module exposes the three concept endpoints", () => {
    expect(apiSource).toMatch(/getConcepts/);
    expect(apiSource).toMatch(/runConceptSynthesis/);
    expect(apiSource).toMatch(/deleteConcept/);
    expect(apiSource).toMatch(/\/api\/concepts/);
    expect(apiSource).toMatch(/\/api\/concepts\/run/);
  });
});
