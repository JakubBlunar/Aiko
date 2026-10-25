import { useEffect, useState } from "react";
import { api } from "../../api";
import { useAssistantStore } from "../../store";
import { Section } from "./SettingsSection";
import { isVeniceProvider, usageNumber, usageUsd, type ProviderUsage } from "./providerUsage";

const control = "rounded-md border border-white/10 bg-white/5 px-2 py-1 text-xs text-ink-100";

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return <div className="rounded-lg border border-white/10 bg-white/[0.025] p-3">
    <div className="text-[11px] text-ink-100/50">{label}</div>
    <div className="mt-1 break-words text-base font-semibold tabular-nums text-ink-100">{value}</div>
    {hint && <div className="mt-1 text-[10px] text-ink-100/45">{hint}</div>}
  </div>;
}

export function UsageDashboard({ data }: { data: ProviderUsage }) {
  const s = data.summary;
  const maxRequests = Math.max(1, ...data.daily.map(d => d.requests ?? 0));
  return <div className="space-y-3">
    <p className="text-[11px] text-ink-100/50">
      Venice account-wide usage, including other API keys and web usage reported by Venice.
      {" "}{data.start_date} to {data.end_date} (UTC calendar days).
      {" "}Analytics can lag by about 10 minutes; request counts may update sooner.
    </p>
    <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
      <Stat label="Total text tokens" value={usageNumber(s.total_tokens)}
        hint={`${usageNumber(s.input_tokens)} input / ${usageNumber(s.output_tokens)} output`} />
      <Stat label="Billed text requests"
        value={`${s.requests != null && !data.requests_complete ? "At least " : ""}${usageNumber(s.requests)}`}
        hint="Unique billed request IDs; excludes unbilled failures" />
      <Stat label="Prompt cache hit rate" value={s.cache_hit_pct == null ? "Not reported" : `${usageNumber(s.cache_hit_pct, 1)}%`}
        hint="Cached input tokens / all input tokens" />
      <Stat label="USD spend" value={usageUsd(s.usd)} hint="All model types; reported USD charges" />
      <Stat label="DIEM spend" value={usageNumber(s.diem, 4)} hint="Shown separately from USD charges" />
      <Stat label="Cached input tokens" value={usageNumber(s.cached_tokens)}
        hint={`${usageNumber(s.cache_write_tokens)} cache-write tokens`} />
    </div>
    {data.balances && <div className="grid grid-cols-2 gap-2 sm:grid-cols-4">
      <Stat label="USD balance" value={usageUsd(data.balances.usd)} />
      <Stat label="DIEM balance" value={usageNumber(data.balances.diem, 4)} />
      <Stat label="Bundled credits" value={usageNumber(data.balances.bundledCredits, 4)} />
      <Stat label="Earned credits" value={usageNumber(data.balances.earnedCredits, 4)} />
    </div>}
    {!data.requests_complete && <p className="text-xs text-amber-200" role="status">
      Request counts are incomplete. Some billing rows have no request ID, or history could not be fully loaded.
    </p>}
    {data.warnings.map((warning, i) => <p key={i} className="text-xs text-amber-200" role="status">{warning}</p>)}
    <details className="rounded-lg border border-white/10 p-3" open>
      <summary className="cursor-pointer text-xs text-ink-100/70">Requests per day (UTC)</summary>
      <div className="mt-3 space-y-1">
        {data.daily.map(d => <div key={d.date} className="flex items-center gap-2 text-[11px] text-ink-100/60">
          <span className="w-20 shrink-0 font-mono">{d.date.slice(5)}</span>
          <div className="h-2 flex-1 rounded bg-white/5" aria-hidden="true">
            <div className="h-2 rounded bg-violet-400/70" style={{ width: `${100 * (d.requests ?? 0) / maxRequests}%` }} />
          </div>
          <span className="w-16 text-right tabular-nums">{usageNumber(d.requests)}</span>
        </div>)}
      </div>
    </details>
    <details className="rounded-lg border border-white/10 p-3">
      <summary className="cursor-pointer text-xs text-ink-100/70">Usage by text model</summary>
      {data.models.length === 0 ? <p className="mt-2 text-xs text-ink-100/50">No text usage in this period.</p>
        : <div className="mt-2 space-y-2">{data.models.map((m, i) => <div key={i} className="border-t border-white/5 pt-2 text-xs">
          <div className="font-medium text-ink-100/80">{m.name}</div>
          <div className="mt-1 text-ink-100/50">{usageNumber(m.total_tokens)} tokens · {usageUsd(m.usd)} · {usageNumber(m.diem, 4)} DIEM</div>
          <div className="text-ink-100/50">Input {usageNumber(m.input_tokens)} · Output {usageNumber(m.output_tokens)} · Cache read {usageNumber(m.cached_tokens)} · Cache write {usageNumber(m.cache_write_tokens)}</div>
        </div>)}</div>}
    </details>
    <details className="rounded-lg border border-white/10 p-3">
      <summary className="cursor-pointer text-xs text-ink-100/70">Current text model pricing</summary>
      <p className="mt-2 text-[11px] text-ink-100/50">USD per million tokens. Current catalogue rates; historical spend above uses billing data.</p>
      {data.prices.length === 0 && <p className="mt-2 text-xs text-ink-100/50">Pricing is unavailable.</p>}
      <div className="mt-2 max-h-64 space-y-2 overflow-y-auto">{data.prices.map(p => <div key={p.id} className="border-t border-white/5 pt-2 text-xs">
        <div className="text-ink-100/80">{p.name} <span className="text-[10px] text-ink-100/40">{p.id}</span></div>
        <div className="text-ink-100/50">Input {usageUsd(p.input)} · Output {usageUsd(p.output)} · Cache read {usageUsd(p.cache_input)} · Cache write {usageUsd(p.cache_write)}</div>
      </div>)}</div>
    </details>
    <p className="text-[10px] text-ink-100/40">Fetched {data.fetched_at.replace("T", " ").slice(0, 19)} UTC. Refresh is cached locally for one minute.</p>
  </div>;
}

