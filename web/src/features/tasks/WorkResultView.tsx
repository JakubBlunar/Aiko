import { useEffect, useState } from "react";
import { api } from "@/api";
import type { WorkResult } from "@/types";

/** Large evidence stays server-side until the user opens it. */
export function WorkResultView({ taskId }: { taskId: number }) {
  const [result, setResult] = useState<WorkResult | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    let cancelled = false;
    setResult(null); setError("");
    void api.getWorkResult(taskId).then((r) => { if (!cancelled) setResult(r); })
      .catch((e: Error) => { if (!cancelled) setError(e.message); });
    return () => { cancelled = true; };
  }, [taskId]);
  const more = async () => {
    if (!result || result.next_offset == null || busy) return;
    setBusy(true); setError("");
    try {
      const page = await api.getWorkResult(taskId, result.next_offset, result.revision);
      setResult((old) => old ? { ...page, content: old.content + page.content } : page);
    } catch (e) { setError(e instanceof Error ? e.message : "Could not load result."); }
    finally { setBusy(false); }
  };
  return <div className="space-y-3 text-sm">
    {error && <p role="alert" className="text-rose-300">{error}</p>}
    {!result && !error && <p className="text-ink-100/50">Loading result…</p>}
    {result && <>
      {result.error && <p className="text-rose-300">{result.error}</p>}
      <pre className="whitespace-pre-wrap break-words font-sans leading-relaxed">{result.content || "No result saved yet."}</pre>
      {result.truncated && <p className="text-amber-200">Some provider output was omitted or cannot be represented as text; this evidence is partial.</p>}
      {result.receipt && <details><summary className="cursor-pointer text-ink-100/60">Operation receipt</summary>
        <pre className="mt-2 whitespace-pre-wrap break-all text-xs">{JSON.stringify(result.receipt, null, 2)}</pre>
        <p className="mt-2 text-xs text-ink-100/50">An acknowledgement records what the tool returned. It does not independently confirm an external change.</p>
      </details>}
      {result.next_offset != null && <button type="button" disabled={busy} onClick={() => void more()}
        className="rounded border border-white/20 px-3 py-2">{busy ? "Loading…" : "Read more"}</button>}
    </>}
  </div>;
}
