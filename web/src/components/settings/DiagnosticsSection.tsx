import { useEffect, useState } from "react";
import { debugLog } from "../../log";
import { useAssistantStore } from "../../store";
import type { MetricsResponse, MetricsSnapshot } from "../../types";
import { PersonaRegressionPanel } from "./PersonaRegressionPanel";
import { Section } from "./SettingsSection";

interface DiagnosticsProps {
  metrics: MetricsResponse | null;
  liveLastMetrics: MetricsSnapshot;
  /** Apply a partial settings patch (mirrors the outer drawer's
   * ``apply`` helper). Used by the debug-logging toggle to PATCH
   * ``logging.ui_log_enabled``. */
  onApplyPatch: (patch: Record<string, unknown>) => Promise<void> | void;
  busy: boolean;
}

export function DiagnosticsSection({
  metrics,
  liveLastMetrics,
  onApplyPatch,
  busy,
}: DiagnosticsProps) {
  // Prefer the live store metrics (back-filled with tts_ms via WS) over the
  // /api/metrics snapshot for the "last turn" rows; fall back to /api/metrics
  // if the store is empty (e.g. drawer opened pre-first-turn).
  const last =
    Object.keys(liveLastMetrics).length > 0
      ? liveLastMetrics
      : (metrics?.last ?? {});
  const avg = metrics?.average ?? {};
  const config = metrics?.config;

  const ctxWindow = config?.context_window ?? last.context_window ?? 0;
  const ctxSource = config?.context_source ?? last.context_source ?? "fallback";
  const promptTokens = last.prompt_tokens ?? 0;
  const promptPct =
    typeof last.prompt_pct === "number" && last.prompt_pct > 0
      ? last.prompt_pct
      : promptTokens && ctxWindow
        ? promptTokens / ctxWindow
        : 0;
  const fillPct = Math.min(100, Math.round(promptPct * 100));
  const sourceLabel: Record<string, string> = {
    client: "auto-detected from provider",
    ollama_show: "auto-detected from Ollama", // legacy label, kept for back-compat
    config: "from config",
    fallback: "default fallback",
  };

  return (
    <Section title="Diagnostics">
      <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
        <div className="flex items-baseline justify-between gap-2 text-[11px]">
          <span className="font-semibold uppercase tracking-wide text-ink-100/60">
            Context fill
          </span>
          <span className="text-ink-100/50">
            {ctxWindow ? ctxWindow.toLocaleString() : "—"} tokens ·{" "}
            <span className="text-ink-100/40">
              {sourceLabel[ctxSource] ?? ctxSource}
            </span>
          </span>
        </div>
        <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-white/10">
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
      </div>

      <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ink-100/60">
          Last turn
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] tabular-nums">
          <Stat label="Capture" value={fmtMs(last.capture_ms)} />
          <Stat label="STT" value={fmtMs(last.stt_ms)} />
          <Stat label="LLM" value={fmtMs(last.llm_ms)} />
          <Stat label="TTS" value={fmtMs(last.tts_ms)} />
          <Stat label="Total" value={fmtMs(last.total_ms)} />
          <Stat
            label="Tokens/sec"
            value={
              last.tokens_per_second
                ? `${last.tokens_per_second.toFixed(1)}`
                : "—"
            }
          />
          <Stat
            label="Prompt"
            value={(last.prompt_tokens ?? 0).toLocaleString()}
          />
          <Stat
            label="Completion"
            value={(last.completion_tokens ?? 0).toLocaleString()}
          />
        </div>
      </div>

      <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
        <div className="mb-2 flex items-baseline justify-between text-[11px] font-semibold uppercase tracking-wide text-ink-100/60">
          <span>Last 10 turns (avg)</span>
          {"window" in avg ? (
            <span className="text-[10px] font-normal normal-case text-ink-100/40">
              window={(avg as { window?: number }).window ?? 0}
            </span>
          ) : null}
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] tabular-nums">
          <Stat label="Capture" value={fmtMs(avg.capture_ms)} />
          <Stat label="STT" value={fmtMs(avg.stt_ms)} />
          <Stat label="LLM" value={fmtMs(avg.llm_ms)} />
          <Stat label="TTS" value={fmtMs(avg.tts_ms)} />
          <Stat label="Total" value={fmtMs(avg.total_ms)} />
          <Stat
            label="Tokens/sec"
            value={
              avg.tokens_per_second
                ? `${avg.tokens_per_second.toFixed(1)}`
                : "—"
            }
          />
          <Stat
            label="Prompt avg"
            value={
              avg.prompt_tokens
                ? Math.round(avg.prompt_tokens).toLocaleString()
                : "—"
            }
          />
          <Stat
            label="Fill avg"
            value={
              avg.prompt_pct
                ? `${Math.round(avg.prompt_pct * 100)}%`
                : "—"
            }
          />
        </div>
      </div>

      <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ink-100/60">
          Summary state
        </div>
        <div className="space-y-1 text-[11px]">
          <RowMini
            label="Active"
            value={last.summary_active ? "yes" : "no"}
          />
          <RowMini
            label="Messages covered"
            value={String(last.summary_messages ?? 0)}
          />
          <RowMini
            label="Compactions this session"
            value={String(last.compactions_total ?? 0)}
          />
          <RowMini
            label="Last turn compacted"
            value={last.compaction_triggered ? "yes" : "no"}
          />
          <RowMini
            label="Dropped from history"
            value={String(last.history_dropped_count ?? 0)}
          />
          {config ? (
            <>
              <RowMini
                label="Compaction threshold"
                value={`${Math.round(config.max_prompt_tokens_pct * 100)}%`}
              />
              <RowMini
                label="Summary idle"
                value={`${config.summary_idle_seconds}s`}
              />
            </>
          ) : null}
        </div>
      </div>

      <PersonaRegressionPanel />

      <DebugLoggingBlock onApplyPatch={onApplyPatch} busy={busy} />
    </Section>
  );
}