export function ProviderUsageSection() {
  const providers = useAssistantStore(s => s.llmProviders);
  const upsertProvider = useAssistantStore(s => s.upsertLlmProvider);
  const venice = (providers ?? []).filter(isVeniceProvider);
  const [chosenId, setChosenId] = useState("");
  const providerId = venice.some(p => p.id === chosenId) ? chosenId : venice[0]?.id ?? "";
  const [days, setDays] = useState(7);
  const [refresh, setRefresh] = useState(0);
  const [data, setData] = useState<ProviderUsage | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [adminKey, setAdminKey] = useState("");
  const [savingKey, setSavingKey] = useState(false);
  const [keyError, setKeyError] = useState("");

  async function connectBilling() {
    if (!adminKey.trim() || savingKey) return;
    setSavingKey(true);
    setKeyError("");
    try {
      const saved = await api.addLlmProvider({ template_id: "venice", draft: {
        name: "Venice statistics", api_key: adminKey.trim(), api_key_env: "",
      } });
      setAdminKey("");
      upsertProvider(saved);
      setChosenId(saved.id);
    } catch (e) {
      setKeyError(e instanceof Error ? e.message : "Could not save the statistics provider.");
    } finally {
      setSavingKey(false);
    }
  }
  useEffect(() => {
    let active = true;
    setData(null);
    setError("");
    if (!providerId) { setBusy(false); return; }
    setBusy(true);
    void api.getProviderUsage(providerId, days).then(result => {
      if (active) setData(result);
    }).catch((e: unknown) => {
      if (active) setError(e instanceof Error ? e.message : "Could not load usage.");
    }).finally(() => { if (active) setBusy(false); });
    return () => { active = false; };
  }, [providerId, days, refresh, providers]);

  return <Section title="Provider usage">
    <details className="rounded-lg border border-white/10 p-3">
      <summary className="cursor-pointer text-xs text-ink-100/70">Connect Venice billing statistics</summary>
      <p className="my-2 text-[11px] text-ink-100/50">
        Create an Admin API key in <a href="https://venice.ai/settings/api" target="_blank"
          rel="noreferrer" className="text-violet-300 underline">Venice settings</a>.
        {" "}Saving adds a separate provider named Venice statistics using the existing credential storage.
        It stays unassigned to chat roles. Manage or remove it in Saved providers above.
      </p>
      <div className="flex flex-wrap gap-2">
        <input aria-label="Venice Admin API key" type="password" autoComplete="new-password"
          value={adminKey} onChange={e => setAdminKey(e.target.value)}
          className={`${control} min-w-0 flex-1`} placeholder="Venice Admin API key" />
        <button className={control} disabled={!adminKey.trim() || savingKey} onClick={() => void connectBilling()}>
          {savingKey ? "Saving..." : "Save statistics provider"}
        </button>
      </div>
      {keyError && <p className="mt-2 text-xs text-rose-200" role="alert">{keyError}</p>}
    </details>
    {!providerId ? <p className="text-xs text-ink-100/50">
      Add a Venice provider above to view account usage, cache statistics, pricing and balances.
      Account statistics for other providers are not available yet.
    </p> : <>
      <p className="text-[11px] text-ink-100/50">
        Billing access requires a Venice Admin API key. You can add a separate saved Venice
        provider for statistics and leave it unassigned to chat roles.
        {" "}<a href="https://venice.ai/settings/api" target="_blank" rel="noreferrer"
          className="text-violet-300 underline">Manage Venice keys</a>
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <select aria-label="Usage provider" value={providerId} onChange={e => setChosenId(e.target.value)} className={control}>
          {venice.map(p => <option key={p.id} value={p.id}>{p.name}</option>)}
        </select>
        <div className="flex gap-1" role="group" aria-label="Usage period">
          {[1, 7, 14, 30].map(n => <button key={n} className={`${control} ${days === n ? "border-violet-400/70 bg-violet-400/20" : ""}`}
            aria-pressed={days === n} onClick={() => setDays(n)}>{n === 1 ? "Today" : `${n}d`}</button>)}
        </div>
        <button className={control} disabled={busy} onClick={() => setRefresh(n => n + 1)}>Refresh</button>
      </div>
      {busy && <p className="text-xs text-ink-100/50" role="status">Loading Venice usage...</p>}
      {error && <p className="text-xs text-rose-200" role="alert">{error}</p>}
      {data && <UsageDashboard data={data} />}
    </>}
  </Section>;
}
