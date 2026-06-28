import { beforeEach, describe, expect, it } from "vitest";

import { useTasksStore } from "./stores/useTasksStore";
import type { TaskSnapshot, TaskStatus } from "./types";

/**
 * Covers the Zustand reducers backing the chunk-14 task surfaces
 * (TaskStrip + TasksTab). The WS hook dispatches the four
 * ``applyTask*`` reducers from incoming events; exercising them
 * directly verifies the strip / history-page-aware semantics
 * without standing up the hook.
 *
 * Page-aware contract under test:
 *   - ``task_started`` always bumps ``total`` (filter-aware) +
 *     prepends to the strip; prepends to the history page only
 *     when on page 0 AND the status filter matches.
 *   - ``task_progress`` is a strict merge on the existing row; the
 *     reducer is a no-op when the task id is unknown.
 *   - ``task_input_needed`` replaces the row + ensures the strip
 *     chip is present; never bumps ``total`` (the row already
 *     existed).
 *   - ``task_completed`` replaces the row + keeps the chip on the
 *     strip; ``sweepRecentlyCompletedTasks`` is the only path that
 *     drops it.
 *   - ``sweepRecentlyCompletedTasks`` only drops terminal rows
 *     whose ``completed_at`` is older than the threshold; running
 *     rows stay regardless of how long they've been running.
 */

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

function resetTasksView(overrides?: {
  page?: number;
  pageSize?: number;
  statusFilter?: TaskStatus | null;
}) {
  // Reset by replacing the slice in place. ``setTasksPage`` works
  // for this because it fully replaces ``historyOrder`` + the
  // pagination fields.
  useTasksStore.setState((state) => ({
    tasksView: {
      ...state.tasksView,
      tasksById: {},
      activeIds: [],
      historyOrder: [],
      total: 0,
      page: overrides?.page ?? 0,
      pageSize: overrides?.pageSize ?? 50,
      statusFilter: overrides?.statusFilter ?? null,
      loading: false,
      enabled: true,
      lastEventAt: 0,
    },
  }));
}

beforeEach(() => {
  resetTasksView();
});

describe("tasksView — applyTaskStarted", () => {
  it("inserts the row + prepends to strip + bumps total when no filter", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 7 }));
    const view = useTasksStore.getState().tasksView;
    expect(view.tasksById[7]).toBeDefined();
    expect(view.activeIds[0]).toBe(7);
    expect(view.historyOrder[0]).toBe(7);
    expect(view.total).toBe(1);
  });

  it("prepends new row in front of existing strip chips (newest-first)", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 1 }));
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 2 }));
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 3 }));
    const view = useTasksStore.getState().tasksView;
    expect(view.activeIds).toEqual([3, 2, 1]);
    expect(view.historyOrder).toEqual([3, 2, 1]);
    expect(view.total).toBe(3);
  });

  it("does NOT touch history page when not on page 0", () => {
    resetTasksView({ page: 2 });
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 9 }));
    const view = useTasksStore.getState().tasksView;
    expect(view.activeIds).toEqual([9]);
    expect(view.historyOrder).toEqual([]);
    expect(view.total).toBe(1);
  });

  it("only bumps total when the status filter doesn't match", () => {
    resetTasksView({ statusFilter: "done" });
    useTasksStore
      .getState()
      .applyTaskStarted(makeTask({ id: 5, status: "running" }));
    const view = useTasksStore.getState().tasksView;
    expect(view.activeIds[0]).toBe(5);
    expect(view.historyOrder).toEqual([]);
    expect(view.total).toBe(0);
  });

  it("does NOT duplicate the chip if the same id fires twice", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 4 }));
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 4 }));
    const view = useTasksStore.getState().tasksView;
    expect(view.activeIds).toEqual([4]);
  });
});

