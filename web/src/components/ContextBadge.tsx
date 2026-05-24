import { useEffect, useRef, useState } from "react";
import { useAssistantStore } from "../store";

/**
 * Compact pill in the ChatView header showing how full the LLM context is and
 * how the model is performing. Click toggles a popover with the per-block
 * breakdown so power users can see exactly what they're paying for.
 *
 * Pill format:  ``42% · 1.4s · 38 tok/s``
 *
 * - ``%`` is `prompt_tokens / context_window` from the last turn (Ollama
 *   authoritative; falls back to the assembler's char-heuristic estimate).
 * - ``s`` is `llm_ms / 1000` — wall-clock latency for the just-finished turn.
 * - ``tok/s`` is the model's reported throughput.
 *
 * The pill stays hidden until we have a `context_window` (any source); first-
 * load with no metric data renders a tiny placeholder so layout doesn't jump.
 */
export function ContextBadge() {
  const metrics = useAssistantStore((s) => s.metrics);
  const fallbackWindow = useAssistantStore((s) => s.contextWindow);
  const fallbackSource = useAssistantStore((s) => s.contextSource);

  const ctxWindow = metrics.context_window || fallbackWindow;
  const ctxSource = (metrics.context_source as string) || fallbackSource;

  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLDivElement>(null);

  // Close popover on outside click.
  useEffect(() => {
    if (!open) return;
    function onDoc(ev: MouseEvent) {
      if (!containerRef.current) return;
      if (!containerRef.current.contains(ev.target as Node)) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  if (!ctxWindow) {
    return null;
  }

  const promptPct =
    typeof metrics.prompt_pct === "number" && metrics.prompt_pct > 0
      ? metrics.prompt_pct
      : metrics.prompt_tokens && ctxWindow
        ? metrics.prompt_tokens / ctxWindow
        : 0;
  const pctText = `${Math.round(promptPct * 100)}%`;
  const llmText =
    typeof metrics.llm_ms === "number" && metrics.llm_ms > 0
      ? `${(metrics.llm_ms / 1000).toFixed(1)}s`
      : "—";
  const tpsText =
    typeof metrics.tokens_per_second === "number" && metrics.tokens_per_second > 0
      ? `${metrics.tokens_per_second.toFixed(0)} tok/s`
      : "—";

  const tone =
    promptPct < 0.6
      ? "border-emerald-400/40 bg-emerald-500/15 text-emerald-100"
      : promptPct < 0.85
        ? "border-amber-300/40 bg-amber-400/15 text-amber-100"
        : "border-rose-400/50 bg-rose-500/15 text-rose-100";

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={`flex items-center gap-2 rounded-full border px-3 py-1 text-xs font-medium tabular-nums transition hover:brightness-110 ${tone}`}
        title="Click for full breakdown"
      >
        <span>{pctText}</span>
        <span className="text-ink-100/40">·</span>
        <span>{llmText}</span>
        <span className="text-ink-100/40">·</span>
        <span>{tpsText}</span>
      </button>
      {open ? <ContextPopover ctxWindow={ctxWindow} ctxSource={ctxSource} /> : null}
    </div>
  );
}

interface PopoverProps {
  ctxWindow: number;
  ctxSource: string;
}

function ContextPopover({ ctxWindow, ctxSource }: PopoverProps) {
  const m = useAssistantStore((s) => s.metrics);
  const promptTokens = m.prompt_tokens ?? 0;
  const promptPct =
    typeof m.prompt_pct === "number" && m.prompt_pct > 0
      ? m.prompt_pct
      : promptTokens && ctxWindow
        ? promptTokens / ctxWindow
        : 0;
  const fillPct = Math.min(100, Math.round(promptPct * 100));

  const sourceLabel: Record<string, string> = {
    ollama_show: "auto-detected from Ollama",
    config: "from config",
    fallback: "default fallback",
  };
  return (
    <div className="absolute right-0 top-9 z-50 w-80 rounded-xl border border-white/10 bg-black/85 p-4 text-xs text-ink-100/85 shadow-lg backdrop-blur">
      <div className="flex items-center justify-between">
        <div className="font-semibold text-ink-100">Context window</div>
        <div className="text-ink-100/50">
          {ctxWindow.toLocaleString()} tokens
        </div>
      </div>
      <div className="mt-1 text-[10px] uppercase tracking-wider text-ink-100/40">
        {sourceLabel[ctxSource] ?? ctxSource}
      </div>

      <div className="mt-3 h-2 w-full overflow-hidden rounded-full bg-white/10">
        <div
          className={`h-full ${
            promptPct < 0.6
              ? "bg-emerald-400"
              : promptPct < 0.85
                ? "bg-amber-400"
                : "bg-rose-500"
          }`}
          style={{ width: `${fillPct}%` }}
        />
      </div>
      <div className="mt-1 flex justify-between text-[11px] tabular-nums text-ink-100/60">
        <span>{promptTokens.toLocaleString()} used</span>
        <span>{Math.round(promptPct * 100)}%</span>
      </div>

      <table className="mt-4 w-full text-[11px]">
        <tbody className="divide-y divide-white/5">
          <Row label="System" value={m.system_tokens} />
          <Row label="Summary" value={m.summary_tokens} />
          <Row label="RAG" value={m.rag_tokens} />
          <Row label="History" value={m.history_tokens} />
          <Row label="User" value={m.user_tokens} />
          {m.tool_tokens ? <Row label="Tool" value={m.tool_tokens} /> : null}
        </tbody>
      </table>

      <div className="mt-3 grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] tabular-nums">
        <Stat label="LLM time" value={fmtMs(m.llm_ms)} />
        <Stat label="TTS time" value={fmtMs(m.tts_ms)} />
        <Stat label="Eval" value={fmtMs(m.eval_duration_ms)} />
        <Stat label="Prompt eval" value={fmtMs(m.prompt_eval_duration_ms)} />
        <Stat
          label="Tokens/sec"
          value={
            m.tokens_per_second ? `${m.tokens_per_second.toFixed(1)}` : "—"
          }
        />
        <Stat
          label="Tokens"
          value={`${promptTokens.toLocaleString()} / ${(m.completion_tokens ?? 0).toLocaleString()}`}
        />
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        {m.summary_active ? (
          <Tag tone="indigo">
            summary covers {m.summary_messages ?? 0} msgs
          </Tag>
        ) : (
          <Tag tone="muted">no summary yet</Tag>
        )}
        {m.history_dropped_count ? (
          <Tag tone="amber">
            dropped {m.history_dropped_count} from history
          </Tag>
        ) : null}
        {m.compaction_triggered ? <Tag tone="rose">compacted</Tag> : null}
        {m.compactions_total ? (
          <Tag tone="muted">
            {m.compactions_total} compaction{m.compactions_total === 1 ? "" : "s"}
          </Tag>
        ) : null}
      </div>
    </div>
  );
}

function fmtMs(value: number | undefined): string {
  if (!value) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

function Row({ label, value }: { label: string; value: number | undefined }) {
  if (!value) return null;
  return (
    <tr>
      <td className="py-1 text-ink-100/60">{label}</td>
      <td className="py-1 text-right tabular-nums">{value.toLocaleString()}</td>
    </tr>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-ink-100/55">{label}</span>
      <span>{value}</span>
    </div>
  );
}

function Tag({
  tone,
  children,
}: {
  tone: "indigo" | "amber" | "rose" | "muted";
  children: React.ReactNode;
}) {
  const palette: Record<string, string> = {
    indigo: "bg-indigo-500/15 border-indigo-300/30 text-indigo-100",
    amber: "bg-amber-400/15 border-amber-300/30 text-amber-100",
    rose: "bg-rose-500/15 border-rose-400/30 text-rose-100",
    muted: "bg-white/5 border-white/10 text-ink-100/60",
  };
  return (
    <span
      className={`rounded-full border px-2 py-0.5 text-[10px] uppercase tracking-wider ${palette[tone]}`}
    >
      {children}
    </span>
  );
}
