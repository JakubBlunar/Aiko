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
 */
const here = dirname(fileURLToPath(import.meta.url));
const settingsSource = readFileSync(
  resolve(here, "SettingsDrawer.tsx"),
  "utf-8",
);
const apiSource = readFileSync(
  resolve(here, "..", "api.ts"),
  "utf-8",
);

describe("CuriositySeedsPanel wiring", () => {
  it("declares the panel function", () => {
    expect(settingsSource).toMatch(
      /function\s+CuriositySeedsPanel\s*\(/,
    );
  });

  it("is mounted in the Memory tab next to the other panels", () => {
    expect(settingsSource).toMatch(/<CuriositySeedsPanel\s*\/>/);
  });

  it("fetches seeds with kind=curiosity_seed", () => {
    expect(settingsSource).toMatch(
      /kind:\s*"curiosity_seed"/,
    );
  });

  it("offers a 'regenerate now' control wired to runCuriositySeedWorker", () => {
    expect(settingsSource).toMatch(/regenerate now/i);
    expect(settingsSource).toMatch(/runCuriositySeedWorker/);
  });

  it("api module exposes runCuriositySeedWorker hitting the right endpoint", () => {
    expect(apiSource).toMatch(/runCuriositySeedWorker/);
    expect(apiSource).toMatch(/\/api\/curiosity-seeds\/run/);
  });
});
