/**
 * Chunk 15 — tests for ``PersonaTaskBanner``.
 *
 * Like ``TaskStrip``, the banner reads from the Zustand store, so
 * we follow the codebase convention:
 *
 *   * The pure ``pickAwaitingTask`` selector is exported and
 *     exercised directly — that's the core decision the banner
 *     makes ("which awaiting task should I surface right now?")
 *     and locking it down here keeps the rendering tests cheap.
 *   * The outer component's contracts are covered with source-
 *     level regex assertions (same pattern as
 *     ``PersonaActionBanner.test.tsx``): subscription wiring,
 *     REST call sites, master switch, layout positioning vs the
 *     touch banner, and the PersonaWindow mount.
 *
 * Why no full render assertions: the Zustand hook returns the
 * initial state during ``renderToStaticMarkup``, so a populated
 * banner can't be exercised via SSR without standing up jsdom —
 * the same constraint that drove the ``TaskStrip`` split into a
 * pure ``TaskChip`` for visual coverage.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";

import type { TaskSnapshot } from "@/types";
import { pickAwaitingTask } from "./PersonaTaskBanner";

const here = dirname(fileURLToPath(import.meta.url));
const bannerSource = readFileSync(
  resolve(here, "PersonaTaskBanner.tsx"),
  "utf-8",
);
const personaWindowSource = readFileSync(
  resolve(here, "PersonaWindow.tsx"),
  "utf-8",
);

function makeTask(overrides: Partial<TaskSnapshot> = {}): TaskSnapshot {
  return {
    id: 1,
    user_id: "jacob",
    handler_name: "file_search",
    title: "Searching files",
    status: "running",
    progress: 0,
    last_message: null,
    initiated_by: "aiko",
    args: {},
    input_request: null,
    result: null,
    error: null,
    notify_aiko: true,
    visible_to_user: true,
    created_at: "2026-06-07T12:00:00Z",
    updated_at: "2026-06-07T12:00:00Z",
    completed_at: null,
    metadata: null,
    ...overrides,
  };
}

describe("pickAwaitingTask — pure selector", () => {
  it("returns null when no tasks are active", () => {
    expect(pickAwaitingTask([], {}, new Set())).toBeNull();
  });

  it("returns null when no active task has awaiting_input status", () => {
    const t1 = makeTask({ id: 1, status: "running" });
    const t2 = makeTask({ id: 2, status: "done" });
    const result = pickAwaitingTask(
      [2, 1],
      { 1: t1, 2: t2 },
      new Set(),
    );
    expect(result).toBeNull();
  });

  it("returns the first awaiting_input task in activeIds order (newest-first)", () => {
    const t1 = makeTask({ id: 1, status: "awaiting_input" });
    const t2 = makeTask({ id: 2, status: "running" });
    const t3 = makeTask({ id: 3, status: "awaiting_input" });
    // activeIds is newest-first; t3 was added most recently so it
    // wins over t1 even though both are awaiting_input.
    const result = pickAwaitingTask(
      [3, 2, 1],
      { 1: t1, 2: t2, 3: t3 },
      new Set(),
    );
    expect(result?.id).toBe(3);
  });

  it("skips ids the user has dismissed and falls through to the next awaiting", () => {
    const t1 = makeTask({ id: 1, status: "awaiting_input" });
    const t2 = makeTask({ id: 2, status: "awaiting_input" });
    const result = pickAwaitingTask(
      [2, 1],
      { 1: t1, 2: t2 },
      new Set([2]),
    );
    expect(result?.id).toBe(1);
  });

  it("returns null when every awaiting task has been dismissed", () => {
    const t1 = makeTask({ id: 1, status: "awaiting_input" });
    const t2 = makeTask({ id: 2, status: "awaiting_input" });
    const result = pickAwaitingTask(
      [2, 1],
      { 1: t1, 2: t2 },
      new Set([1, 2]),
    );
    expect(result).toBeNull();
  });

  it("defensively skips ids that aren't in tasksById (stale activeIds)", () => {
    // ``dismissTaskFromStrip`` can race with a fresh WS event; the
    // selector must not crash on a missing entry.
    const t1 = makeTask({ id: 1, status: "awaiting_input" });
    const result = pickAwaitingTask(
      [99, 1],
      { 1: t1 },
      new Set(),
    );
    expect(result?.id).toBe(1);
  });
});

describe("PersonaTaskBanner — source-level wiring", () => {
  it("subscribes to the canonical strip selectors (not whole-state reads)", () => {
    expect(bannerSource).toMatch(
      /useAssistantStore\(\(s\)\s*=>\s*s\.tasksView\.tasksById\)/,
    );
    expect(bannerSource).toMatch(
      /useAssistantStore\(\(s\)\s*=>\s*s\.tasksView\.activeIds\)/,
    );
    expect(bannerSource).toMatch(
      /useAssistantStore\(\(s\)\s*=>\s*s\.pushToast\)/,
    );
  });

  it("delegates to the same REST endpoints as the strip (no duplicate wiring)", () => {
    expect(bannerSource).toMatch(/api\.answerTask\(task\.id,/);
    expect(bannerSource).toMatch(/api\.cancelTask\(task\.id\)/);
  });

  it("uses ``api.answerTask`` for BOTH option clicks and free-text submits", () => {
    // Both code paths must hit the same backend endpoint so the
    // orchestrator can resolve the task identically regardless of
    // surface. A regression here would split the answer protocol.
    const matches = bannerSource.match(/api\.answerTask\(/g) || [];
    expect(matches.length).toBeGreaterThanOrEqual(2);
  });

  it("returns null when the master switch is off OR no task is awaiting", () => {
    expect(bannerSource).toMatch(
      /if\s*\(!enabled\s*\|\|\s*!task\)\s*return null/,
    );
  });

  it("respects the ``enabled`` prop with a true default (parity with PersonaActionBanner)", () => {
    expect(bannerSource).toMatch(/enabled\s*=\s*true\s*,?\s*\}/);
  });

  it("tracks dismissed task ids in component state (not a permanent global)", () => {
    // Dismiss is per-session per-id — a fresh task with a
    // different id must still surface. Lock down that the storage
    // is a useState Set, not e.g. a top-level module variable.
    expect(bannerSource).toMatch(
      /useState<Set<number>>/,
    );
    expect(bannerSource).toMatch(/setDismissed\(\(prev\)/);
  });

  it("data-testid + data-task-id pin the banner for e2e tests", () => {
    expect(bannerSource).toMatch(/data-testid="persona-task-banner"/);
    expect(bannerSource).toMatch(/data-task-id=\{task\.id\}/);
  });

  it("dismiss is local-only — it never calls cancelTask on the server", () => {
    // The dismiss handler must not fire any REST request; a
    // regression that wired ``api.cancelTask`` into the X button
    // would silently kill tasks the user only wanted to hide.
    const dismissBlockMatch = bannerSource.match(
      /handleDismissClick\s*=\s*useCallback\([\s\S]*?\},\s*\[task\]\)/,
    );
    expect(dismissBlockMatch).not.toBeNull();
    expect(dismissBlockMatch?.[0]).not.toMatch(/api\./);
  });

  it("renders an option-buttons branch AND a free-text branch", () => {
    // The two render paths cover the design doc's "channel B"
    // split (structured choices vs open-ended). Both must be
    // present so a config drift doesn't silently drop one.
    expect(bannerSource).toMatch(/options\.length\s*>\s*0/);
    expect(bannerSource).toMatch(/<input[\s\S]*?placeholder="answer/);
    expect(bannerSource).toMatch(/handleFreeTextSubmit/);
  });

  it("toasts on REST failure rather than swallowing the error", () => {
    expect(bannerSource).toMatch(/pushToast\("error",/);
  });
});

describe("PersonaWindow — mount contract", () => {
  it("imports PersonaTaskBanner alongside PersonaActionBanner", () => {
    expect(personaWindowSource).toMatch(
      /import\s*\{\s*PersonaTaskBanner\s*\}\s*from\s*"\.\/PersonaTaskBanner"/,
    );
  });

  it("renders the task banner element inside the avatar container", () => {
    expect(personaWindowSource).toMatch(/<PersonaTaskBanner\s*\/>/);
  });

  it("keeps the K31 touch banner mounted (chunk 15 does not displace it)", () => {
    // The banner now receives the companion master-switch + duration
    // props (I5), so match the opening tag rather than the old
    // self-closing form.
    expect(personaWindowSource).toMatch(/<PersonaActionBanner[\s>]/);
  });
});
