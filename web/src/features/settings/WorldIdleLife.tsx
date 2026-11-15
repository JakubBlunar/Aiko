import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api";
import { Section } from "./SettingsSection";
import type { IdleLifeEvent, IdleLifeHistory, IdleLifeSummary } from "./world/idleLifeTypes";

const control = "rounded border border-white/10 bg-ink-900 px-2 py-1 text-xs text-ink-100";
const localTime = (value: string | null) => value ? new Date(value).toLocaleString() : "unknown";

function eventLabel(event: IdleLifeEvent) {
  const p = event.payload;
  return String(p.summary ?? p.reason ?? p.key ?? p.action ?? event.kind.replaceAll("_", " "));
}

export function IdleLifeTimeline({ events }: { events: IdleLifeEvent[] }) {
  return <ol className="space-y-2" aria-label="Idle-life events">
    {events.map(event => <li key={event.id} className="rounded border border-white/10 p-2 text-xs">
      <details>
        <summary className="cursor-pointer space-y-1">
          <div className="text-ink-100/50">{localTime(event.recorded_at)} · {event.worker} · {event.actor}</div>
          <div>{eventLabel(event)}</div>
          <div className="text-[10px] text-ink-100/50">{event.kind.replaceAll("_", " ")}
            {event.observations > 1 ? ` · ${event.observations} observations through ${localTime(event.last_seen_at)}` : ""}</div>
        </summary>
        {event.occurred_at !== event.recorded_at && <p className="mt-2 text-ink-100/50">
          Narrative time: {localTime(event.occurred_at)}. Recording time is shown above.</p>}
        <p className="mt-2 text-ink-100/50">Event #{event.id}{event.run_id ? ` · Run ${event.run_id}` : ""}</p>
        <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap break-words text-[10px] text-ink-100/70">
          {JSON.stringify(event.payload, null, 2)}
        </pre>
      </details>
    </li>)}
  </ol>;
}

export function WorldIdleLife() {
  const [hours, setHours] = useState(72);
  const [worker, setWorker] = useState("");
  const [kind, setKind] = useState("");
  const [actor, setActor] = useState("");
  const [history, setHistory] = useState<IdleLifeHistory | null>(null);
  const [summary, setSummary] = useState<IdleLifeSummary | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const request = useRef(0);
  const refresh = useCallback(async () => {
    const epoch = ++request.current;
    setBusy(true); setError("");
    try {
      const [page, totals] = await Promise.all([
        api.getIdleLifeHistory(hours, undefined, worker, kind, actor), api.getIdleLifeSummary(hours),
      ]);
      if (epoch === request.current) { setHistory(page); setSummary(totals); }
    } catch (err) { if (epoch === request.current) setError(String(err)); }
    finally { if (epoch === request.current) setBusy(false); }
  }, [hours, worker, kind, actor]);
  useEffect(() => { void refresh(); return () => { ++request.current; }; }, [refresh]);

  async function older() {
    if (!history?.next_before_id || busy) return;
    const epoch = ++request.current;
    setBusy(true); setError("");
    try {
      const page = await api.getIdleLifeHistory(hours, history.next_before_id, worker, kind, actor);
      if (epoch === request.current) setHistory(previous => previous
        ? { ...page, events: [...previous.events, ...page.events] } : page);
    } catch (err) { if (epoch === request.current) setError(String(err)); }
    finally { if (epoch === request.current) setBusy(false); }
  }

  function exportPage() {
    const blob = new Blob([JSON.stringify({ history, summary }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a"); link.href = url;
    link.download = "aiko-idle-life-visible-events.json"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }

  return <Section title="Idle life">
    <p className="text-xs text-ink-100/60">Fourteen days of decisions, saved effects and reasons for waiting.
      Quiet rest is part of her day. Counts are not a productivity score.</p>
    <div className="flex flex-wrap gap-2">
      <select className={control} aria-label="History window" value={hours}
        onChange={event => setHours(Number(event.target.value))}>
        <option value={24}>Last 24 hours</option><option value={72}>Last 72 hours</option>
        <option value={168}>Last 7 days</option><option value={336}>Last 14 days</option>
      </select>
      <select className={control} aria-label="Worker" value={worker} onChange={e => setWorker(e.target.value)}>
        <option value="">All workers</option>
        {["away_activity", "hobby", "garden_visit", "plant_growth", "room_evolution", "home_maintenance",
          "sleep_lifecycle", "idle_curiosity", "idle_knowledge", "home_actions", "scheduler"].map(w =>
          <option key={w} value={w}>{w.replaceAll("_", " ")}</option>)}
      </select>
      <select className={control} aria-label="Event type" value={kind} onChange={e => setKind(e.target.value)}>
        <option value="">All events</option>
        {["admission", "choice", "context", "step_completed", "action_effect", "run_result", "run_failed"].map(k =>
          <option key={k} value={k}>{k.replaceAll("_", " ")}</option>)}
      </select>
      <select className={control} aria-label="Initiator" value={actor} onChange={e => setActor(e.target.value)}>
        <option value="">All initiators</option><option value="autonomous">Autonomous</option>
        <option value="conversation">Conversation</option><option value="direct_worker">Direct worker/debug</option>
      </select>
      <button className={control} disabled={busy} onClick={() => void refresh()}>Refresh</button>
      <button className={control} disabled={!history} onClick={exportPage}>Export visible events</button>
    </div>
    {error && <p role="alert" className="text-xs text-rose-200">{error}</p>}
    {history && <div className="space-y-1 text-[11px] text-ink-100/60">
      <p>Capture available from {localTime(history.telemetry.oldest_recorded_at)}.
        Last observation: {localTime(history.telemetry.latest_observed_at)}.</p>
      <p>{history.telemetry.coverage_note}</p>
      {history.telemetry.failed_writes_since_boot > 0 && <p role="alert" className="text-amber-200">
        {history.telemetry.failed_writes_since_boot} trace writes failed since startup. Some evidence is missing.</p>}
    </div>}
    {summary && <details className="text-xs text-ink-100/70"><summary className="cursor-pointer">Window totals and holds</summary>
      <p className="my-2">{summary.interpretation}</p>
      <ul>{summary.counts.map(row => <li key={`${row.worker}:${row.kind}`}>
        {row.worker}: {row.kind.replaceAll("_", " ")} {row.count}</li>)}</ul>
      <ul className="mt-2">{summary.admission.map(row => <li key={row.reason}>
        {row.reason}: {row.intervals} intervals, {row.observations} observations</li>)}</ul>
      {summary.flags?.map((flag, i) => <p key={i} className="mt-2 text-amber-200">Review: {flag.detail}</p>)}
    </details>}
    {!history && busy && <p className="text-xs">Loading history...</p>}
    {history?.events.length === 0 && <p className="text-xs text-ink-100/60">No recorded events in this window.</p>}
    <IdleLifeTimeline events={history?.events ?? []} />
    {history?.next_before_id && <button className={control} disabled={busy} onClick={() => void older()}>
      {busy ? "Loading..." : "Load older events"}</button>}
  </Section>;
}
