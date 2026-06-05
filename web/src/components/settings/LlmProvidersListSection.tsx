import { useCallback, useEffect, useMemo, useState } from "react";

import { api } from "../../api";
import { useAssistantStore } from "../../store";
import type { LlmProvider, LlmProviderPreset } from "../../types";
import { Section } from "./SettingsSection";

/**
 * PR 2 — Saved providers catalogue.
 *
 * Renders one row per entry in ``llm.providers`` with:
 *   - masked credentials,
 *   - inline credential edit (api_key / api_key_env),
 *   - inline edit for non-credential fields (name, base_url,
 *     extra_headers, keep_alive, timeout),
 *   - a per-row Test button that runs a one-token probe through the
 *     existing ``POST /api/llm/providers/{id}/test`` endpoint,
 *   - a Delete action (server returns 409 with a useful message when
 *     a route still references the provider — we surface it).
 *
 * "Add provider" picks one of the curated templates from
 * ``/api/llm/presets`` and appends a new catalogue row. The legacy
 * ``ChatProviderSection`` preset-card UI keeps working for the
 * single-active-provider story (it mirror-writes through the same
 * catalogue), so users who only need one provider never have to
 * interact with this list.
 */

interface ProviderDraft {
  name: string;
  base_url: string;
  api_key_env: string;
  keep_alive: string;
  timeout_seconds: number;
  extra_headers_json: string;
  // Credential draft fields. ``api_key`` is empty when the user hasn't
  // touched the password field; on save we only send it when touched.
  api_key: string;
  api_key_touched: boolean;
}

function providerToDraft(provider: LlmProvider): ProviderDraft {
  return {
    name: provider.name,
    base_url: provider.base_url,
    api_key_env: provider.api_key_env,
    keep_alive: provider.keep_alive,
    timeout_seconds: provider.timeout_seconds,
    extra_headers_json:
      Object.keys(provider.extra_headers || {}).length > 0
        ? JSON.stringify(provider.extra_headers, null, 2)
        : "",
    api_key: "",
    api_key_touched: false,
  };
}

function parseHeaders(json: string): Record<string, string> {
  const trimmed = (json || "").trim();
  if (!trimmed) return {};
  const parsed: unknown = JSON.parse(trimmed);
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("extra_headers must be a JSON object");
  }
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
    if (k && v !== null && v !== undefined) out[k] = String(v);
  }
  return out;
}