interface DebugLoggingBlockProps {
  onApplyPatch: (patch: Record<string, unknown>) => Promise<void> | void;
  busy: boolean;
}

/**
 * Debug-logging block inside ``Diagnostics``.
 *
 * The toggle PATCHes ``logging.ui_log_enabled`` so the change persists
 * on the backend; the WS ``logging_settings_changed`` broadcast then
 * flips :func:`debugLog.setEnabled` on every connected tab (this one
 * included, via :file:`useAssistantSocket.ts`). The local "Download"
 * + "Clear" buttons operate on the in-memory ring buffer so they work
 * even when the backend is offline.
 *
 * We poll :func:`debugLog.size` once a second to drive the entry
 * counter without subscribing every keystroke; the cost is one
 * function call per render frame instead of a Zustand subscription
 * that would re-render the whole drawer on every push.
 */
function DebugLoggingBlock({ onApplyPatch, busy }: DebugLoggingBlockProps) {
  const loggingSettings = useAssistantStore((s) => s.loggingSettings);
  const enabled = loggingSettings.ui_log_enabled;

  // Counter ticks at ~1Hz when the toggle is on so the user sees the
  // buffer grow as they reproduce. When off we still refresh once so
  // the displayed count matches whatever the ring had at the moment
  // of disabling.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!enabled) {
      setTick((t) => t + 1);
      return;
    }
    const handle = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(handle);
  }, [enabled]);
  // `tick` is read implicitly via the snapshot below; the variable is
  // referenced here so the linter doesn't warn it's unused.
  void tick;

  const size = debugLog.size();
  const lastFlush = debugLog.lastFlushAt();
  const lastFlushLabel = lastFlush
    ? `${Math.max(0, Math.round((Date.now() - lastFlush) / 1000))}s ago`
    : "—";

  const handleToggle = (next: boolean) => {
    void onApplyPatch({ logging: { ui_log_enabled: next } });
  };

  return (
    <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
      <div className="mb-2 flex items-baseline justify-between text-[11px] font-semibold uppercase tracking-wide text-ink-100/60">
        <span>Debug logging</span>
        <span className="text-[10px] font-normal normal-case text-ink-100/40">
          UI → app.log
        </span>
      </div>
      <p className="mb-3 text-[11px] leading-snug text-ink-100/55">
        Captures WS events, avatar channel decisions, and settings
        changes into <code className="font-mono text-ink-100/70">data/app.log</code> with a{" "}
        <code className="font-mono text-ink-100/70">[ui]</code> prefix. Leave off in normal use;
        flip on, reproduce a bug, then share the log file.
      </p>
      <label className="flex cursor-pointer items-center gap-2 text-[12px] text-ink-100/85">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => handleToggle(e.target.checked)}
          disabled={busy}
          className="h-4 w-4 accent-violet-400"
        />
        Enable debug logging
      </label>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
        <button
          type="button"
          onClick={() => debugLog.download()}
          disabled={size === 0}
          className="rounded border border-white/10 bg-white/5 px-2 py-1 text-ink-100/80 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Download buffer
        </button>
        <button
          type="button"
          onClick={() => {
            debugLog.clear();
            setTick((t) => t + 1);
          }}
          disabled={size === 0}
          className="rounded border border-white/10 bg-white/5 px-2 py-1 text-ink-100/80 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Clear
        </button>
        <span className="ml-auto text-ink-100/50 tabular-nums">
          {size.toLocaleString()} entries · last flush {lastFlushLabel}
        </span>
      </div>
    </div>
  );
}

function fmtMs(value: number | undefined): string {
  if (!value) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-ink-100/55">{label}</span>
      <span className="text-ink-100/85">{value}</span>
    </div>
  );
}

function RowMini({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between text-[11px]">
      <span className="text-ink-100/55">{label}</span>
      <span className="font-mono text-ink-100/80">{value}</span>
    </div>
  );
}
