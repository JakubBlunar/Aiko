import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api";
import { Toggle } from "@/components/Toggle";
import { Section } from "../SettingsSection";
import { SchemaField } from "./SchemaField";
import type { PluginRow, PluginsSnapshot, PluginUpdate } from "./types";
import { subscribePluginsUpdated } from "./updates";

const buttonClass = "rounded border border-white/15 px-3 py-1.5 text-xs hover:bg-white/10 disabled:opacity-40";
const phases: Record<string, string> = {
  disabled: "Disabled", blocked: "Blocked by master switch", needs_configuration: "Needs configuration",
  installing: "Installing dependencies", starting: "Connecting", ready: "Active", applying: "Applying settings", error: "Error",
};

function message(error: unknown): string {
  const text = error instanceof Error ? error.message : String(error);
  const start = text.indexOf('{"detail":');
  if (start >= 0) {
    try {
      const detail = JSON.parse(text.slice(start)).detail;
      if (Array.isArray(detail)) return detail.map(row => `${row.path?.join(" / ") || "Settings"}: ${row.message}`).join("; ");
      if (typeof detail === "string") return detail;
    } catch { /* Keep the original network error. */ }
  }
  return text;
}

export function PluginCard({ row, onUpdate }: { row: PluginRow; onUpdate: () => void }) {
  const [draft, setDraft] = useState<Record<string, unknown>>({});
  const [baseRevision, setBaseRevision] = useState(row.revision);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const dirty = Object.keys(draft).length > 0;
  const conflict = dirty && baseRevision !== row.revision;
  const config = { ...row.config, ...draft };
  const advanced = new Set(row.ui.advanced ?? []);

  const mutate = async (patch: Omit<PluginUpdate, "revision">, useDraft = false) => {
    setBusy(true); setError("");
    try {
      const next = await api.updatePlugin(row.id, { revision: useDraft ? baseRevision : row.revision, ...patch });
      if (useDraft || patch.reset) setDraft({});
      else if (!conflict) setBaseRevision(next.revision);
      onUpdate();
    } catch (err) { setError(message(err)); onUpdate(); }
    finally { setBusy(false); }
  };
  const field = ([key, schema]: [string, NonNullable<PluginRow["schema"]["properties"]>[string]]) =>
    <SchemaField key={key} label={key} schema={schema} root={row.schema} value={config[key]}
      onChange={value => {
        if (!dirty) setBaseRevision(row.revision);
        setDraft(old => ({ ...old, [key]: value }));
      }} />;
  const fields = Object.entries(row.schema.properties ?? {});
  return <article className="min-w-0 space-y-3 rounded-lg border border-white/10 bg-white/[0.02] p-3">
    <div className="flex flex-wrap items-center justify-between gap-2">
      <Toggle checked={row.enabled} disabled={busy} onChange={enabled => void mutate({ enabled })}>{row.name}</Toggle>
      <span className={`text-xs ${row.phase === "error" ? "text-red-300" : "text-ink-100/50"}`} role="status">{phases[row.phase] ?? row.phase}</span>
    </div>
    {row.ui.description && <p className="text-xs leading-relaxed text-ink-100/60">{row.ui.description}</p>}
    {row.phase === "ready" ? <p className="text-xs text-ink-100/45">Saved settings are active{row.tool_count ? ` · ${row.tool_count} background tools` : ""}.</p>
      : row.enabled && <p className="text-xs text-ink-100/45">Settings are saved. Tools become available when this plugin is active.</p>}
    {row.access.length > 0 && <div className="space-y-1 rounded bg-black/15 p-2 text-xs text-ink-100/60">
      <p>{row.phase === "ready" ? "Active access" : "Configured access"}</p>
      {row.access.map((access, index) => <p key={index} className="break-all">
        {access.name && `${access.name}: `}{access.path} <span className="text-ink-100/40">({access.mode === "read_only" ? "read only" : access.mode === "read_write" ? "read and write; task approval rules apply" : access.mode})</span>
        {access.scope && <span className="block text-ink-100/45">Allowed relative paths: {access.scope.join(", ")}</span>}
      </p>)}
    </div>}
    {row.error && <p className="break-words text-xs text-amber-200">{row.error}</p>}
    {row.warnings.map((warning, index) => <p key={index} className="break-words text-xs text-amber-200">{warning}</p>)}
    <fieldset disabled={busy} className="min-w-0 space-y-3">
      {fields.filter(([key]) => !advanced.has(key)).map(field)}
      {fields.some(([key]) => advanced.has(key)) && <details className="space-y-3">
        <summary className="cursor-pointer text-xs text-ink-100/60">Advanced settings</summary>
        {fields.filter(([key]) => advanced.has(key)).map(field)}
      </details>}
    </fieldset>
    {conflict && <div className="space-y-2 text-xs text-amber-200">
      <p>Settings changed elsewhere. Your draft is preserved. Review it against the current settings before applying.</p>
      <button type="button" className={buttonClass} disabled={busy} onClick={() => setBaseRevision(row.revision)}>Keep draft over current settings</button>
    </div>}
    {error && <p role="alert" className="break-words text-xs text-red-300">{error}</p>}
    <div className="flex flex-wrap gap-2">
      {fields.length > 0 && <>
        <button type="button" className={buttonClass} disabled={!dirty || busy || conflict} onClick={() => void mutate({ config: draft }, true)}>Apply settings</button>
        <button type="button" className={buttonClass} disabled={!dirty || busy} onClick={() => { setDraft({}); setError(""); }}>Discard draft</button>
        <button type="button" className={buttonClass} disabled={busy} onClick={() => void mutate({ reset: true })}>Reset overrides</button>
      </>}
      {["error", "needs_configuration"].includes(row.phase) && row.enabled && <button type="button" className={buttonClass} disabled={busy} onClick={async () => {
        setBusy(true); setError("");
        try { await api.retryPlugin(row.id, row.revision); onUpdate(); }
        catch (err) { setError(message(err)); }
        finally { setBusy(false); }
      }}>Retry activation</button>}
      {dirty && <span className="self-center text-xs text-amber-200">Unsaved changes</span>}
    </div>
  </article>;
}

