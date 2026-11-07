import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api";
import type { TaskSnapshot, WorkDetail } from "@/types";
import { useWorkStore } from "./useWorkStore";
import { useTasksStore } from "@/stores/useTasksStore";

export function mergeMessages(previous: WorkDetail["messages"], next: WorkDetail["messages"]) {
  return [...new Map([...previous, ...next].map((m) => [m.id, m])).values()]
    .sort((a, b) => a.id - b.id);
}

export function useWorkController(open: boolean) {
  const selected = useWorkStore((s) => s.selected);
  const select = useWorkStore((s) => s.select);
  const [jobs, setJobs] = useState<TaskSnapshot[]>([]);
  const snapshots = useTasksStore((s) => s.tasksView.tasksById);
  const [detail, setDetail] = useState<WorkDetail | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [hasMore, setHasMore] = useState(false);
  const offset = useRef(0);
  const generation = useRef(0);
  const acting = useRef(false);
  const current = useRef({ selected, open });
  current.current = { selected, open };

  const loadJobs = useCallback(async (more = false) => {
    const token = generation.current;
    const at = more ? offset.current + 30 : 0;
    const response = await api.listWork(at);
    if (!current.current.open || token !== generation.current) return;
    setJobs((old) => more
      ? [...new Map([...old, ...response.tasks].map((t) => [t.id, t])).values()]
      : [...new Map([...response.tasks, ...old.filter((t) => !response.tasks.some((n) => n.id === t.id))]
        .map((t) => [t.id, t])).values()]);
    if (more || offset.current === 0) setHasMore(response.has_more);
    if (more) offset.current = at;
    if (current.current.selected == null && response.tasks[0]) select(response.tasks[0].id);
  }, [select]);

  const refresh = useCallback(async () => {
    const id = current.current.selected;
    if (id == null || !current.current.open) return;
    const token = generation.current;
    const response = await api.getWork(id);
    if (token !== generation.current || current.current.selected !== id || !current.current.open) return;
    setDetail((old) => old?.task.id === id
      ? { ...response, messages: mergeMessages(old.messages, response.messages),
          oldest_event_id: old.oldest_event_id ?? response.oldest_event_id,
          has_older: old.messages.length > response.messages.length ? old.has_older : response.has_older }
      : response);
    setJobs((old) => old.map((t) => t.id === id ? response.task : t));
    setError("");
  }, []);

  useEffect(() => {
    generation.current++;
    setDetail(null);
    setError("");
    if (!open) return;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout>;
    const poll = async () => {
      try {
        await loadJobs();
        if (!stopped) await refresh();
      } catch (e) {
        if (!stopped) setError(e instanceof Error ? e.message : "Could not load work.");
      }
      if (!stopped) timer = setTimeout(poll, 2500);
    };
    void poll();
    return () => { stopped = true; clearTimeout(timer); generation.current++; };
  }, [open, selected, loadJobs, refresh]);

  const act = async (action: () => Promise<unknown>) => {
    if (acting.current) return false;
    acting.current = true;
    setBusy(true);
    setError("");
    try {
      await action();
      // A refresh failure must not turn an accepted answer into an apparent
      // failed send. The next poll can recover the view without resubmission.
      try { await refresh(); } catch { /* polling retries the view */ }
      return true;
    }
    catch (e) { setError(e instanceof Error ? e.message : "That action could not be completed."); return false; }
    finally { acting.current = false; setBusy(false); }
  };
  const earlier = () => act(async () => {
    if (!detail?.oldest_event_id) return;
    const id = detail.task.id;
    const response = await api.getWork(id, detail.oldest_event_id);
    if (current.current.selected !== id) return;
    setDetail((old) => old?.task.id === id ? {
      ...old, messages: mergeMessages(response.messages, old.messages),
      oldest_event_id: response.oldest_event_id, has_older: response.has_older,
    } : old);
  });
  return { jobs: jobs.map((job) => {
    const live = snapshots[job.id];
    return live && live.updated_at > job.updated_at ? live : job;
  }), detail, error, busy, hasMore, refresh, act, earlier,
    more: () => act(() => loadJobs(true)) };
}
