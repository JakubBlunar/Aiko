/**
 * Chunk 14 — tests for the ``TaskStrip`` chip surface.
 *
 * The vitest config runs in Node with no jsdom + no React DOM
 * client renderer; we follow the codebase convention for testing
 * Zustand-coupled components:
 *
 *   * The inner ``TaskChip`` is exported as a pure prop-driven
 *     component and rendered via ``react-dom/server`` to walk the
 *     real HTML output — same approach as ``TogetherTab.test.tsx``.
 *   * The outer ``TaskStrip`` reads from the store + schedules an
 *     interval, so we lock its wiring with source-level regex
 *     assertions — same approach as ``PersonaActionBanner.test.tsx``
 *     and ``ChatView.reactions.test.tsx``.
 *
 * Together this catches the contracts that matter:
 *   - status-driven rendering branches (running / awaiting / done /
 *     failed) and their affordances (progress bar / option buttons /
 *     free-text input / cancel / dismiss)
 *   - REST wiring (``api.cancelTask`` + ``api.answerTask``)
 *   - the 1 Hz sweep with the ``TASK_STRIP_FADE_MS`` budget
 *   - data-testid for end-to-end pinning
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";

import type { TaskSnapshot } from "@/types";
import { TaskChip } from "./TaskStrip";

const here = dirname(fileURLToPath(import.meta.url));
const stripSource = readFileSync(resolve(here, "TaskStrip.tsx"), "utf-8");

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

function renderChip(task: TaskSnapshot): string {
  return renderToStaticMarkup(
    <TaskChip
      task={task}
      onCancel={() => {}}
      onAnswer={() => {}}
      onDismiss={() => {}}
    />,
  );
}

describe("TaskChip — rendering branches", () => {
  it("renders a running task with title + progress bar + percentage", () => {
    const html = renderChip(
      makeTask({ title: "Searching meeting notes", progress: 0.42 }),
    );
    expect(html).toContain("Searching meeting notes");
    expect(html).toContain("running");
    expect(html).toContain("42%");
    expect(html).toContain("progressbar");
    expect(html).toMatch(/width:\s*42%/);
  });

  it("falls back to handler_name when title is empty", () => {
    const html = renderChip(
      makeTask({ handler_name: "noisy_handler", title: "" }),
    );
    expect(html).toContain("noisy_handler");
  });

  it("renders a cancel affordance for running tasks", () => {
    const html = renderChip(makeTask({ status: "running" }));
    expect(html).toContain("Cancel task");
    expect(html).toContain("cancel");
    expect(html).not.toContain("Dismiss task");
  });

  it("renders a cancel affordance for awaiting_input tasks", () => {
    const html = renderChip(
      makeTask({
        status: "awaiting_input",
        input_request: { prompt: "pick one", options: ["a", "b"] },
      }),
    );
    expect(html).toContain("Cancel task");
  });

  it("renders option buttons when input_request.options is non-empty", () => {
    const html = renderChip(
      makeTask({
        status: "awaiting_input",
        title: "Pick a file",
        input_request: {
          prompt: "Which one?",
          options: ["recent.md", "draft.md", "notes.md"],
        },
      }),
    );
    expect(html).toContain("Which one?");
    expect(html).toContain("recent.md");
    expect(html).toContain("draft.md");
    expect(html).toContain("notes.md");
    expect(html).not.toMatch(/placeholder="answer/);
  });

  it("renders a free-text input when options is null / empty", () => {
    const html = renderChip(
      makeTask({
        status: "awaiting_input",
        input_request: { prompt: "Anything to add?", options: null },
      }),
    );
    expect(html).toContain("Anything to add?");
    expect(html).toMatch(/placeholder="answer/);
  });

  it("renders error text on a failed task (no progress bar)", () => {
    const html = renderChip(
      makeTask({
        status: "failed",
        error: "sandbox boundary violated",
        completed_at: "2026-06-07T12:02:00Z",
      }),
    );
    expect(html).toContain("sandbox boundary violated");
    expect(html).toContain("failed");
    expect(html).not.toContain("progressbar");
  });

  it("renders a dismiss (✕) affordance on terminal tasks", () => {
    const doneHtml = renderChip(
      makeTask({
        status: "done",
        completed_at: "2026-06-07T12:02:00Z",
      }),
    );
    expect(doneHtml).toContain("Dismiss task");
    expect(doneHtml).not.toContain("Cancel task");

    const cancelledHtml = renderChip(
      makeTask({
        status: "cancelled",
        completed_at: "2026-06-07T12:02:00Z",
      }),
    );
    expect(cancelledHtml).toContain("Dismiss task");
  });

  it("renders result.summary as the subtitle on done tasks", () => {
    const html = renderChip(
      makeTask({
        status: "done",
        result: { summary: "found 3 files" },
        completed_at: "2026-06-07T12:02:00Z",
      }),
    );
    expect(html).toContain("found 3 files");
  });

  it("tags the chip with data-task-id and data-task-status", () => {
    const html = renderChip(makeTask({ id: 42, status: "awaiting_input" }));
    expect(html).toContain('data-task-id="42"');
    expect(html).toContain('data-task-status="awaiting_input"');
  });
});

describe("TaskStrip — source-level wiring", () => {
  it("calls api.answerTask when a user clicks an option / submits free text", () => {
    expect(stripSource).toMatch(/api\.answerTask\(task\.id,/);
  });

  it("calls api.cancelTask on the cancel affordance", () => {
    expect(stripSource).toMatch(/api\.cancelTask\(task\.id\)/);
  });

  it("schedules a 1 Hz sweep so terminal chips fade after TASK_STRIP_FADE_MS", () => {
    expect(stripSource).toMatch(
      /export const TASK_STRIP_FADE_MS\s*=\s*20_000/,
    );
    expect(stripSource).toMatch(
      /setInterval\(\s*\(\)\s*=>\s*\{[\s\S]*?sweepRecentlyCompletedTasks\(TASK_STRIP_FADE_MS\)/,
    );
  });

  it("subscribes to the store via individual selectors (no whole-state reads)", () => {
    expect(stripSource).toMatch(
      /useAssistantStore\(\(s\)\s*=>\s*s\.tasksView\.tasksById\)/,
    );
    expect(stripSource).toMatch(
      /useAssistantStore\(\(s\)\s*=>\s*s\.tasksView\.activeIds\)/,
    );
    expect(stripSource).toMatch(
      /useAssistantStore\(\s*\(s\)\s*=>\s*s\.dismissTaskFromStrip,?\s*\)/,
    );
    expect(stripSource).toMatch(
      /useAssistantStore\(\s*\(s\)\s*=>\s*s\.sweepRecentlyCompletedTasks,?\s*\)/,
    );
  });

  it("data-testid pins the strip container so e2e tests can find it", () => {
    expect(stripSource).toMatch(/data-testid="task-strip"/);
  });

  it("renders null when no tasks are active (the strip is hidden)", () => {
    expect(stripSource).toMatch(/if\s*\(tasks\.length\s*===\s*0\)\s*return null/);
  });
});