describe("tasksView — child tasks stay out of the parents-only history", () => {
  it("keeps a child task off historyOrder + total but still in the strip", () => {
    useTasksStore
      .getState()
      .applyTaskStarted(makeTask({ id: 11, parent_task_id: 7 }));
    const view = useTasksStore.getState().tasksView;
    // Canonical map + strip projection still carry the row...
    expect(view.tasksById[11]).toBeDefined();
    expect(view.activeIds).toContain(11);
    // ...but the parents-only tab list (and its pager) ignores it.
    expect(view.historyOrder).toEqual([]);
    expect(view.total).toBe(0);
  });

  it("keeps roots in the history while children are skipped (mixed fire)", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 7 }));
    useTasksStore
      .getState()
      .applyTaskStarted(makeTask({ id: 8, parent_task_id: 7 }));
    const view = useTasksStore.getState().tasksView;
    expect(view.historyOrder).toEqual([7]);
    expect(view.total).toBe(1);
  });

  it("does NOT insert a completed child into the history page", () => {
    resetTasksView({ page: 0 });
    useTasksStore.getState().applyTaskCompleted(
      makeTask({
        id: 14,
        parent_task_id: 7,
        status: "done",
        completed_at: "2026-06-07T13:00:00Z",
      }),
    );
    const view = useTasksStore.getState().tasksView;
    expect(view.historyOrder).not.toContain(14);
    expect(view.tasksById[14]).toBeDefined();
  });
});

describe("tasksView — applyTaskProgress", () => {
  it("merges progress + last_message onto an existing row", () => {
    useTasksStore
      .getState()
      .applyTaskStarted(makeTask({ id: 3, last_message: "starting" }));
    useTasksStore
      .getState()
      .applyTaskProgress(3, { progress: 0.42, last_message: "scanning" });
    const row = useTasksStore.getState().tasksView.tasksById[3];
    expect(row.progress).toBeCloseTo(0.42);
    expect(row.last_message).toBe("scanning");
  });

  it("is a no-op for unknown task ids (no entry added)", () => {
    useTasksStore
      .getState()
      .applyTaskProgress(999, { progress: 0.5, last_message: "hi" });
    const view = useTasksStore.getState().tasksView;
    expect(view.tasksById[999]).toBeUndefined();
    expect(view.total).toBe(0);
  });

  it("merges only the fields present in the patch", () => {
    useTasksStore
      .getState()
      .applyTaskStarted(makeTask({ id: 8, progress: 0.1, last_message: "x" }));
    useTasksStore.getState().applyTaskProgress(8, { progress: 0.7 });
    const row = useTasksStore.getState().tasksView.tasksById[8];
    expect(row.progress).toBeCloseTo(0.7);
    expect(row.last_message).toBe("x");
  });

  it("can flip status (paused → running, etc.) via the patch", () => {
    useTasksStore
      .getState()
      .applyTaskStarted(makeTask({ id: 10, status: "paused" }));
    useTasksStore.getState().applyTaskProgress(10, { status: "running" });
    const row = useTasksStore.getState().tasksView.tasksById[10];
    expect(row.status).toBe("running");
  });
});

describe("tasksView — applyTaskInputNeeded", () => {
  it("replaces the snapshot + keeps the chip on the strip", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 5 }));
    useTasksStore.getState().applyTaskInputNeeded(
      makeTask({
        id: 5,
        status: "awaiting_input",
        input_request: { prompt: "which file?", options: ["a", "b"] },
      }),
    );
    const view = useTasksStore.getState().tasksView;
    expect(view.tasksById[5].status).toBe("awaiting_input");
    expect(view.tasksById[5].input_request?.options).toEqual(["a", "b"]);
    expect(view.activeIds).toEqual([5]);
  });

  it("does not bump total when the row was already known", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 11 }));
    expect(useTasksStore.getState().tasksView.total).toBe(1);
    useTasksStore
      .getState()
      .applyTaskInputNeeded(makeTask({ id: 11, status: "awaiting_input" }));
    expect(useTasksStore.getState().tasksView.total).toBe(1);
  });
});

describe("tasksView — applyTaskCompleted", () => {
  it("flips status + keeps chip on strip (sweep is the only dropper)", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 22 }));
    useTasksStore.getState().applyTaskCompleted(
      makeTask({
        id: 22,
        status: "done",
        completed_at: "2026-06-07T12:01:00Z",
        result: { summary: "found 3 files" },
      }),
    );
    const view = useTasksStore.getState().tasksView;
    expect(view.tasksById[22].status).toBe("done");
    expect(view.tasksById[22].result?.summary).toBe("found 3 files");
    expect(view.activeIds).toEqual([22]);
  });

  it("inserts a never-seen-before terminal row into the history page on page 0", () => {
    resetTasksView({ page: 0 });
    useTasksStore
      .getState()
      .applyTaskCompleted(
        makeTask({ id: 99, status: "done", completed_at: "2026-06-07T13:00:00Z" }),
      );
    const view = useTasksStore.getState().tasksView;
    expect(view.historyOrder).toContain(99);
  });
});

