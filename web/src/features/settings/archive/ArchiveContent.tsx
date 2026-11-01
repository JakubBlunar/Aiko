import { useEffect, useRef, useState } from "react";
import { api } from "@/api";
import { archiveStatus, type ArchiveMessage, type ArchiveOverview, type ArchiveResponse } from "./types";

export const archiveButton = "rounded border border-white/15 px-2.5 py-1.5 text-xs hover:bg-white/10 disabled:opacity-40";

export function MessageCard({ message, onSource }: {
  message: ArchiveMessage; onSource: (id: number) => void;
}) {
  return (
    <article className="min-w-0 rounded-lg border border-white/10 bg-white/[0.03] p-3">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2 text-[11px] text-ink-100/60">
        <span>{message.role === "user" ? "You" : "Aiko"} · {new Date(message.created_at).toLocaleString()}</span>
        <button type="button" className="underline" onClick={() => onSource(message.message_id)}>
          Open source #{message.message_id}
        </button>
      </div>
      <p className="whitespace-pre-wrap break-words text-sm">{message.text}</p>
      {!message.text_complete ? <p className="mt-2 text-xs text-ink-100/50">Excerpt — open source for more.</p> : null}
    </article>
  );
}

const kindLabels = {
  discussion: "Discussed", user_statement: "You said", assistant_suggestion: "Aiko suggested",
  plan: "Planned", decision: "Decided", correction: "Corrected",
};

export function OverviewContent({ overview, onSource }: {
  overview: ArchiveOverview; onSource: (id: number) => void;
}) {
  const summary = overview.summary;
  return (
    <div className="space-y-3">
      {overview.coverage.current_day_open ? <p className="text-xs text-amber-200">This day is still in progress.</p> : null}
      {!overview.coverage.scan_complete ? <p className="text-xs text-amber-200">Only part of this day was indexed. Narrow the transcript range for more.</p> : null}
      {summary ? (
        <section aria-label="Generated discussion summary" className="space-y-3">
          <p className="text-xs text-ink-100/60">
            Generated from {summary.source_messages_used} messages · {summary.source_text_complete ? "Full retained text considered" : "Selected or shortened text only"}.
            {" "}An interpretation of the conversation; plans and suggestions may not have happened.
          </p>
          {summary.bullets.length === 0 ? <p className="text-sm">No reliable summary points were produced. Read the transcript below.</p> : null}
          <ul className="space-y-3">
            {summary.bullets.map((bullet, index) => (
              <li key={index} className="rounded-lg border border-white/10 bg-white/[0.03] p-3">
                <p className="mb-1 text-[10px] uppercase tracking-wide text-pink-200/80">{kindLabels[bullet.kind]}</p>
                <p className="break-words text-sm">{bullet.text}</p>
                {bullet.event_date ? <p className="mt-1 text-xs text-ink-100/60">Mentioned event date: {bullet.event_date}</p> : null}
                <div className="mt-2 flex flex-wrap gap-2">
                  {bullet.sources.map((source, i) => (
                    <button type="button" key={`${source.message_id}-${i}`} className="text-xs text-pink-200 underline"
                      title={source.quote} onClick={() => onSource(source.message_id)}>
                      Open source #{source.message_id}
                    </button>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        </section>
      ) : <p className="text-xs text-ink-100/60">No generated summary yet. Selected quotes are available below.</p>}
      {overview.excerpts?.length ? (
        <details>
          <summary className="cursor-pointer text-xs text-ink-100/70">Selected source quotes ({overview.excerpts.length})</summary>
          <div className="mt-3 space-y-2">{overview.excerpts.map((message) => (
            <MessageCard key={message.message_id} message={message} onSource={onSource} />
          ))}</div>
        </details>
      ) : null}
    </div>
  );
}

export function SourceReader({ id, onClose }: { id: number; onClose: () => void }) {
  const panel = useRef<HTMLElement | null>(null);
  const [result, setResult] = useState<ArchiveResponse | null>(null);
  const [text, setText] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const generation = useRef(0);
  useEffect(() => {
    const current = ++generation.current;
    panel.current?.focus({ preventScroll: true });
    panel.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    setBusy(true);
    void api.getArchive({ mode: "message", message_id: id }).then((value) => {
      if (current !== generation.current) return;
      setError(archiveStatus(value));
      setResult(value);
      setText(value.message?.text ?? "");
    }).catch(() => {
      if (current === generation.current) setError("Could not open this source. Try again.");
    }).finally(() => {
      if (current === generation.current) setBusy(false);
    });
    return () => { generation.current++; };
  }, [id]);

  const next = async () => {
    if (!result?.next_cursor || busy) return;
    const current = generation.current;
    setBusy(true);
    setError(null);
    try {
      const value = await api.getArchive({ mode: "message", cursor: result.next_cursor });
      if (current !== generation.current) return;
      const failure = archiveStatus(value);
      if (failure) { setText(""); setResult(null); setError(failure); }
      else { setResult(value); setText((old) => old + (value.message?.text ?? "")); }
    } catch { if (current === generation.current) setError("Could not load more source text."); }
    finally { if (current === generation.current) setBusy(false); }
  };

  return (
    <section ref={panel} tabIndex={-1} aria-label={`Source message ${id}`} className="min-w-0 space-y-3 rounded-xl border border-pink-300/30 bg-black/20 p-4">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-sm font-medium">Original message #{id}</h4>
        <button type="button" className={archiveButton} onClick={onClose}>Close source</button>
      </div>
      {result?.message ? <p className="break-all text-xs text-ink-100/60">
        {result.message.role === "user" ? "You" : "Aiko"} · {new Date(result.message.created_at).toLocaleString()} · {result.message.session_id}
      </p> : null}
      {error ? <p role="alert" className="text-xs text-rose-200">{error}</p> : null}
      {text ? <p className="whitespace-pre-wrap break-words text-sm">{text}</p> : null}
      {busy ? <p role="status" className="text-xs text-ink-100/60">Loading source…</p> : null}
      {result?.next_cursor ? <button type="button" disabled={busy} className={archiveButton} onClick={() => void next()}>Load more source text</button> : null}
    </section>
  );
}
