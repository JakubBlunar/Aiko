import { beforeEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { useWorkStore } from "./useWorkStore";
import { useTasksStore } from "@/stores/useTasksStore";
import { workCanDock } from "./workLayout";
import { mergeMessages } from "./useWorkController";
import type { TaskSnapshot, WorkDetail } from "@/types";
import WorkDrawer, { WorkUsage } from "./WorkDrawer";

const data = vi.hoisted(() => ({ detail: null as WorkDetail | null }));
vi.mock("./useWorkController", async (original) => {
  const actual = await original<typeof import("./useWorkController")>();
  return { ...actual, useWorkController: () => ({
    jobs: data.detail ? [data.detail.task] : [], detail: data.detail,
    error: "", busy: false, hasMore: false, refresh: vi.fn(), act: vi.fn(), earlier: vi.fn(),
  }) };
});

function task(overrides: Partial<TaskSnapshot> = {}): TaskSnapshot {
  return { id: 1, user_id: "u", handler_name: "goal_workflow", title: "Inspect orchard",
    status: "paused", progress: null, last_message: null, initiated_by: "aiko", args: {},
    input_request: null, result: null, error: null, notify_aiko: true, visible_to_user: true,
    created_at: "2026-11-07T09:00:00Z", updated_at: "2026-11-07T09:00:00Z",
    completed_at: null, metadata: null,
    work: { contract: { objective: "Inspect orchard", criteria: [], constraints: [] },
      profile: "reader", plan: [], notes: "", iteration: 1, children: 0,
      active_seconds: 0, tokens: 0, limits: {} }, ...overrides };
}

beforeEach(() => {
  useWorkStore.setState({ open: true, selected: 1, drafts: {} });
  data.detail = { task: task(), children: [], messages: [], oldest_event_id: null, has_older: false };
});

describe("work drawer", () => {
  it("docks only with room for the unchanged avatar and usable chat", () => {
    expect(workCanDock(1920, false, false, 420, 420)).toBe(true);
    expect(workCanDock(1366, false, false, 420, 420)).toBe(false);
    expect(workCanDock(1366, true, true, 420, 420)).toBe(true);
    expect(workCanDock(390, true, true, 420, 320)).toBe(false);
  });
  it("offers real resume/cancel controls and separates guidance from discussion", () => {
    const html = renderToStaticMarkup(<WorkDrawer open mobile={false} docked width={420} />);
    expect(html).toContain("Resume");
    expect(html).toContain("Cancel work");
    expect(html).toContain("Ask about work");
    expect(html).toContain("Guide next step");
    expect(html).toContain("does not run operations");
  });
  it("makes a mobile dialog and answers the actual awaiting child question", () => {
    data.detail!.children = [task({ id: 2, parent_task_id: 1, status: "awaiting_input",
      input_request: { prompt: "May I save orchard.txt?", options: ["approve", "deny"] } })];
    const html = renderToStaticMarkup(<WorkDrawer open mobile docked={false} width={420} />);
    expect(html).toContain('role="dialog"');
    expect(html).toContain('aria-modal="true"');
    expect(html).toContain("May I save orchard.txt?");
    expect(html).toContain("approve");
    expect(html).toContain('aria-label="Answer Aiko"');
    expect(html).not.toContain("Guide next step");
  });
  it("keeps per-job drafts and selected work through close/reopen", () => {
    const store = useWorkStore.getState();
    store.draft(1, "Inspect the older source too");
    store.show(2); store.draft(2, "Second job"); store.close(); store.show(1);
    expect(useWorkStore.getState().drafts).toEqual({ 1: "Inspect the older source too", 2: "Second job" });
    expect(useWorkStore.getState().selected).toBe(1);
  });
  it("merges history pages by durable event ID without duplicate messages", () => {
    const row = (id: number, content: string) => ({ id, task_id: 1, type: "work_message",
      data: { content }, created_at: "2026-11-07T09:00:00Z" });
    expect(mergeMessages([row(2, "old"), row(1, "first")], [row(2, "new"), row(3, "last")])
      .map((m) => m.data!.content)).toEqual(["first", "new", "last"]);
  });
  it("shows model waiting separately from active calls and distinguishes measured context", () => {
    data.detail!.task.phase = "waiting_model";
    data.detail!.task.work = {
      contract: { objective: "Inspect source", criteria: [], constraints: [] },
      profile: "browser", plan: [], notes: "", iteration: 2, children: 1,
      active_seconds: 3, queue_seconds: 18, tokens: 300,
      limits: { iterations: 32, tokens: 40000, active_seconds: 1800 },
      context_budget: { stage: "planner", context_window: 65536, estimated_input_tokens: 900,
        actual_input_tokens: 1000, input_limit: 55000, answer_reserved: 384,
        reasoning_reserved: 0, safety_reserved: 8192 },
    };
    const html = renderToStaticMarkup(<WorkDrawer open mobile={false} docked width={420} />);
    expect(html).toContain("Waiting for the shared model");
    const usage = renderToStaticMarkup(<WorkUsage work={data.detail!.task.work!} />);
    expect(usage).toContain("18s waiting for the model");
    expect(usage).toContain("1000 reported");
    expect(usage).toContain("This total is separate from each request");
  });
  it("task updates preserve history totals and pagination", () => {
    const store = useTasksStore.getState();
    store.setTasksPage({ tasks: [task()], total: 20, page: 2, pageSize: 5, enabled: true });
    store.applyTaskUpdated(task({ phase: "scheduled" }));
    store.applyTaskUpdated(task({ phase: "user_pause" }));
    const view = useTasksStore.getState().tasksView;
    expect(view.total).toBe(20);
    expect(view.page).toBe(2);
    expect(view.historyOrder).toEqual([1]);
    expect(view.tasksById[1].phase).toBe("user_pause");
  });
});
