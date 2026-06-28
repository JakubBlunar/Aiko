import { describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * K9: the Memory tab's CuriositySeedsPanel surfaces seeds the
 * background worker has written and exposes a "regenerate now"
 * button that hits ``POST /api/curiosity-seeds/run``.
 *
 * Vitest runs under the Node environment without jsdom (see
 * ``vitest.config.ts``), so we can't render the panel and exercise
 * its effects end-to-end. Lock in the wiring with cheap source
 * checks instead -- the runtime behaviour is covered by the Python
 * worker tests + manual verification.
 *
 * After the file-size refactor the panel lives at
 * ``settings/memory/CuriositySeedsPanel.tsx`` and is re-exported
 * back into the Memory tab; the assertions point at the new source
 * file and the MemoryTab shell.
 */
const here = dirname(fileURLToPath(import.meta.url));
const panelSource = readFileSync(
  resolve(here, "CuriositySeedsPanel.tsx"),
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

describe("CuriositySeedsPanel wiring", () => {
  it("declares the panel function", () => {
    expect(panelSource).toMatch(
      /function\s+CuriositySeedsPanel\s*\(/,
    );
  });

  it("is mounted in the Memory tab next to the other panels", () => {
    expect(memoryTabSource).toMatch(/<CuriositySeedsPanel\s*\/>/);
  });

  it("fetches seeds with kind=curiosity_seed", () => {
    expect(panelSource).toMatch(
      /kind:\s*"curiosity_seed"/,
    );
  });

  it("offers a 'regenerate now' control wired to runCuriositySeedWorker", () => {
    expect(panelSource).toMatch(/regenerate now/i);
    expect(panelSource).toMatch(/runCuriositySeedWorker/);
  });

  it("api module exposes runCuriositySeedWorker hitting the right endpoint", () => {
    expect(apiSource).toMatch(/runCuriositySeedWorker/);
    expect(apiSource).toMatch(/\/api\/curiosity-seeds\/run/);
  });
});
