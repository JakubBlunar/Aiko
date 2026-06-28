import type { TaskProgressPatch, TaskSnapshot, TaskStatus } from "@/types";
import type { SliceCreator } from "../types";

export interface TasksSlice {
  // Background tasks (chunk 14). The brain orchestration tasks API
  // surfaces in a compact ``TaskStrip`` above the chat and a paginated
  // ``TasksTab`` in the SettingsDrawer. ``tasksById`` is canonical; both
  // surfaces project from it.
  tasksView: {
    tasksById: Record<number, TaskSnapshot>;
    activeIds: number[];
    historyOrder: number[];
    total: number;
    page: number;
    pageSize: number;
    statusFilter: TaskStatus | null;
    loading: boolean;
    enabled: boolean;
    lastEventAt: number;
  };
  /** Reducer for ``task_started``. */
  applyTaskStarted: (task: TaskSnapshot) => void;
  /** Reducer for ``task_progress``. */
  applyTaskProgress: (taskId: number, patch: TaskProgressPatch) => void;
  /** Reducer for ``task_input_needed``. */
  applyTaskInputNeeded: (task: TaskSnapshot) => void;
  /** Reducer for ``task_completed``. */
  applyTaskCompleted: (task: TaskSnapshot) => void;
  /** REST load: ``GET /api/tasks`` paginated. */
  setTasksPage: (response: {
    tasks: TaskSnapshot[];
    total: number;
    page: number;
    pageSize: number;
    enabled: boolean;
  }) => void;
  setTaskStatusFilter: (status: TaskStatus | null) => void;
  setTasksLoading: (loading: boolean) => void;
  /** Drop a chip from the strip (dismiss button + sweep). Idempotent. */
  dismissTaskFromStrip: (taskId: number) => void;
  /** Sweep terminal tasks whose ``completed_at`` is older than maxAgeMs. */
  sweepRecentlyCompletedTasks: (maxAgeMs: number) => void;
}

