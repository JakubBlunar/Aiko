import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * K9: the Memory tab's TopicGraphPanel is a read-only browser over the
 * cosine-cluster topic graph. It fetches ``GET /api/topic-graph`` on
 * mount and renders expandable cluster rows.
 *
 * Vitest runs under Node without jsdom (see ``vitest.config.ts``), so
 * we lock in the wiring with source checks rather than rendering.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(
  resolve(here, "settings", "memory", "TopicGraphPanel.tsx"),
  "utf-8",
);
const memoryTabSource = readFileSync(
  resolve(here, "settings", "MemoryTab.tsx"),
  "utf-8",
);
const apiSource = readFileSync(resolve(here, "..", "api.ts"), "utf-8");

describe("TopicGraphPanel wiring", () => {
  it("declares the panel function", () => {
    expect(panelSource).toMatch(/function\s+TopicGraphPanel\s*\(/);
  });

  it("is mounted in the Memory tab next to the other panels", () => {
    expect(memoryTabSource).toMatch(/<TopicGraphPanel\s*\/>/);
  });

  it("fetches the topic graph via api.getTopicGraph", () => {
    expect(panelSource).toMatch(/api\.getTopicGraph\s*\(/);
  });

  it("renders cluster members with their kind/tier", () => {
    expect(panelSource).toMatch(/cluster\.members/);
    expect(panelSource).toMatch(/member\.kind/);
  });

  it("api module exposes getTopicGraph hitting the right endpoint", () => {
    expect(apiSource).toMatch(/getTopicGraph/);
    expect(apiSource).toMatch(/\/api\/topic-graph/);
  });
});
