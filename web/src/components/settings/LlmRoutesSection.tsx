import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../../api";
import { useAssistantStore } from "../../store";
import type {
  LlmProvider,
  LlmProviderTestResult,
  LlmRoute,
} from "../../types";
import { Section } from "./SettingsSection";

/**
 * PR 2 — Role -> Provider / Model / Context / Max-tokens table.
 *
 * One row per active role (``main_chat``, ``worker_default``, plus
 * any future ``heavy_workers`` etc. introduced server-side). The
 * Model column is a free-text combobox so users can pick from the
 * provider's curated suggestions OR type any custom id (parallels
 * the PR 1 combobox in ``ChatProviderSection``).
 *
 * Dirty rows are highlighted; unsaved changes survive tab switches
 * within the drawer because the draft state lives in this
 * component, not in the global store.
 *
 * Note: the legacy ``ChatProviderSection`` is kept alongside this
 * panel for back-compat — both edit ``main_chat``'s settings through
 * mirror-write paths on the backend. Users can stick with the
 * preset-card UX they're used to, or switch to the catalogue + roles
 * UX once they want multiple providers.
 */

interface RouteDraft {
  provider_id: string;
  model: string;
  context_window: number | null;
  max_tokens: number;
  temperature: number | null;
}

function routeToDraft(route: LlmRoute): RouteDraft {
  return {
    provider_id: route.provider_id,
    model: route.model,
    context_window: route.context_window,
    max_tokens: route.max_tokens,
    temperature: route.temperature,
  };
}

function draftsEqual(a: RouteDraft, b: LlmRoute): boolean {
  return (
    a.provider_id === b.provider_id &&
    a.model === b.model &&
    (a.context_window ?? null) === (b.context_window ?? null) &&
    a.max_tokens === b.max_tokens &&
    (a.temperature ?? null) === (b.temperature ?? null)
  );
}

function formatRoleLabel(role: string): string {
  // ``main_chat`` -> "Main chat"; future ``heavy_workers`` ->
  // "Heavy workers". Tabular case so the table reads cleanly without
  // hand-coding every role name.
  return role
    .split("_")
    .map((word) => (word ? word[0].toUpperCase() + word.slice(1) : ""))
    .join(" ");
}

