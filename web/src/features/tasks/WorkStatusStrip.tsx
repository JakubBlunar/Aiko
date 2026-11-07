import { useTasksStore } from "@/stores/useTasksStore";
import { useWorkStore } from "./useWorkStore";

/** Keep the chat quiet: operations are grouped under their root job. */
export function WorkStatusStrip() {
  const tasks = useTasksStore((s) => s.tasksView.tasksById);
  const show = useWorkStore((s) => s.show);
  const roots = Object.values(tasks).filter((t) => t.parent_task_id == null &&
    ["running", "paused", "awaiting_input"].includes(t.status));
  if (!roots.length) return null;
  return <div aria-label="Active work" className="flex shrink-0 gap-2 overflow-x-auto border-b border-white/5 px-4 py-2">
    {roots.map((t) => {
      const waiting = Object.values(tasks).some((child) => child.parent_task_id === t.id && child.status === "awaiting_input");
      return <button key={t.id} type="button" onClick={() => show(t.id)} className="max-w-xs shrink-0 truncate rounded border border-white/10 px-3 py-1 text-xs text-ink-100/70">
        {waiting || t.status === "awaiting_input" ? "Needs answer" : t.status === "paused" ? "Waiting" : "Working"} · {t.title}
      </button>;
    })}
  </div>;
}
