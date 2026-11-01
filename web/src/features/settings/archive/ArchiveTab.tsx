import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api";
import { useAssistantStore } from "@/store";
import { archiveButton, MessageCard, OverviewContent, SourceReader } from "./ArchiveContent";
import { archiveStatus, localDate, type ArchiveQuery, type ArchiveResponse } from "./types";

const inputClass = "min-w-0 rounded border border-white/15 bg-black/20 px-2 py-1.5 text-sm text-ink-100";

function initialRange() {
  const end = new Date();
  const start = new Date(end);
  start.setDate(start.getDate() - 6);
  return { start: localDate(start), end: localDate(end) };
}

export function ArchiveTab() {
  const deletedMessageIds = useAssistantStore((state) => state.deletedMessageIds);
  const [dates, setDates] = useState(initialRange);
  const [topic, setTopic] = useState("");
  const [query, setQuery] = useState<ArchiveQuery>(() => ({ mode: "overviews", ...initialRange() }));
  const [result, setResult] = useState<ArchiveResponse | null>(null);
  const [source, setSource] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [requesting, setRequesting] = useState(false);
  const [refresh, setRefresh] = useState(0);
  const generation = useRef(0);
  const singleDay = query.start === query.end && !!query.start;
  const zone = result?.range?.timezone ?? result?.archive_timezone ?? "system";
  const job = result?.jobs?.find((entry) => entry.local_day === query.start && entry.timezone === zone);
  const waiting = singleDay && job != null && job.state !== "failed";

  useEffect(() => {
    const current = ++generation.current;
    setBusy(true); setResult(null); setSource(null); setError(null);
    void api.getArchive(query).then((value) => {
      if (current !== generation.current) return;
      setError(archiveStatus(value)); setResult(value);
    }).catch(() => {
      if (current === generation.current) setError("Could not load the archive. Check the connection and refresh.");
    }).finally(() => {
      if (current === generation.current) setBusy(false);
    });
    return () => { generation.current++; };
  }, [query, refresh, deletedMessageIds]);

  // Poll only an explicitly requested job, while this panel and page are visible.
  useEffect(() => {
    if (!waiting) return;
    const timer = window.setInterval(() => {
      if (!document.hidden) setRefresh((value) => value + 1);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [waiting]);

  const openDay = (day: string) => {
    setDates({ start: day, end: day }); setTopic("");
    setQuery({ mode: "overviews", start: day, end: day });
  };
  const more = useCallback(async () => {
    if (!result?.next_cursor || busy) return;
    const current = generation.current;
    setBusy(true); setError(null);
    try {
      const value = await api.getArchive({ mode: query.mode, cursor: result.next_cursor });
      if (current !== generation.current) return;
      const failure = archiveStatus(value);
      if (failure) { setResult(null); setSource(null); setError(failure); }
      else setResult((previous) => ({ ...value,
        overviews: [...(previous?.overviews ?? []), ...(value.overviews ?? [])],
        messages: [...(previous?.messages ?? []), ...(value.messages ?? [])],
        context: [...new Map([...(previous?.context ?? []), ...(value.context ?? [])].map((m) => [m.message_id, m])).values()],
      }));
    } catch { if (current === generation.current) setError("Could not load the next page. Try again."); }
    finally { if (current === generation.current) setBusy(false); }
  }, [result, busy, query.mode]);

  const request = async (cancel = false) => {
    if (!query.start || requesting) return;
    const current = generation.current;
    setRequesting(true); setError(null);
    try {
      await api.requestArchiveSummary(query.start, zone, cancel);
      if (current === generation.current) setRefresh((value) => value + 1);
    } catch { if (current === generation.current) setError("Could not update the summary request. Check that tools and daily overviews are enabled."); }
    finally { setRequesting(false); }
  };

  const overviews = result?.overviews ?? [];
  const days = [...new Set([...overviews.map((day) => day.local_day), ...(result?.missing_days ?? [])])].sort().reverse();

  return (
    <div className="min-w-0 space-y-5">
      <div>
        <h3 className="text-base font-medium">Conversation archive</h3>
        <p className="mt-1 text-xs text-ink-100/60">Browse past conversations, review daily summaries, and open the original words.</p>
      </div>
      <form className="space-y-3" onSubmit={(event) => {
        event.preventDefault(); setQuery({ mode: "messages", ...dates, topic });
      }}>
        <div className="flex flex-wrap gap-3">
          <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">From
            <input aria-label="Archive start date" type="date" required value={dates.start} max={dates.end} className={inputClass}
              onChange={(event) => setDates((old) => ({ ...old, start: event.target.value }))} />
          </label>
          <label className="flex min-w-0 flex-1 flex-col gap-1 text-xs">To
            <input aria-label="Archive end date" type="date" required value={dates.end} min={dates.start} className={inputClass}
              onChange={(event) => setDates((old) => ({ ...old, end: event.target.value }))} />
          </label>
        </div>
        <label className="flex flex-col gap-1 text-xs">Find words in the transcript
          <input value={topic} maxLength={200} placeholder="Optional word or phrase" className={inputClass}
            onChange={(event) => setTopic(event.target.value)} />
        </label>
        <div className="flex flex-wrap gap-2">
          <button type="submit" className={archiveButton}>Search transcript</button>
          <button type="button" className={archiveButton} onClick={() => setQuery({ mode: "overviews", ...dates })}>Browse days</button>
          <button type="button" className={archiveButton} onClick={() => setRefresh((value) => value + 1)}>Refresh</button>
        </div>
        <p className="text-[11px] text-ink-100/50">Dates use {zone === "system" ? "the backend’s system timezone" : zone}. Search matches the exact phrase, ignoring case.</p>
      </form>
      {error ? <p role="alert" className="text-sm text-rose-200">{error}</p> : null}
      {busy ? <p role="status" className="text-sm text-ink-100/60">Loading archive…</p> : null}
      {source != null ? <SourceReader key={source} id={source} onClose={() => setSource(null)} /> : null}
      {result && !error ? (
        <div className="space-y-4">
          {query.mode === "overviews" ? <>
            {singleDay ? <div className="space-y-3">
              <h4 className="text-sm font-medium">{query.start}</h4>
              <div className="flex flex-wrap items-center gap-2">
                {waiting ? <>
                  <p role="status" className="text-xs text-ink-100/60">{job?.state === "running" ? "Generating summary…" : "Summary queued — waiting for idle time."}</p>
                  <button type="button" disabled={requesting} className={archiveButton} onClick={() => void request(true)}>Cancel request</button>
                </> : <button type="button" disabled={requesting || !result.generation_available || result.status === "empty_range"} className={archiveButton}
                  onClick={() => void request()}>{job?.state === "failed" ? "Retry summary" : overviews[0]?.summary ? "Regenerate summary" : "Generate summary"}</button>}
                <button type="button" className={archiveButton} onClick={() => setQuery({ mode: "messages", start: query.start, end: query.end })}>Read day transcript</button>
              </div>
              {!result.generation_available ? <p className="text-xs text-ink-100/60">Summary generation needs tools, daily overviews and the idle scheduler enabled.</p> : null}
              {job?.state === "failed" ? <div className="space-y-2">
                <p role="alert" className="text-xs text-amber-200">The summary could not be saved. The source may have changed, or generation failed. You can retry or read the transcript.</p>
                <button type="button" disabled={requesting} className={archiveButton} onClick={() => void request(true)}>Dismiss failed request</button>
              </div> : null}
              {overviews[0] ? <OverviewContent overview={overviews[0]} onSource={setSource} /> : null}
            </div> : <div className="space-y-2">
              {days.map((day) => {
                const overview = overviews.find((item) => item.local_day === day);
                return <button type="button" key={day} onClick={() => openDay(day)}
                  className="block w-full rounded-lg border border-white/10 bg-white/[0.03] p-3 text-left hover:bg-white/[0.07]">
                  <span className="text-sm font-medium">{day}</span>
                  <span className="ml-2 text-xs text-ink-100/60">{overview ? `${overview.message_count ?? `${overview.message_count_lower_bound}+`} messages · ${overview.session_count} conversations` : "Transcript available · no daily outline yet"}</span>
                  {overview?.summary?.bullets[0] ? <p className="mt-1 break-words text-xs text-ink-100/70">{overview.summary.bullets[0].text}</p> : null}
                </button>;
              })}
            </div>}
            {result.status === "missing_overviews" ? <p className="text-xs text-ink-100/60">Daily outlines haven’t been built yet. Open a day to read or request its summary.</p> : null}
            {result.coverage?.source_scan_complete === false ? <p className="text-xs text-amber-200">This directory covers only part of the range. Choose fewer dates to find more source days.</p> : null}
          </> : <>
            {query.topic ? <p className="text-xs text-ink-100/60">Selected matches for “{query.topic}”; this is not the full conversation.</p> : null}
            {result.coverage?.scan_complete === false ? <p className="text-xs text-amber-200">Only part of this range was searched. Narrow the dates to continue.</p> : null}
            {(result.messages ?? []).map((message) => <MessageCard key={message.message_id} message={message} onSource={setSource} />)}
            {result.context?.length ? <details><summary className="cursor-pointer text-xs text-ink-100/60">Nearby conversation context</summary>
              <div className="mt-2 space-y-2">{result.context.map((message) => <MessageCard key={message.message_id} message={message} onSource={setSource} />)}</div>
            </details> : null}
            {result.status === "no_topic_match" ? <p className="text-sm">No matching words in this date range.</p> : null}
          </>}
          {result.status === "empty_range" ? <p className="text-sm text-ink-100/60">No retained conversation in this date range.</p> : null}
          {result.next_cursor ? <button type="button" disabled={busy} className={archiveButton} onClick={() => void more()}>Load more</button> : null}
        </div>
      ) : null}
    </div>
  );
}