export function LlmRoutesSection() {
  const pushToast = useAssistantStore((s) => s.pushToast);
  const providers = useAssistantStore((s) => s.llmProviders);
  const routes = useAssistantStore((s) => s.llmRoutes);
  const setProviders = useAssistantStore((s) => s.setLlmProviders);
  const setRoutes = useAssistantStore((s) => s.setLlmRoutes);
  const setRoute = useAssistantStore((s) => s.setLlmRoute);

  const [drafts, setDrafts] = useState<Record<string, RouteDraft>>({});
  // Per-provider model lists, fetched lazily when a row points at one.
  const [providerModels, setProviderModels] = useState<
    Record<string, string[]>
  >({});
  const [savingRole, setSavingRole] = useState<string | null>(null);
  const [testingRole, setTestingRole] = useState<string | null>(null);
  const [testResults, setTestResults] = useState<
    Record<string, LlmProviderTestResult>
  >({});
  const [loadError, setLoadError] = useState<string | null>(null);

  // First load: fetch the catalogue + routes.
  useEffect(() => {
    let cancelled = false;
    const load = async () => {
      try {
        const [provs, rts] = await Promise.all([
          api.listLlmProviders(),
          api.listLlmRoutes(),
        ]);
        if (cancelled) return;
        setProviders(provs.providers);
        setRoutes(rts.routes);
        setLoadError(null);
      } catch (exc) {
        if (cancelled) return;
        setLoadError(
          exc instanceof Error ? exc.message : "Failed to load LLM routes",
        );
      }
    };
    void load();
    return () => {
      cancelled = true;
    };
  }, [setProviders, setRoutes]);

  // Seed drafts when routes land. Existing dirty rows are preserved
  // so a refetch (WS broadcast) doesn't clobber in-progress edits.
  useEffect(() => {
    if (!routes) return;
    setDrafts((prev) => {
      const next = { ...prev };
      for (const [role, route] of Object.entries(routes)) {
        if (!(role in next)) {
          next[role] = routeToDraft(route);
        }
      }
      // Drop drafts for roles that no longer exist server-side.
      for (const key of Object.keys(next)) {
        if (!(key in routes)) delete next[key];
      }
      return next;
    });
  }, [routes]);

  // Lazy-fetch provider models when a row selects a new provider.
  const fetchModelsFor = useCallback(
    async (providerId: string) => {
      if (!providerId || providerModels[providerId] !== undefined) return;
      const provider = (providers ?? []).find((p) => p.id === providerId);
      if (!provider) return;
      try {
        // Reuse the legacy ?provider= shape; the server treats it as
        // a kind filter, not an id, so we pass the provider's kind.
        const models = await api.listModels(false, provider.kind);
        setProviderModels((cur) => ({ ...cur, [providerId]: models }));
      } catch {
        setProviderModels((cur) => ({ ...cur, [providerId]: [] }));
      }
    },
    [providers, providerModels],
  );

  const editRoute = useCallback(
    (role: string, patch: Partial<RouteDraft>) => {
      setDrafts((cur) => {
        const existing = cur[role];
        if (!existing) return cur;
        return { ...cur, [role]: { ...existing, ...patch } };
      });
      // Clear the test-result badge as soon as anything changes —
      // mirrors the ChatProviderSection convention.
      setTestResults((cur) => {
        if (!(role in cur)) return cur;
        const next = { ...cur };
        delete next[role];
        return next;
      });
    },
    [],
  );

  const saveRoute = useCallback(
    async (role: string) => {
      const draft = drafts[role];
      if (!draft) return;
      setSavingRole(role);
      try {
        const updated = await api.updateLlmRoute(role, {
          provider_id: draft.provider_id,
          model: draft.model,
          context_window: draft.context_window,
          max_tokens: draft.max_tokens,
          temperature: draft.temperature,
        });
        setRoute(role, updated);
        pushToast("info", `Route '${formatRoleLabel(role)}' saved.`);
      } catch (exc) {
        pushToast(
          "error",
          exc instanceof Error ? exc.message : `Failed to save '${role}'`,
        );
      } finally {
        setSavingRole(null);
      }
    },
    [drafts, setRoute, pushToast],
  );

  const testRoute = useCallback(
    async (role: string) => {
      const draft = drafts[role];
      if (!draft) return;
      setTestingRole(role);
      try {
        const result = await api.testLlmProvider(draft.provider_id, {
          model: draft.model,
          context_window: draft.context_window,
        });
        setTestResults((cur) => ({ ...cur, [role]: result }));
      } catch (exc) {
        setTestResults((cur) => ({
          ...cur,
          [role]: {
            success: false,
            error_code: "request_failed",
            error_message:
              exc instanceof Error ? exc.message : "Request failed",
          },
        }));
      } finally {
        setTestingRole(null);
      }
    },
    [drafts],
  );

  // ── Render ────────────────────────────────────────────────────

  if (loadError) {
    return (
      <Section title="LLM routes">
        <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          Failed to load: {loadError}
        </div>
      </Section>
    );
  }

  if (!providers || !routes) {
    return (
      <Section title="LLM routes">
        <p className="text-xs text-ink-100/50">Loading routes…</p>
      </Section>
    );
  }

  const orderedRoles = Object.keys(routes).sort((a, b) => {
    // ``main_chat`` first, then ``worker_default``, then anything
    // else alphabetically — keeps the most-used row at the top.
    const rank = (k: string) =>
      k === "main_chat" ? 0 : k === "worker_default" ? 1 : 2;
    const da = rank(a);
    const db = rank(b);
    if (da !== db) return da - db;
    return a.localeCompare(b);
  });

  return (
    <Section title="LLM routes">
      <p className="text-[11px] text-ink-100/50">
        Pick which saved provider serves each Aiko role. Main chat is the
        path you talk to; Worker default covers the ~24 background
        workers (reflection, dream, memory extraction, …). Two roles
        pointing at the same provider share one underlying connection.
      </p>

      {providers.length === 0 ? (
        <div className="rounded-md border border-amber-400/40 bg-amber-500/10 px-3 py-2 text-xs text-amber-200">
          No saved providers yet. Pick a preset above to add one — then
          come back here to wire roles to it.
        </div>
      ) : null}

      <div className="space-y-3">
        {orderedRoles.map((role) => {
          const draft = drafts[role];
          const currentRoute = routes[role];
          if (!draft || !currentRoute) return null;
          const dirty = !draftsEqual(draft, currentRoute);
          const selectedProvider = providers.find(
            (p) => p.id === draft.provider_id,
          );
          const modelOptions = computeModelOptions(
            providerModels[draft.provider_id] ?? [],
            selectedProvider,
            draft.model,
          );
          const testResult = testResults[role];
          return (
            <div
              key={role}
              className={[
                "rounded-md border px-3 py-2",
                dirty
                  ? "border-sky-500/60 bg-sky-500/[0.04]"
                  : "border-white/10 bg-black/30",
              ].join(" ")}
            >
              <div className="flex items-center justify-between">
                <div className="text-sm font-semibold text-ink-100">
                  {formatRoleLabel(role)}
                </div>
                {dirty ? (
                  <span className="text-[10px] text-sky-300">unsaved</span>
                ) : null}
              </div>

              {/* Provider */}
              <label className="mt-2 block">
                <span className="block text-[11px] text-ink-100/60">
                  Provider
                </span>
                <select
                  value={draft.provider_id}
                  onChange={(e) => {
                    editRoute(role, { provider_id: e.target.value });
                    void fetchModelsFor(e.target.value);
                  }}
                  onFocus={() => void fetchModelsFor(draft.provider_id)}
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1.5 text-sm text-ink-100"
                >
                  {providers.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name} ({p.kind === "ollama" ? "local" : "remote"})
                    </option>
                  ))}
                </select>
              </label>

              {/* Model — combobox (free-text + suggestions). */}
              <div className="mt-2">
                <label
                  htmlFor={`llm-route-model-${role}`}
                  className="block text-[11px] text-ink-100/60"
                >
                  Model
                </label>
                <input
                  id={`llm-route-model-${role}`}
                  type="text"
                  list={`llm-route-model-options-${role}`}
                  value={draft.model}
                  onChange={(e) => editRoute(role, { model: e.target.value })}
                  placeholder="Type or pick a model id"
                  autoComplete="off"
                  spellCheck={false}
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1.5 text-sm text-ink-100"
                />
                <datalist id={`llm-route-model-options-${role}`}>
                  {modelOptions.map((m) => (
                    <option key={m} value={m} />
                  ))}
                </datalist>
              </div>

              {/* Context + Max tokens + Temperature — three numbers
                  laid out flow-wise to keep the row compact. */}
              <div className="mt-2 flex flex-wrap gap-2">
                <label className="block">
                  <span className="block text-[11px] text-ink-100/60">
                    Context window
                  </span>
                  <input
                    type="number"
                    min={0}
                    max={1_048_576}
                    step={1024}
                    value={draft.context_window ?? 0}
                    onChange={(e) => {
                      const raw = Number.parseInt(e.target.value, 10);
                      const cleaned =
                        Number.isFinite(raw) && raw > 0
                          ? Math.min(1_048_576, Math.max(0, raw))
                          : null;
                      editRoute(role, { context_window: cleaned });
                    }}
                    placeholder="0 (auto)"
                    className="mt-1 w-28 rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
                  />
                </label>
                <label className="block">
                  <span className="block text-[11px] text-ink-100/60">
                    Max tokens
                  </span>
                  <input
                    type="number"
                    min={64}
                    max={8192}
                    value={draft.max_tokens}
                    onChange={(e) =>
                      editRoute(role, {
                        max_tokens: Math.max(
                          64,
                          Math.min(
                            8192,
                            Number.parseInt(e.target.value, 10) || 512,
                          ),
                        ),
                      })
                    }
                    className="mt-1 w-24 rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
                  />
                </label>
                <label className="block">
                  <span className="block text-[11px] text-ink-100/60">
                    Temperature
                  </span>
                  <input
                    type="number"
                    min={0}
                    max={2}
                    step={0.05}
                    value={draft.temperature ?? ""}
                    onChange={(e) => {
                      const raw = e.target.value;
                      const parsed = raw === "" ? null : Number.parseFloat(raw);
                      editRoute(role, {
                        temperature:
                          parsed === null || !Number.isFinite(parsed)
                            ? null
                            : parsed,
                      });
                    }}
                    placeholder="auto"
                    className="mt-1 w-20 rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
                  />
                </label>
              </div>

              {/* Test / Save buttons + result strip. */}
              <div className="mt-3 flex items-center gap-2">
                <button
                  type="button"
                  disabled={testingRole === role || !draft.model}
                  onClick={() => void testRoute(role)}
                  className="rounded-md border border-white/10 bg-black/40 px-3 py-1.5 text-xs text-ink-100 hover:bg-white/5 disabled:opacity-50"
                >
                  {testingRole === role ? "Testing…" : "Test"}
                </button>
                <button
                  type="button"
                  disabled={savingRole === role || !dirty}
                  onClick={() => void saveRoute(role)}
                  className="rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-500 disabled:opacity-50"
                >
                  {savingRole === role ? "Saving…" : "Save"}
                </button>
              </div>
              {testResult ? (
                testResult.success ? (
                  <p className="mt-2 text-xs text-emerald-400">
                    ✓ Connected in {testResult.latency_ms ?? 0} ms ·{" "}
                    {testResult.completion_tokens ?? 0} token
                    {testResult.completion_tokens === 1 ? "" : "s"} used
                  </p>
                ) : (
                  <div
                    role="alert"
                    className="mt-2 rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200"
                  >
                    <span className="font-semibold uppercase">
                      {testResult.error_code ?? "error"}:
                    </span>{" "}
                    {testResult.error_message ||
                      "Provider rejected the request."}
                  </div>
                )
              ) : null}
            </div>
          );
        })}
      </div>
    </Section>
  );
}

/**
 * Union of (live model list, current draft id) deduplicated. Keeping
 * this exported makes the source-text test happy and matches the
 * pattern used in ``ChatProviderSection.tsx``.
 */
function computeModelOptions(
  liveModels: string[],
  _selectedProvider: LlmProvider | undefined,
  currentModel: string,
): string[] {
  const seen = new Set<string>();
  const out: string[] = [];
  for (const raw of [...liveModels, currentModel]) {
    const id = (raw ?? "").trim();
    if (id && !seen.has(id)) {
      seen.add(id);
      out.push(id);
    }
  }
  return out;
}
