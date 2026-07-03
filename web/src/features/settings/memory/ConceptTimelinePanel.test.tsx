import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * The Memory tab's ConceptTimelinePanel is a read-only, day-grouped feed
 * of Aiko's concept discoveries ("aha!" moments). It fetches
 * ``GET /api/concepts/timeline`` on mount and renders event cards with a
 * novelty bar, generated reason, and timestamps.
 *
 * Vitest runs under Node without jsdom, so we lock in the wiring with
 * source checks rather than rendering.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(
  resolve(here, "ConceptTimelinePanel.tsx"),
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

describe("ConceptTimelinePanel wiring", () => {
  it("declares the panel function", () => {
    expect(panelSource).toMatch(/function\s+ConceptTimelinePanel\s*\(/);
  });

  it("is mounted in the Memory tab as a Discoveries sub-tab", () => {
    expect(memoryTabSource).toMatch(/<ConceptTimelinePanel\s*\/>/);
    expect(memoryTabSource).toMatch(/id:\s*"discoveries"/);
  });

  it("fetches the timeline", () => {
    expect(panelSource).toMatch(/api\.getConceptTimeline\s*\(/);
  });

  it("renders novelty and reason, wrapping text instead of truncating", () => {
    expect(panelSource).toMatch(/novelty/);
    expect(panelSource).toMatch(/reason/);
    expect(panelSource).toMatch(/whitespace-pre-wrap break-words/);
    expect(panelSource).not.toMatch(/\btruncate\b/);
    expect(panelSource).not.toMatch(/line-clamp/);
  });

  it("is read-only (no delete / no synthesis trigger)", () => {
    expect(panelSource).not.toMatch(/api\.deleteConcept/);
    expect(panelSource).not.toMatch(/api\.runConceptSynthesis/);
  });

  it("api module exposes the timeline endpoint", () => {
    expect(apiSource).toMatch(/getConceptTimeline/);
    expect(apiSource).toMatch(/\/api\/concepts\/timeline/);
  });
});
