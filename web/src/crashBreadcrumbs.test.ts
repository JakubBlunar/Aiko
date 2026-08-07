/**
 * Tests for the always-on breadcrumb trail.
 *
 * The trail's job is to survive the conditions a crash creates: a
 * reconnect loop hammering the same event, a circular object handed to
 * a logger, a console patched while React is mid-teardown. So most of
 * these are about *not losing the useful crumbs* and *never throwing*,
 * rather than about the happy path.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import {
  BREADCRUMB_CAPACITY,
  __resetBreadcrumbsForTests,
  addBreadcrumb,
  breadcrumbCount,
  installConsoleBreadcrumbs,
  snapshotBreadcrumbs,
} from "./crashBreadcrumbs";

describe("addBreadcrumb", () => {
  beforeEach(() => {
    __resetBreadcrumbsForTests();
  });

  it("records category, message and a relative timestamp", () => {
    addBreadcrumb("ws", "open");
    const [crumb] = snapshotBreadcrumbs();
    expect(crumb.cat).toBe("ws");
    expect(crumb.msg).toBe("open");
    expect(typeof crumb.t).toBe("number");
  });

  it("keeps the trail in the order things happened", () => {
    addBreadcrumb("a", "first");
    addBreadcrumb("b", "second");
    addBreadcrumb("c", "third");
    expect(snapshotBreadcrumbs().map((c) => c.msg)).toEqual([
      "first",
      "second",
      "third",
    ]);
  });

  it("serialises an object detail", () => {
    addBreadcrumb("ws", "close", { code: 1006, reason: "abnormal" });
    expect(snapshotBreadcrumbs()[0].detail).toContain("1006");
  });

  it("summarises an Error detail without its stack", () => {
    addBreadcrumb("api", "failed", new TypeError("network down"));
    expect(snapshotBreadcrumbs()[0].detail).toBe("TypeError: network down");
  });

  it("survives a circular object", () => {
    const circular: Record<string, unknown> = { a: 1 };
    circular.self = circular;
    expect(() => addBreadcrumb("x", "circular", circular)).not.toThrow();
    expect(breadcrumbCount()).toBe(1);
  });

  it("survives a getter that throws", () => {
    const hostile = {
      get boom() {
        throw new Error("nope");
      },
    };
    expect(() => addBreadcrumb("x", "hostile", hostile)).not.toThrow();
    expect(breadcrumbCount()).toBe(1);
  });

  it("clips a long detail", () => {
    addBreadcrumb("api", "body", "q".repeat(5000));
    const detail = snapshotBreadcrumbs()[0].detail as string;
    expect(detail.length).toBeLessThan(400);
    expect(detail).toContain("+");
  });

  it("drops the oldest crumbs once full", () => {
    for (let i = 0; i < BREADCRUMB_CAPACITY + 20; i += 1) {
      addBreadcrumb("n", `crumb-${i}`);
    }
    const crumbs = snapshotBreadcrumbs();
    expect(crumbs).toHaveLength(BREADCRUMB_CAPACITY);
    expect(crumbs[0].msg).toBe("crumb-20");
    expect(crumbs[crumbs.length - 1].msg).toBe(
      `crumb-${BREADCRUMB_CAPACITY + 19}`,
    );
  });

  it("folds a rapid repeat into a count instead of flooding", () => {
    // A reconnect loop or a per-frame warning would otherwise evict the
    // whole trail within a second, which is exactly when you need it.
    for (let i = 0; i < 30; i += 1) addBreadcrumb("ws", "error");
    const crumbs = snapshotBreadcrumbs();
    expect(crumbs).toHaveLength(1);
    expect(crumbs[0].count).toBe(30);
  });

  it("does not fold two different messages together", () => {
    addBreadcrumb("ws", "open");
    addBreadcrumb("ws", "close");
    expect(snapshotBreadcrumbs()).toHaveLength(2);
  });

  it("does not fold repeats that differ only by detail", () => {
    addBreadcrumb("api", "GET /x", "500");
    addBreadcrumb("api", "GET /x", "503");
    expect(snapshotBreadcrumbs()).toHaveLength(2);
  });

  it("stops folding once the window has passed", () => {
    vi.useFakeTimers();
    const spy = vi.spyOn(performance, "now");
    spy.mockReturnValue(0);
    addBreadcrumb("ws", "error");
    spy.mockReturnValue(5000);
    addBreadcrumb("ws", "error");
    expect(snapshotBreadcrumbs()).toHaveLength(2);
    spy.mockRestore();
    vi.useRealTimers();
  });

  it("returns a copy so callers cannot mutate the live trail", () => {
    addBreadcrumb("a", "one");
    snapshotBreadcrumbs().push({ t: 0, cat: "fake", msg: "injected" });
    expect(breadcrumbCount()).toBe(1);
  });
});

describe("installConsoleBreadcrumbs", () => {
  beforeEach(() => {
    __resetBreadcrumbsForTests();
  });

  afterEach(() => {
    __resetBreadcrumbsForTests();
  });

  it("captures console.error", () => {
    const original = vi.spyOn(console, "error").mockImplementation(() => {});
    installConsoleBreadcrumbs();
    console.error("React does not recognize the prop");
    const crumbs = snapshotBreadcrumbs();
    expect(crumbs[0].cat).toBe("console");
    expect(crumbs[0].msg).toContain("React does not recognize");
    original.mockRestore();
  });

  it("captures console.warn too", () => {
    const original = vi.spyOn(console, "warn").mockImplementation(() => {});
    installConsoleBreadcrumbs();
    console.warn("deprecated");
    expect(snapshotBreadcrumbs()[0].msg).toContain("warn: deprecated");
    original.mockRestore();
  });

  it("still calls through to the real console", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    installConsoleBreadcrumbs();
    console.error("passthrough");
    expect(spy).toHaveBeenCalledWith("passthrough");
    spy.mockRestore();
  });

  it("keeps extra console arguments as detail", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    installConsoleBreadcrumbs();
    console.error("the above error occurred in", "<Live2DAvatar>");
    expect(snapshotBreadcrumbs()[0].detail).toContain("Live2DAvatar");
    spy.mockRestore();
  });

  it("is idempotent, so a crumb is never recorded twice", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    installConsoleBreadcrumbs();
    installConsoleBreadcrumbs();
    console.error("once");
    expect(snapshotBreadcrumbs()).toHaveLength(1);
    spy.mockRestore();
  });

  it("records the crumb even when the underlying console throws", () => {
    const spy = vi.spyOn(console, "error").mockImplementation(() => {
      throw new Error("console is gone");
    });
    installConsoleBreadcrumbs();
    // The throw propagates (we must not swallow the caller's console
    // semantics), but the breadcrumb is already recorded by then.
    expect(() => console.error("last words")).toThrow();
    expect(snapshotBreadcrumbs()[0].msg).toContain("last words");
    spy.mockRestore();
  });
});
