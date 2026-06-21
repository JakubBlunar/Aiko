import { useCallback, useEffect, useState } from "react";
import { api } from "../../api";
import type {
  PersonaRegressionResult,
  PersonaRegressionSnapshot,
} from "../../types";

function formatRanAt(iso: string | undefined): string {
  if (!iso) return "never";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return iso;
  return new Date(ts).toLocaleString();
}

export function PersonaRegressionPanel() {
  const [data, setData] = useState<PersonaRegressionSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await api.getPersonaDrift());
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  const runCheck = useCallback(async () => {
    setRunning(true);
    setError(null);
    try {
      setData(await api.runPersonaDrift());
    } catch (err) {
      setError(String(err));
    } finally {
      setRunning(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const total = data?.total ?? 0;
  const passed = data?.passed ?? 0;
  const hasRun = Boolean(data?.ran_at) || total > 0;
  const allPassed = hasRun && total > 0 && passed === total;
  const results = data?.results ?? [];
  const failedResults = results.filter((r) => !r.passed);

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="K10 — replays a fixture of canonical 'golden turns' through Aiko's prompt and scores each reply against style markers (required reaction tags, forbidden corporate phrases). Catches the persona quietly drifting from prompt rot or memory contamination. On-demand; no background token spend."
        >
          Persona regression
          {hasRun ? (
            <span
              className={
                "ml-2 " +
                (allPassed ? "text-emerald-300/80" : "text-rose-300/80")
              }
            >
              {passed}/{total}
            </span>
          ) : null}
        </span>
        <button
          type="button"
          onClick={runCheck}
          disabled={running || loading}
          className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
        >
          {running ? "running…" : "run check"}
        </button>
      </div>

      {error ? (
        <div className="rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200">
          {error}
        </div>
      ) : null}

      {data?.error === "disabled" ? (
        <p className="text-[11px] text-ink-100/40">
          The persona-regression harness is off (
          <code className="text-ink-100/60">persona_regression_enabled</code>{" "}
          is disabled).
        </p>
      ) : data?.error === "no_fixture" ? (
        <p className="text-[11px] text-ink-100/40">
          No golden turns found in the fixture file.
        </p>
      ) : !hasRun ? (
        <p className="text-[11px] text-ink-100/40">
          Never run. Hit <span className="text-ink-100/60">run check</span> to
          replay the golden turns and score the persona.
        </p>
      ) : (
        <>
          <div className="text-[10px] text-ink-100/40">
            ran {formatRanAt(data?.ran_at)}
            {data?.model ? ` · ${data.model}` : ""}
            {data?.ran_ms ? ` · ${Math.round(data.ran_ms)}ms` : ""}
          </div>
          {allPassed ? (
            <p className="text-[11px] text-emerald-300/70">
              All {total} golden turns passed.
            </p>
          ) : (
            <ul className="space-y-1.5">
              {failedResults.map((r) => (
                <PersonaRegressionRow key={r.id} result={r} />
              ))}
            </ul>
          )}
        </>
      )}
    </div>
  );
}

interface PersonaRegressionRowProps {
  result: PersonaRegressionResult;
}

function PersonaRegressionRow({ result }: PersonaRegressionRowProps) {
  const [open, setOpen] = useState(false);
  return (
    <li className="rounded border border-rose-400/20 bg-rose-500/[0.04] p-2 text-[11px]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start justify-between gap-2 text-left"
      >
        <span className="min-w-0 flex-1">
          <span className="text-ink-100/90">{result.id}</span>
          <span className="ml-2 rounded bg-white/[0.06] px-1 py-px text-[9px] text-ink-100/50">
            {result.scope}
          </span>
        </span>
        <span className="shrink-0 text-[10px] text-rose-300/70">
          {result.failures.length} miss
          {result.failures.length === 1 ? "" : "es"} · {open ? "hide" : "show"}
        </span>
      </button>
      {open ? (
        <div className="mt-1.5 space-y-1 border-t border-white/5 pt-1.5">
          <ul className="space-y-0.5">
            {result.failures.map((f, idx) => (
              <li key={idx} className="text-rose-200/80">
                {f}
              </li>
            ))}
          </ul>
          {result.reply_preview ? (
            <div className="mt-1 text-ink-100/50">
              <span className="text-[10px] uppercase tracking-wide text-ink-100/30">
                reply
              </span>
              <div className="text-ink-100/70">{result.reply_preview}</div>
            </div>
          ) : null}
        </div>
      ) : null}
    </li>
  );
}