export const createTasksSlice: SliceCreator<TasksSlice> = (set) => ({
  tasksView: {
    tasksById: {},
    activeIds: [],
    historyOrder: [],
    total: 0,
    page: 0,
    pageSize: 50,
    statusFilter: null,
    loading: false,
    enabled: true,
    lastEventAt: 0,
  },
  applyTaskStarted: (task) =>
    set((state) => {
      const view = state.tasksView;
      const nextById = { ...view.tasksById, [task.id]: task };
      // Strip projection: prepend only if the id wasn't already
      // present (a server-side double-fire would otherwise stack).
      const nextActive = view.activeIds.includes(task.id)
        ? view.activeIds
        : [task.id, ...view.activeIds];
      // History projection: prepend only when the user is on
      // page 0 AND the filter matches (mirror of memory_added).
      // The tab is parents-only (server ``roots_only``), so child
      // tasks never enter ``historyOrder`` nor bump ``total`` —
      // they surface inside their parent's expandable detail.
      const isRoot = task.parent_task_id == null;
      const filterMatches =
        view.statusFilter === null || view.statusFilter === task.status;
      const onFirstPage = view.page === 0;
      const nextHistory =
        isRoot &&
        onFirstPage &&
        filterMatches &&
        !view.historyOrder.includes(task.id)
          ? [task.id, ...view.historyOrder].slice(0, view.pageSize)
          : view.historyOrder;
      // ``total`` bumps for root tasks so the pager updates even
      // when the row didn't land on the visible page.
      const nextTotal = isRoot && filterMatches ? view.total + 1 : view.total;
      return {
        tasksView: {
          ...view,
          tasksById: nextById,
          activeIds: nextActive,
          historyOrder: nextHistory,
          total: nextTotal,
          lastEventAt: Date.now(),
        },
      };
    }),
  applyTaskProgress: (taskId, patch) =>
    set((state) => {
      const view = state.tasksView;
      const existing = view.tasksById[taskId];
      if (!existing) return {};
      const merged: TaskSnapshot = {
        ...existing,
        status: patch.status ?? existing.status,
        progress:
          typeof patch.progress === "number"
            ? patch.progress
            : existing.progress,
        last_message:
          typeof patch.last_message === "string"
            ? patch.last_message
            : existing.last_message,
        // Schema v17: ``phase`` rides on the same patch.
        // ``undefined`` = handler didn't supply one; ``null`` = clear
        // the existing phase; a string = the new value.
        phase:
          patch.phase === undefined ? (existing.phase ?? null) : patch.phase,
      };
      return {
        tasksView: {
          ...view,
          tasksById: { ...view.tasksById, [taskId]: merged },
          lastEventAt: Date.now(),
        },
      };
    }),
  applyTaskInputNeeded: (task) =>
    set((state) => {
      const view = state.tasksView;
      const previouslyKnown = task.id in view.tasksById;
      // Keep the chip on the strip; ensure it's there if the row
      // is somehow new to us (broadcast race between client init
      // and a fast handler).
      const nextActive = view.activeIds.includes(task.id)
        ? view.activeIds
        : [task.id, ...view.activeIds];
      // History: same prepend rule as ``applyTaskStarted`` but
      // we don't bump ``total`` — the row already existed. Root
      // tasks only; children live in their parent's detail.
      const isRoot = task.parent_task_id == null;
      const filterMatches =
        view.statusFilter === null || view.statusFilter === task.status;
      const onFirstPage = view.page === 0;
      const inHistory = view.historyOrder.includes(task.id);
      const nextHistory =
        isRoot && onFirstPage && filterMatches && !inHistory && !previouslyKnown
          ? [task.id, ...view.historyOrder].slice(0, view.pageSize)
          : view.historyOrder;
      return {
        tasksView: {
          ...view,
          tasksById: { ...view.tasksById, [task.id]: task },
          activeIds: nextActive,
          historyOrder: nextHistory,
          lastEventAt: Date.now(),
        },
      };
    }),
  applyTaskCompleted: (task) =>
    set((state) => {
      const view = state.tasksView;
      // Keep the row in ``tasksById`` so the strip can render
      // "done" / "failed" / "cancelled" briefly before the sweep
      // drops it.
      const nextActive = view.activeIds.includes(task.id)
        ? view.activeIds
        : [task.id, ...view.activeIds];
      // History: ensure terminal root rows show up on a fresh load
      // even when the user wasn't on page 0 when the start fired.
      // Children stay out of the tab list (parents-only).
      const isRoot = task.parent_task_id == null;
      const filterMatches =
        view.statusFilter === null || view.statusFilter === task.status;
      const onFirstPage = view.page === 0;
      const inHistory = view.historyOrder.includes(task.id);
      const inTasksById = task.id in view.tasksById;
      const nextHistory =
        isRoot && onFirstPage && filterMatches && !inHistory && !inTasksById
          ? [task.id, ...view.historyOrder].slice(0, view.pageSize)
          : view.historyOrder;
      return {
        tasksView: {
          ...view,
          tasksById: { ...view.tasksById, [task.id]: task },
          activeIds: nextActive,
          historyOrder: nextHistory,
          lastEventAt: Date.now(),
        },
      };
    }),
  setTasksPage: ({ tasks, total, page, pageSize, enabled }) =>
    set((state) => {
      const view = state.tasksView;
      const nextById = { ...view.tasksById };
      for (const t of tasks) {
        nextById[t.id] = t;
      }
      return {
        tasksView: {
          ...view,
          tasksById: nextById,
          historyOrder: tasks.map((t) => t.id),
          total,
          page,
          pageSize,
          enabled,
          loading: false,
        },
      };
    }),
  setTaskStatusFilter: (status) =>
    set((state) => ({
      tasksView: {
        ...state.tasksView,
        statusFilter: status,
        page: 0,
      },
    })),
  setTasksLoading: (loading) =>
    set((state) => ({
      tasksView: { ...state.tasksView, loading },
    })),
  dismissTaskFromStrip: (taskId) =>
    set((state) => {
      const view = state.tasksView;
      if (!view.activeIds.includes(taskId)) return {};
      return {
        tasksView: {
          ...view,
          activeIds: view.activeIds.filter((id) => id !== taskId),
        },
      };
    }),
  sweepRecentlyCompletedTasks: (maxAgeMs) =>
    set((state) => {
      const view = state.tasksView;
      if (view.activeIds.length === 0) return {};
      const now = Date.now();
      // A task is sweep-eligible when it's terminal AND its
      // ``completed_at`` (or our local lastEventAt as a fallback)
      // is older than ``maxAgeMs``.
      const TERMINAL = new Set<TaskStatus>([
        "done",
        "failed",
        "cancelled",
        "interrupted",
      ]);
      const remaining = view.activeIds.filter((id) => {
        const row = view.tasksById[id];
        if (!row) return false;
        if (!TERMINAL.has(row.status)) return true;
        const completedAt = row.completed_at
          ? Date.parse(row.completed_at)
          : NaN;
        const referenceAt = Number.isFinite(completedAt)
          ? completedAt
          : view.lastEventAt || now;
        return now - referenceAt < maxAgeMs;
      });
      if (remaining.length === view.activeIds.length) return {};
      return {
        tasksView: { ...view, activeIds: remaining },
      };
    }),
});
