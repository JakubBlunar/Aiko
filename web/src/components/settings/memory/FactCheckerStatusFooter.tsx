import { useCallback, useEffect, useState } from "react";
import { api } from "../../../api";
import { formatRelative } from "../SettingsSection";

export interface FactCheckerSnapshot {
  enabled: boolean;
  pending: number;
  queue_total: number;
  last_verified_at: string | null;
  hour_used: number;
  hour_cap: number;
  day_used: number;
  day_cap: number;
}

export function FactCheckerStatusFooter() {
  const [status, setStatus] = useState<FactCheckerSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.factCheckerStatus();
      setStatus(data);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Re-poll every 30 seconds while the drawer is open. Cheap (one
    // GET) and gives a live view of the queue draining.
    const t = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(t);
  }, [refresh]);

  if (status === null) {
    return null;
  }
  return (
    <div
      className="mt-3 rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/60"
      title={
        "F1 background fact-checker: pops one claim per idle tick, " +
        "calls web_search, then distils a JSON verdict via the main chat model. " +
        "Cancels cleanly on the next user turn (the claim requeues at the front)."
      }
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={status.enabled ? "text-emerald-300/80" : "text-rose-300/80"}>
          fact-checker {status.enabled ? "on" : "off"}
        </span>
        <span
          className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-emerald-200/80"
          title={
            "Privacy gate is always on. Memories containing your name, " +
            "first-person pronouns, emails, phone numbers, URLs, or " +
            "street addresses never enter the fact-check queue. " +
            "Claims with the user/assistant name embedded are redacted " +
            "before the web query is sent. See app/core/memory/fact_check_privacy.py."
          }
        >
          private
        </span>
        <span>queue: {status.pending} pending</span>
        <span>last verified: {formatRelative(status.last_verified_at)}</span>
        <span>
          {status.hour_used}/{status.hour_cap} this hour ·{" "}
          {status.day_used}/{status.day_cap} today
        </span>
        <button
          type="button"
          onClick={refresh}
          className="ml-auto rounded border border-white/10 px-2 py-0.5 text-ink-100/50 hover:border-ink-400"
        >
          refresh
        </button>
      </div>
      {error ? (
        <div className="mt-1 text-rose-200/70">{error}</div>
      ) : null}
    </div>
  );
}