export function PluginsSection() {
  const [snapshot, setSnapshot] = useState<PluginsSnapshot | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const sequence = useRef(0);
  const refresh = useCallback(async () => {
    const request = ++sequence.current;
    try {
      const next = await api.getPlugins();
      if (request === sequence.current) { setSnapshot(next); setError(""); }
    } catch (err) { if (request === sequence.current) setError(message(err)); }
  }, []);
  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => { if (!document.hidden) void refresh(); }, 2000);
    const unsubscribe = subscribePluginsUpdated(() => void refresh());
    return () => { window.clearInterval(timer); unsubscribe(); sequence.current++; };
  }, [refresh]);
  return <Section title="Plugins">
    <p className="mb-3 text-xs text-ink-100/50">Manage plugin tools and their access here. Changes apply while Aiko is running.</p>
    <p className="mb-3 text-xs text-ink-100/40">Reset overrides restores bundle defaults and any existing plugin-local configuration.</p>
    {error && <p role="alert" className="mb-2 break-words text-xs text-red-300">{error}</p>}
    {!snapshot ? <button type="button" className={buttonClass} onClick={() => void refresh()}>Reload plugins</button> : <div className="min-w-0 space-y-3">
      <Toggle checked={snapshot.enabled} disabled={busy} onChange={async enabled => {
        setBusy(true);
        try { setSnapshot(await api.setPluginsEnabled(enabled)); setError(""); }
        catch (err) { setError(message(err)); }
        finally { setBusy(false); }
      }}>Plugin system enabled</Toggle>
      {snapshot.plugins.map(row => <PluginCard key={row.id} row={row} onUpdate={() => void refresh()} />)}
      {snapshot.plugins.length === 0 && <p className="text-xs text-ink-100/50">No plugin bundles found.</p>}
    </div>}
  </Section>;
}