describe("tasksView — setTasksPage (REST loader)", () => {
  it("replaces history order + merges rows into tasksById", () => {
    useTasksStore.getState().setTasksPage({
      tasks: [makeTask({ id: 1 }), makeTask({ id: 2 })],
      total: 100,
      page: 1,
      pageSize: 50,
      enabled: true,
    });
    const view = useTasksStore.getState().tasksView;
    expect(view.historyOrder).toEqual([1, 2]);
    expect(view.tasksById[1]).toBeDefined();
    expect(view.tasksById[2]).toBeDefined();
    expect(view.total).toBe(100);
    expect(view.page).toBe(1);
    expect(view.loading).toBe(false);
  });

  it("does NOT clobber the strip projection", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 50 }));
    useTasksStore.getState().setTasksPage({
      tasks: [makeTask({ id: 7 })],
      total: 1,
      page: 0,
      pageSize: 50,
      enabled: true,
    });
    const view = useTasksStore.getState().tasksView;
    expect(view.activeIds).toContain(50);
    expect(view.historyOrder).toEqual([7]);
  });

  it("flips ``enabled`` when the backend reports the subsystem off", () => {
    useTasksStore.getState().setTasksPage({
      tasks: [],
      total: 0,
      page: 0,
      pageSize: 50,
      enabled: false,
    });
    expect(useTasksStore.getState().tasksView.enabled).toBe(false);
  });
});

describe("tasksView — sweepRecentlyCompletedTasks", () => {
  it("drops terminal rows older than the cutoff but keeps running ones", () => {
    const longAgo = new Date(Date.now() - 60_000).toISOString();
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 1 }));
    useTasksStore.getState().applyTaskCompleted(
      makeTask({
        id: 1,
        status: "done",
        completed_at: longAgo,
      }),
    );
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 2 }));
    useTasksStore.getState().sweepRecentlyCompletedTasks(20_000);
    const view = useTasksStore.getState().tasksView;
    expect(view.activeIds).toEqual([2]);
  });

  it("keeps terminal rows that are still within the grace window", () => {
    const recent = new Date(Date.now() - 1_000).toISOString();
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 9 }));
    useTasksStore.getState().applyTaskCompleted(
      makeTask({ id: 9, status: "done", completed_at: recent }),
    );
    useTasksStore.getState().sweepRecentlyCompletedTasks(60_000);
    const view = useTasksStore.getState().tasksView;
    expect(view.activeIds).toContain(9);
  });

  it("does nothing when the strip is empty", () => {
    const before = useTasksStore.getState().tasksView;
    useTasksStore.getState().sweepRecentlyCompletedTasks(1_000);
    const after = useTasksStore.getState().tasksView;
    expect(after).toBe(before);
  });
});

describe("tasksView — dismissTaskFromStrip", () => {
  it("removes the row from activeIds but keeps it in tasksById + history", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 4 }));
    useTasksStore.getState().dismissTaskFromStrip(4);
    const view = useTasksStore.getState().tasksView;
    expect(view.activeIds).toEqual([]);
    expect(view.tasksById[4]).toBeDefined();
    expect(view.historyOrder).toEqual([4]);
  });

  it("is idempotent for already-dismissed ids", () => {
    useTasksStore.getState().applyTaskStarted(makeTask({ id: 6 }));
    useTasksStore.getState().dismissTaskFromStrip(6);
    const beforeState = useTasksStore.getState().tasksView;
    useTasksStore.getState().dismissTaskFromStrip(6);
    const afterState = useTasksStore.getState().tasksView;
    expect(afterState).toBe(beforeState);
  });
});

describe("tasksView — setTaskStatusFilter", () => {
  it("flips filter + resets page to 0", () => {
    resetTasksView({ page: 3 });
    useTasksStore.getState().setTaskStatusFilter("done");
    const view = useTasksStore.getState().tasksView;
    expect(view.statusFilter).toBe("done");
    expect(view.page).toBe(0);
  });

  it("clears the filter when passed null", () => {
    useTasksStore.getState().setTaskStatusFilter("running");
    useTasksStore.getState().setTaskStatusFilter(null);
    expect(useTasksStore.getState().tasksView.statusFilter).toBeNull();
  });
});
