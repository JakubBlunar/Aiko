import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * The Cues panel — successor to the K9 curiosity-seeds panel, widened
 * to the whole ``cue_pool`` when the seven cue workers were
 * consolidated onto it.
 *
 * Vitest runs under the Node environment without jsdom (see
 * ``vitest.config.ts``), so we can't render the panel and exercise its
 * effects end-to-end. Lock in the wiring with cheap source checks
 * instead; the reducer it feeds is tested for real in
 * ``useCuePoolStore.test.ts`` and the endpoint in
 * ``tests/test_web_cue_pool_route.py``.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(resolve(here, "CuesPanel.tsx"), "utf-8");
const memoryTabSource = readFileSync(
  resolve(here, "..", "MemoryTab.tsx"),
  "utf-8",
);
const apiSource = readFileSync(
  resolve(here, "..", "..", "..", "api.ts"),
  "utf-8",
);
const socketSource = readFileSync(
  resolve(here, "..", "..", "..", "hooks", "useAssistantSocket.ts"),
  "utf-8",
);

describe("CuesPanel wiring", () => {
  it("declares the panel function", () => {
    expect(panelSource).toMatch(/function\s+CuesPanel\s*\(/);
  });

  it("is mounted in the Memory tab under its own sub-tab", () => {
    expect(memoryTabSource).toMatch(/<CuesPanel\s*\/>/);
    expect(memoryTabSource).toMatch(/id:\s*"cues"/);
  });

  it("reads the pool through api.listCuePool", () => {
    expect(panelSource).toMatch(/api\.listCuePool/);
  });

  it("filters by both cue type and state", () => {
    expect(panelSource).toMatch(/setCuePoolTypeFilter/);
    expect(panelSource).toMatch(/setCuePoolStateFilter/);
  });

  it("still offers the seed worker's on-demand run", () => {
    expect(panelSource).toMatch(/runCuriositySeedWorker/);
  });

  it("api module hits the cue-pool endpoint", () => {
    expect(apiSource).toMatch(/listCuePool/);
    expect(apiSource).toMatch(/\/api\/cue-pool/);
  });

  it("the socket routes cue_pool_updated into the pool store", () => {
    expect(socketSource).toMatch(/case "cue_pool_updated":/);
    expect(socketSource).toMatch(/applyCuePoolUpdated/);
  });
});
