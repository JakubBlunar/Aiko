/**
 * Tests for the I8 UI crash-reporting pipeline.
 *
 * The vitest config runs in the Node environment (no jsdom), so we test
 * the pure ``buildCrashReport`` builder and the deduped/capped
 * ``reportUiCrash`` POST behaviour by stubbing ``globalThis.fetch``.
 * ``backendBase()`` returns an empty origin under Node, so the request
 * targets the relative ``/api/logs/ui-crash`` path.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  __resetBreadcrumbsForTests,
  addBreadcrumb,
} from "./crashBreadcrumbs";
import {
  __resetCrashContextForTests,
  setCrashContextProvider,
} from "./crashContext";
import {
  __resetCrashReportStateForTests,
  buildCrashReport,
  reportUiCrash,
} from "./crashReport";

describe("buildCrashReport", () => {
  it("extracts message + stack from a real Error", () => {
    const err = new Error("boom");
    const report = buildCrashReport({ error: err, source: "render" });
    expect(report.message).toBe("boom");
    expect(report.stack).toContain("Error: boom");
    expect(report.source).toBe("render");
    expect(report.ts).toMatch(/^\d{4}-\d{2}-\d{2}T/);
  });

  it("falls back to a placeholder when there is no message", () => {
    const report = buildCrashReport({ error: {}, source: "render" });
    expect(report.message).toBe("(no message)");
  });

  it("handles a thrown string", () => {
    const report = buildCrashReport({ error: "plain string throw", source: "window.onerror" });
    expect(report.message).toBe("plain string throw");
  });

  it("prefers an explicit message override", () => {
    const report = buildCrashReport({
      error: new Error("inner"),
      message: "explicit",
      source: "unhandledrejection",
    });
    expect(report.message).toBe("explicit");
  });

  it("clips oversized stacks to keep the wire payload bounded", () => {
    const report = buildCrashReport({
      message: "x",
      stack: "y".repeat(50_000),
      source: "render",
    });
    expect(report.stack).toBeDefined();
    expect((report.stack as string).length).toBeLessThan(20_000);
    expect(report.stack).toContain("more)");
  });

  it("passes componentStack through when present", () => {
    const report = buildCrashReport({
      message: "x",
      componentStack: "\n  in Live2DAvatar",
      source: "render",
    });
    expect(report.componentStack).toContain("Live2DAvatar");
  });
});

describe("buildCrashReport — diagnostics", () => {
  beforeEach(() => {
    __resetBreadcrumbsForTests();
    __resetCrashContextForTests();
  });

  it("attaches the breadcrumb trail", () => {
    addBreadcrumb("ws", "close");
    addBreadcrumb("api", "GET /api/concepts → 500");
    const report = buildCrashReport({ message: "boom", source: "render" });
    expect(report.breadcrumbs?.map((c) => c.msg)).toEqual([
      "close",
      "GET /api/concepts → 500",
    ]);
  });

  it("omits breadcrumbs entirely when there are none", () => {
    const report = buildCrashReport({ message: "boom", source: "render" });
    expect(report.breadcrumbs).toBeUndefined();
  });

  it("does not include the crash itself in its own trail", () => {
    // Otherwise every report ends with a breadcrumb restating the
    // message directly above it, which is noise.
    addBreadcrumb("ws", "open");
    const report = buildCrashReport({ message: "boom", source: "render" });
    expect(report.breadcrumbs).toHaveLength(1);
    expect(report.breadcrumbs?.[0].msg).toBe("open");
  });

  it("leaves the crash as a breadcrumb for a follow-up report", () => {
    // A cascade is common: the boundary catches, the fallback render
    // fails too. The second report should carry the first as context.
    buildCrashReport({ message: "first failure", source: "render" });
    const second = buildCrashReport({ message: "second", source: "render" });
    expect(second.breadcrumbs?.some((c) => c.msg.includes("first failure"))).toBe(
      true,
    );
  });

  it("attaches the environment context", () => {
    const report = buildCrashReport({ message: "boom", source: "render" });
    expect(report.context?.build).toBeDefined();
    expect(report.context?.shell).toBeDefined();
  });

  it("folds registered app state into the context", () => {
    setCrashContextProvider(() => ({
      voiceMode: "listening",
      connection: "connected",
      turnInProgress: true,
    }));
    const report = buildCrashReport({ message: "boom", source: "render" });
    expect(report.context?.voiceMode).toBe("listening");
    expect(report.context?.turnInProgress).toBe("true");
  });

  it("still builds a report when the state provider throws", () => {
    // The provider reads the store — which may be exactly what broke.
    setCrashContextProvider(() => {
      throw new Error("store is toast");
    });
    const report = buildCrashReport({ message: "boom", source: "render" });
    expect(report.message).toBe("boom");
    expect(report.context?.appState).toBe("(provider failed)");
  });

  it("drops empty values from the context rather than shipping blanks", () => {
    setCrashContextProvider(() => ({ empty: "", nothing: null, real: "yes" }));
    const report = buildCrashReport({ message: "boom", source: "render" });
    expect(report.context?.real).toBe("yes");
    expect(report.context).not.toHaveProperty("empty");
    expect(report.context).not.toHaveProperty("nothing");
  });
});

describe("reportUiCrash", () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    __resetCrashReportStateForTests();
    fetchMock = vi.fn(() => Promise.resolve({ ok: true } as Response));
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it("POSTs a JSON body to /api/logs/ui-crash", () => {
    reportUiCrash(buildCrashReport({ message: "boom", source: "render" }));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain("/api/logs/ui-crash");
    expect(init.method).toBe("POST");
    expect(init.keepalive).toBe(true);
    const body = JSON.parse(init.body as string);
    expect(body.message).toBe("boom");
    expect(body.source).toBe("render");
  });

  it("dedupes identical signatures within the window", () => {
    const make = () => buildCrashReport({ message: "same", source: "render" });
    reportUiCrash(make());
    reportUiCrash(make());
    reportUiCrash(make());
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not dedupe distinct messages", () => {
    reportUiCrash(buildCrashReport({ message: "one", source: "render" }));
    reportUiCrash(buildCrashReport({ message: "two", source: "render" }));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("caps the total number of reports per session", () => {
    for (let i = 0; i < 100; i += 1) {
      reportUiCrash(buildCrashReport({ message: `unique-${i}`, source: "render" }));
    }
    // MAX_REPORTS_PER_SESSION is 25.
    expect(fetchMock.mock.calls.length).toBeLessThanOrEqual(25);
  });

  it("never throws even if fetch rejects synchronously", () => {
    fetchMock.mockImplementation(() => {
      throw new Error("network layer exploded");
    });
    expect(() =>
      reportUiCrash(buildCrashReport({ message: "x", source: "render" })),
    ).not.toThrow();
  });
});
