import { describe, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

/**
 * The persona window MUST mount ``usePresenceReporter`` so a session
 * with only the persona window open still tells the backend whether
 * the user is here. Without this, the backend's per-client presence
 * fold treats persona-only sessions as "never reported" and the
 * boot default keeps the typed-proactive timer firing forever even
 * after the user hides the persona window.
 *
 * Vitest runs under the Node environment with no jsdom (see
 * ``vitest.config.ts``), so we can't render the persona window and
 * watch its effects fire end-to-end. Instead we lock in the wiring
 * with two complementary checks:
 *
 *   1. The hook is imported -- a typo / accidental rename in the
 *      import path surfaces here before any runtime test does.
 *   2. The hook is invoked with the WS ``send`` callback from the
 *      props -- the back-end fold relies on every connected window
 *      sending its own presence frame, so the call site has to
 *      reference the prop, not a hard-coded no-op.
 */
const here = dirname(fileURLToPath(import.meta.url));
const personaSource = readFileSync(
  resolve(here, "PersonaWindow.tsx"),
  "utf-8",
);

describe("PersonaWindow presence wiring", () => {
  it("imports usePresenceReporter from the shared hook", () => {
    expect(personaSource).toMatch(
      /import\s*\{\s*usePresenceReporter\s*\}\s*from\s*"@\/hooks\/usePresenceReporter"/,
    );
  });

  it("invokes usePresenceReporter with the WS send callback", () => {
    expect(personaSource).toMatch(
      /usePresenceReporter\(\s*\{\s*send\s*(?:\}|,)/,
    );
  });

  it("mounts the hook before the connected-state read so the first " +
    "presence frame goes out as part of the initial render pass", () => {
    // The hook calls ``flush()`` synchronously inside its first
    // ``useEffect`` so React commits an initial ``presence`` frame
    // on mount. Asserting the order in source is a cheap proxy for
    // "the hook actually mounted in the component body".
    const hookIdx = personaSource.indexOf("usePresenceReporter(");
    const renderIdx = personaSource.indexOf("const connected =");
    expect(hookIdx).toBeGreaterThan(0);
    expect(renderIdx).toBeGreaterThan(0);
    expect(hookIdx).toBeLessThan(renderIdx);
  });
});

describe("usePresenceReporter dynamic import sanity", () => {
  it("exports a function named usePresenceReporter", async () => {
    // Imported lazily to keep the static-source assertions above
    // independent of module loading. Resolution failures here would
    // mean the hook file moved or the named export disappeared --
    // both worth catching at the package level.
    vi.resetModules();
    const mod = await import("@/hooks/usePresenceReporter");
    expect(typeof mod.usePresenceReporter).toBe("function");
  });
});