export function LlmProvidersListSection() {
  const pushToast = useAssistantStore((s) => s.pushToast);
  const providers = useAssistantStore((s) => s.llmProviders);
  const setProviders = useAssistantStore((s) => s.setLlmProviders);
  const upsertProvider = useAssistantStore((s) => s.upsertLlmProvider);
  const removeProviderLocally = useAssistantStore((s) => s.removeLlmProvider);

  const [presets, setPresets] = useState<LlmProviderPreset[]>([]);
  const [drafts, setDrafts] = useState<Record<string, ProviderDraft>>({});
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});
  const [busyProvider, setBusyProvider] = useState<string | null>(null);
  const [showAdd, setShowAdd] = useState(false);

  // Load the catalogue if the store doesn't have it yet. Other panels
  // (LlmRoutesSection) already fetch it; this is a safety net.
  useEffect(() => {
    if (providers !== null) return;
    void api
      .listLlmProviders()
      .then((r) => setProviders(r.providers))
      .catch(() => setProviders([]));
  }, [providers, setProviders]);

  useEffect(() => {
    void api
      .getLlmPresets()
      .then((r) => setPresets(r.presets))
      .catch(() => setPresets([]));
  }, []);

  // Seed drafts when providers change; preserve in-progress edits.
  useEffect(() => {
    if (!providers) return;
    setDrafts((prev) => {
      const next = { ...prev };
      for (const p of providers) {
        if (!(p.id in next)) {
          next[p.id] = providerToDraft(p);
        }
      }
      // Drop drafts for providers that no longer exist.
      const liveIds = new Set(providers.map((p) => p.id));
      for (const id of Object.keys(next)) {
        if (!liveIds.has(id)) delete next[id];
      }
      return next;
    });
  }, [providers]);

  const editDraft = useCallback(
    (providerId: string, patch: Partial<ProviderDraft>) =>
      setDrafts((cur) => {
        const existing = cur[providerId];
        if (!existing) return cur;
        return { ...cur, [providerId]: { ...existing, ...patch } };
      }),
    [],
  );

  const saveProvider = useCallback(
    async (providerId: string) => {
      const draft = drafts[providerId];
      if (!draft) return;
      setBusyProvider(providerId);
      try {
        const headers = parseHeaders(draft.extra_headers_json);
        // 1. Credentials first (mirrors the single-provider section).
        if (draft.api_key_touched) {
          const updated = await api.updateLlmProviderCredentials(providerId, {
            api_key: draft.api_key,
            api_key_env: draft.api_key_env,
          });
          upsertProvider(updated);
        } else if (draft.api_key_env !== undefined) {
          // Even an env-var-only credential change still goes through
          // the credentials endpoint to keep the audit trail tidy.
          const updated = await api.updateLlmProviderCredentials(providerId, {
            api_key_env: draft.api_key_env,
          });
          upsertProvider(updated);
        }
        // 2. Then the non-credential fields.
        const updated = await api.updateLlmProvider(providerId, {
          name: draft.name,
          base_url: draft.base_url,
          extra_headers: headers,
          keep_alive: draft.keep_alive,
          timeout_seconds: draft.timeout_seconds,
        });
        upsertProvider(updated);
        // Reset the touched flag so the masked placeholder reappears.
        setDrafts((cur) => ({
          ...cur,
          [providerId]: {
            ...cur[providerId],
            api_key: "",
            api_key_touched: false,
          },
        }));
        pushToast("info", `Provider '${updated.name}' saved.`);
      } catch (exc) {
        pushToast(
          "error",
          exc instanceof Error ? exc.message : "Failed to save provider",
        );
      } finally {
        setBusyProvider(null);
      }
    },
    [drafts, upsertProvider, pushToast],
  );

  const deleteProvider = useCallback(
    async (providerId: string) => {
      if (!window.confirm(`Delete provider '${providerId}'?`)) return;
      setBusyProvider(providerId);
      try {
        await api.deleteLlmProvider(providerId);
        removeProviderLocally(providerId);
        pushToast("info", "Provider deleted.");
      } catch (exc) {
        pushToast(
          "error",
          exc instanceof Error ? exc.message : "Failed to delete",
        );
      } finally {
        setBusyProvider(null);
      }
    },
    [removeProviderLocally, pushToast],
  );

  const testProvider = useCallback(
    async (providerId: string) => {
      setBusyProvider(providerId);
      try {
        const result = await api.testLlmProvider(providerId);
        if (result.success) {
          pushToast(
            "info",
            `'${providerId}' connected in ${result.latency_ms ?? 0} ms.`,
          );
        } else {
          pushToast(
            "error",
            `'${providerId}' test failed: ${
              result.error_message ?? result.error_code ?? "unknown"
            }`,
          );
        }
      } catch (exc) {
        pushToast(
          "error",
          exc instanceof Error ? exc.message : "Test failed",
        );
      } finally {
        setBusyProvider(null);
      }
    },
    [pushToast],
  );

  const addFromTemplate = useCallback(
    async (templateId: string | null) => {
      try {
        const draft = templateId
          ? { id: templateId }
          : { id: "custom", kind: "openai_compatible" as const };
        const created = await api.addLlmProvider({
          template_id: templateId ?? undefined,
          draft,
        });
        upsertProvider(created);
        setShowAdd(false);
        pushToast("info", `Provider '${created.name}' added.`);
      } catch (exc) {
        pushToast(
          "error",
          exc instanceof Error ? exc.message : "Failed to add provider",
        );
      }
    },
    [upsertProvider, pushToast],
  );

  const availableTemplates = useMemo(() => {
    if (!providers) return presets;
    const existingIds = new Set(providers.map((p) => p.id));
    return presets.filter((p) => !existingIds.has(p.id));
  }, [presets, providers]);

  if (!providers) {
    return (
      <Section title="Saved providers">
        <p className="text-xs text-ink-100/50">Loading providers…</p>
      </Section>
    );
  }

  return (
    <Section title="Saved providers">
      <p className="text-[11px] text-ink-100/50">
        Catalogue of LLM endpoints + credentials. Each entry shows up in
        the role-assignment dropdowns above. Two roles pointing at the
        same provider share one underlying connection.
      </p>

      <div className="space-y-2">
        {providers.map((p) => {
          const draft = drafts[p.id];
          const isOpen = !!expanded[p.id];
          return (
            <div
              key={p.id}
              className="rounded-md border border-white/10 bg-black/30"
            >
              <div className="flex items-center justify-between px-3 py-2">
                <div className="flex flex-col">
                  <div className="text-sm font-medium text-ink-100">
                    {p.name}
                    <span className="ml-2 text-[10px] text-ink-100/40">
                      id={p.id} · {p.kind}
                    </span>
                  </div>
                  <div className="text-[10px] text-ink-100/40">
                    {p.base_url || "(no URL)"} ·{" "}
                    {p.has_api_key ? "API key set" : "no API key"}
                  </div>
                </div>
                <div className="flex items-center gap-1">
                  <button
                    type="button"
                    disabled={busyProvider === p.id}
                    onClick={() => void testProvider(p.id)}
                    className="rounded-md border border-white/10 bg-black/40 px-2 py-1 text-[11px] text-ink-100 hover:bg-white/5 disabled:opacity-50"
                  >
                    Test
                  </button>
                  <button
                    type="button"
                    onClick={() =>
                      setExpanded((cur) => ({ ...cur, [p.id]: !cur[p.id] }))
                    }
                    className="rounded-md border border-white/10 bg-black/40 px-2 py-1 text-[11px] text-ink-100 hover:bg-white/5"
                  >
                    {isOpen ? "Close" : "Edit"}
                  </button>
                  <button
                    type="button"
                    disabled={busyProvider === p.id}
                    onClick={() => void deleteProvider(p.id)}
                    className="rounded-md border border-rose-500/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200 hover:bg-rose-500/20 disabled:opacity-50"
                  >
                    Delete
                  </button>
                </div>
              </div>
              {isOpen && draft ? (
                <div className="space-y-2 border-t border-white/5 px-3 pb-3 pt-2">
                  <label className="block">
                    <span className="block text-[11px] text-ink-100/60">
                      Name
                    </span>
                    <input
                      type="text"
                      value={draft.name}
                      onChange={(e) => editDraft(p.id, { name: e.target.value })}
                      className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
                    />
                  </label>
                  <label className="block">
                    <span className="block text-[11px] text-ink-100/60">
                      Endpoint URL
                    </span>
                    <input
                      type="text"
                      value={draft.base_url}
                      onChange={(e) =>
                        editDraft(p.id, { base_url: e.target.value })
                      }
                      className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
                    />
                  </label>
                  <label className="block">
                    <span className="block text-[11px] text-ink-100/60">
                      API key
                    </span>
                    <input
                      type="password"
                      autoComplete="off"
                      value={
                        draft.api_key_touched
                          ? draft.api_key
                          : p.has_api_key
                          ? "••••••••"
                          : ""
                      }
                      onFocus={() => {
                        if (!draft.api_key_touched) {
                          editDraft(p.id, {
                            api_key: "",
                            api_key_touched: true,
                          });
                        }
                      }}
                      onChange={(e) =>
                        editDraft(p.id, {
                          api_key: e.target.value,
                          api_key_touched: true,
                        })
                      }
                      placeholder={p.has_api_key ? "" : "Paste your API key"}
                      className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
                    />
                  </label>
                  <label className="block">
                    <span className="block text-[11px] text-ink-100/60">
                      API key env-var fallback (optional)
                    </span>
                    <input
                      type="text"
                      value={draft.api_key_env}
                      onChange={(e) =>
                        editDraft(p.id, { api_key_env: e.target.value })
                      }
                      placeholder="e.g. OPENAI_API_KEY"
                      className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
                    />
                  </label>
                  <label className="block">
                    <span className="block text-[11px] text-ink-100/60">
                      Extra headers (JSON)
                    </span>
                    <textarea
                      value={draft.extra_headers_json}
                      onChange={(e) =>
                        editDraft(p.id, {
                          extra_headers_json: e.target.value,
                        })
                      }
                      rows={3}
                      placeholder='{"HTTP-Referer": "https://my-app.example"}'
                      className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1 font-mono text-xs text-ink-100"
                    />
                  </label>
                  <div className="flex justify-end">
                    <button
                      type="button"
                      disabled={busyProvider === p.id}
                      onClick={() => void saveProvider(p.id)}
                      className="rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-500 disabled:opacity-50"
                    >
                      {busyProvider === p.id ? "Saving…" : "Save"}
                    </button>
                  </div>
                </div>
              ) : null}
            </div>
          );
        })}
      </div>

      {showAdd ? (
        <div className="rounded-md border border-white/10 bg-black/30 p-3">
          <div className="mb-1 text-sm font-medium text-ink-100">
            Add provider
          </div>
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
            {availableTemplates.map((t) => (
              <button
                key={t.id}
                type="button"
                onClick={() => void addFromTemplate(t.id)}
                className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-left text-xs text-ink-100/80 hover:bg-white/5"
              >
                <div className="font-medium">{t.label}</div>
                <div className="text-[10px] text-ink-100/40">
                  {t.free_tier}
                </div>
              </button>
            ))}
            <button
              type="button"
              onClick={() => void addFromTemplate(null)}
              className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-left text-xs text-ink-100/80 hover:bg-white/5"
            >
              <div className="font-medium">Custom</div>
              <div className="text-[10px] text-ink-100/40">
                Bring your own base_url
              </div>
            </button>
          </div>
          <div className="mt-2 flex justify-end">
            <button
              type="button"
              onClick={() => setShowAdd(false)}
              className="rounded-md border border-white/10 bg-black/40 px-2 py-1 text-[11px] text-ink-100 hover:bg-white/5"
            >
              Cancel
            </button>
          </div>
        </div>
      ) : (
        <button
          type="button"
          onClick={() => setShowAdd(true)}
          className="rounded-md border border-white/10 bg-black/40 px-3 py-1.5 text-xs text-ink-100 hover:bg-white/5"
        >
          + Add provider
        </button>
      )}
    </Section>
  );
}
