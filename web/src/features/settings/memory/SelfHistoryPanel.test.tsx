import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * L19: the Story sub-tab shows the arc that ``recall_self_history`` hands
 * the model -- the same payload, so what Aiko *would* say about her past
 * is inspectable before she says it.
 *
 * Vitest runs under Node without jsdom (see ``vitest.config.ts``), so we
 * lock in the wiring with source checks rather than rendering.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(
  resolve(here, "SelfHistoryPanel.tsx"),
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

describe("SelfHistoryPanel wiring", () => {
  it("declares the panel function", () => {
    expect(panelSource).toMatch(/function\s+SelfHistoryPanel\s*\(/);
  });

  it("is mounted in the Memory tab as a Story sub-tab", () => {
    expect(memoryTabSource).toMatch(/<SelfHistoryPanel\s*\/>/);
    expect(memoryTabSource).toMatch(/id:\s*"story"/);
  });

  it("fetches the arc from the self-history endpoint", () => {
    expect(panelSource).toMatch(/api\.getSelfHistory\s*\(/);
    expect(apiSource).toMatch(/\/api\/concepts\/self-history/);
  });

  it("can switch between her history and her read on the user", () => {
    expect(panelSource).toMatch(/setSubject/);
    expect(panelSource).toMatch(/"aiko"/);
    expect(panelSource).toMatch(/"user"/);
  });

  it("surfaces thin_record, the one field the caller must honour", () => {
    expect(panelSource).toMatch(/thin_record/);
    expect(panelSource).toMatch(/no record/i);
  });

  it("renders eras with the change, its prior wording, and its reason", () => {
    expect(panelSource).toMatch(/era\.entries/);
    expect(panelSource).toMatch(/prior_label/);
    expect(panelSource).toMatch(/because/);
    expect(panelSource).toMatch(/absorbed_labels/);
    expect(panelSource).toMatch(/whitespace-pre-wrap break-words/);
    expect(panelSource).not.toMatch(/\btruncate\b/);
    expect(panelSource).not.toMatch(/line-clamp/);
  });

  it("distinguishes every classification the builder can emit", () => {
    for (const change of [
      "flipped",
      "faded",
      "revived",
      "born",
      "settled",
    ]) {
      expect(panelSource).toContain(change);
    }
  });

  it("is read-only: the record is not editable from the UI", () => {
    expect(panelSource).not.toMatch(/api\.deleteConcept/);
    expect(panelSource).not.toMatch(/api\.updateConcept/);
  });

  it("api module exposes the self-history call", () => {
    expect(apiSource).toMatch(/getSelfHistory/);
  });

  it("types module declares the arc shapes", () => {
    expect(typesSource).toMatch(/interface SelfHistoryEntry/);
    expect(typesSource).toMatch(/interface SelfHistoryEra/);
    expect(typesSource).toMatch(/interface SelfHistoryArc/);
    expect(typesSource).toMatch(/thin_record/);
  });
});
