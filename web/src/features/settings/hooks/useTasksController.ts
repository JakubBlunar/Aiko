import { useCallback, useEffect, useState } from "react";
import { api } from "@/api";
import { useTasksStore } from "@/stores/useTasksStore";
import type { TaskStatus } from "@/types";

/**
 * Owns all "Tasks" tab state + REST handlers for the SettingsDrawer: the
 * paginated task-history store wiring, cancel / answer actions, and the
 * open-on-tab refresh effect. Live ``task_*`` WS events keep the store in
 * sync between refetches. Extracted (phase 4c).
 */
export function useTasksController(open: boolean, activeTab: string) {
  const tasksView = useTasksStore((s) => s.tasksView);
  const setTasksPage = useTasksStore((s) => s.setTasksPage);
  const setTaskStatusFilter = useTasksStore((s) => s.setTaskStatusFilter);
  const setTasksLoading = useTasksStore((s) => s.setTasksLoading);
  const dismissTaskFromStrip = useTasksStore((s) => s.dismissTaskFromStrip);
  const [tasksError, setTasksError] = useState<string | null>(null);

  // Mirrors ``refreshMemories``: paginated REST fetch with an optional
  // override for page / status filter so user actions (page → / ← /
  // filter pill click) can pass the next desired value without waiting
  // for the previous setState to flush.
  const refreshTasks = useCallback(
    async (overrides?: {
      page?: number;
      statusFilter?: TaskStatus | null;
    }) => {
      const page = overrides?.page ?? tasksView.page;
      const statusFilter =
        overrides?.statusFilter !== undefined
          ? overrides.statusFilter
          : tasksView.statusFilter;
      setTasksLoading(true);
      setTasksError(null);
      try {
        const data = await api.listTasks({
          limit: tasksView.pageSize,
          offset: page * tasksView.pageSize,
          status: statusFilter,
          rootsOnly: true,
        });
        setTasksPage({
          tasks: data.tasks,
          total: data.total,
          page,
          pageSize: tasksView.pageSize,
          enabled: data.enabled,
        });
      } catch (err) {
        setTasksError(String(err));
        setTasksLoading(false);
      }
    },
    [
      tasksView.page,
      tasksView.statusFilter,
      tasksView.pageSize,
      setTasksPage,
      setTasksLoading,
    ],
  );

  const handleTaskCancel = useCallback(async (taskId: number) => {
    setTasksError(null);
    try {
      await api.cancelTask(taskId);
      // The orchestrator listener will fire ``task_completed``
      // through the WS so the store updates without a refetch.
    } catch (err) {
      setTasksError(String(err));
    }
  }, []);

  const handleTaskAnswer = useCallback(
    async (taskId: number, answer: string) => {
      setTasksError(null);
      try {
        await api.answerTask(taskId, answer);
        // Server fires ``task_progress`` / ``task_completed`` once
        // the handler resumes.
      } catch (err) {
        setTasksError(String(err));
      }
    },
    [],
  );

  // Refresh the tasks page whenever the user opens the Tasks tab or
  // flips the status filter / page. Same shape as the memory hook.
  useEffect(() => {
    if (!open || activeTab !== "tasks") return;
    void refreshTasks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, activeTab, tasksView.page, tasksView.statusFilter]);

  return {
    tasksView,
    tasksError,
    setTasksPage,
    setTaskStatusFilter,
    dismissTaskFromStrip,
    refreshTasks,
    handleTaskCancel,
    handleTaskAnswer,
  };
}
