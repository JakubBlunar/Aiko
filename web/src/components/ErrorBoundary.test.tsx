/**
 * Source-level wiring tests for the I8 ``ErrorBoundary``.
 *
 * The vitest config runs in Node with no jsdom, so the class component
 * can't be mounted; we lock in the contract by inspecting the source
 * (same approach as ``PersonaActionBanner.test.tsx``). The assertions
 * cover the bits a refactor could silently break:
 *
 *   1. It's a real React error boundary — implements both
 *      ``getDerivedStateFromError`` (to flip into the fallback) and
 *      ``componentDidCatch`` (to report).
 *   2. The caught error is reported through ``reportUiCrash`` with
 *      ``source: "render"`` so it lands in the backend log unconditionally.
 *   3. The fallback offers a reload affordance (``location.reload``) and
 *      a recover (reset state) path.
 *   4. ``main.tsx`` wraps ``<App />`` in the boundary and installs the
 *      global crash reporters.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

const here = dirname(fileURLToPath(import.meta.url));
const boundarySource = readFileSync(resolve(here, "ErrorBoundary.tsx"), "utf-8");
const mainSource = readFileSync(resolve(here, "..", "main.tsx"), "utf-8");

describe("ErrorBoundary — error-boundary contract", () => {
  it("implements getDerivedStateFromError to enter the fallback", () => {
    expect(boundarySource).toMatch(/static\s+getDerivedStateFromError/);
  });

  it("implements componentDidCatch to capture the error", () => {
    expect(boundarySource).toMatch(/componentDidCatch\s*\(/);
  });

  it("reports the crash with source 'render'", () => {
    expect(boundarySource).toMatch(/reportUiCrash\s*\(/);
    expect(boundarySource).toMatch(/source:\s*"render"/);
  });

  it("forwards the React component stack into the report", () => {
    expect(boundarySource).toMatch(/componentStack/);
  });
});

describe("ErrorBoundary — fallback affordances", () => {
  it("offers a reload via location.reload", () => {
    expect(boundarySource).toMatch(/location\.reload\(\)/);
  });

  it("offers a recover/try-again path that clears the error state", () => {
    expect(boundarySource).toMatch(/setState\(\{\s*error:\s*null/);
  });

  it("renders children unchanged when there is no error", () => {
    expect(boundarySource).toMatch(/return this\.props\.children/);
  });
});

describe("main.tsx — wiring", () => {
  it("wraps <App /> in the ErrorBoundary", () => {
    expect(mainSource).toMatch(/<ErrorBoundary>[\s\S]*<App\s*\/>[\s\S]*<\/ErrorBoundary>/);
  });

  it("installs the global crash reporters before render", () => {
    expect(mainSource).toMatch(/installGlobalCrashReporters\(\)/);
  });
});
