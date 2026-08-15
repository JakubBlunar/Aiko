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

  /**
   * The snapshot resolves every evidence edge to its full untruncated
   * source text, so an unpaged fetch grew to megabytes and hundreds of
   * cards in a single commit — enough to freeze the app on a phone. The
   * page must stay bounded, and paging has to happen on the server so it
   * walks the filtered set rather than hiding rows from a whole graph
   * that was already fetched.
   */
  it("fetches a bounded page rather than the whole graph", () => {
    expect(panelSource).toMatch(/CONCEPT_PAGE_SIZE\s*=\s*\d+/);
    expect(panelSource).toMatch(/limit:\s*CONCEPT_PAGE_SIZE/);
    expect(panelSource).toMatch(/offset:\s*page\s*\*\s*CONCEPT_PAGE_SIZE/);
  });

  it("filters server-side and resets to the first page", () => {
    expect(panelSource).toMatch(/status:\s*statusFilter\s*===\s*STATUS_ALL/);
    expect(panelSource).toMatch(/subject:\s*subjectFilter\s*===\s*SUBJECT_ALL/);
    expect(panelSource).toMatch(/changeFilter/);
    // No client-side re-filter of an already-fetched list.
    expect(panelSource).not.toMatch(/concepts\.filter\s*\(/);
  });

  it("offers Prev/Next once there is more than one page", () => {
    expect(panelSource).toMatch(/pageCount > 1/);
    expect(panelSource).toMatch(/setPage\(page - 1\)/);
    expect(panelSource).toMatch(/setPage\(page \+ 1\)/);
    expect(panelSource).toMatch(/page \{page \+ 1\} of \{pageCount\}/);
  });

  it("api module passes paging and filters through", () => {
    expect(apiSource).toMatch(/search\.set\("limit"/);
    expect(apiSource).toMatch(/search\.set\("offset"/);
    expect(apiSource).toMatch(/search\.set\("status"/);
    expect(apiSource).toMatch(/search\.set\("subject"/);
    expect(apiSource).toMatch(/search\.set\("kind"/);
    expect(apiSource).toMatch(/search\.set\("q"/);
  });

  /**
   * Kind and text search go to the server for the same reason status and
   * subject do — a client-side filter over one fetched page would search
   * 50 of however many concepts exist, and answer "no matches" for rows
   * that are sitting two pages away.
   */
  it("sends kind and the search box to the server", () => {
    expect(panelSource).toMatch(/kind:\s*kindFilter\s*===\s*KIND_ALL/);
    expect(panelSource).toMatch(/q:\s*query\.trim\(\)/);
    expect(panelSource).toMatch(/useDebouncedValue/);
  });

  it("resets to the first page when the search settles", () => {
    // The pill handlers reset the page themselves, but the debounced
    // query reaches the loader a render later and cannot.
    expect(panelSource).toMatch(/setPage\(0\);\s*\}, \[query\]\)/);
  });

  it("builds the kind options from the whole-store tally", () => {
    expect(panelSource).toMatch(/counts\.by_kind/);
  });
});
