import { useTasksStore } from "@/stores/useTasksStore";
import { useEffect } from "react";
import { useAssistantStore } from "@/store";
import { api } from "@/api";
import { useWorkStore } from "./useWorkStore";

export function WorkToggle() {
  const connection = useAssistantStore((s) => s.connection.status);
  useEffect(() => {
    let cancelled = false;
    void api.listWork().then((page) => {
      if (!cancelled) page.tasks.forEach(useTasksStore.getState().applyTaskUpdated);
    }).catch(() => { /* the next connection change retries */ });
    return () => { cancelled = true; };
  }, [connection]);
  const open = useWorkStore((s) => s.open);
  const show = useWorkStore((s) => s.show);
  const close = useWorkStore((s) => s.close);
  const count = useTasksStore((s) => Object.values(s.tasksView.tasksById).filter(
    (t) => t.parent_task_id == null && ["running", "paused", "awaiting_input"].includes(t.status),
  ).length);
  return <button type="button" aria-expanded={open} aria-controls="work-drawer"
    className="rounded-md border border-white/15 px-3 py-2 text-xs text-ink-100 hover:bg-white/10"
    onClick={() => open ? close() : show()}>Work{count ? ` (${count})` : ""}</button>;
}
