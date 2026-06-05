import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "../../api";
import { useAssistantStore } from "../../store";
import type {
  AssistantSettings,
  ChatLlmSnapshot,
  LlmProviderPreset,
  LlmTestConnectionResult,
} from "../../types";
import { Row, Section } from "./SettingsSection";

/**
 * LLM-provider routing UI.
 *
 * Replaces the legacy "Chat model" section in the Chat tab. Renders:
 *   1. Curated preset cards (Ollama / Gemini / OpenAI / Groq /
 *      OpenRouter, plus "Custom" for free-text everything).
 *   2. Endpoint URL (prefilled by preset, user-overrideable).
 *   3. API key password input (write-only — the saved key is never
 *      echoed back).
 *   4. Model dropdown auto-fetched from the selected provider's
 *      ``/v1/models`` endpoint with a free-text fallback when the
 *      provider doesn't expose model listing.
 *   5. **Test connection** button: dry-run one-token chat ping against
 *      the in-form candidate values via ``POST /api/llm/test-connection``;
 *      never touches the saved config. Status pill shows green check +
 *      latency on pass, red banner with the provider error on fail.
 *   6. Workers-use-local toggle (true by default for non-Ollama
 *      providers so free-tier quotas survive).
 *   7. Advanced collapsible: max_tokens, temperature, extra_headers.
 *
 * Save is gated optimistically — the user can save without testing,
 * but a passing test pre-arms a "tested OK" badge that clears the
 * moment any field is edited. See ``docs/llm-providers.md`` for the
 * full contract.
 */

const CUSTOM_PRESET_ID = "custom";

interface DraftState {
  preset_id: string;
  provider: "ollama" | "openai_compatible";
  base_url: string;
  api_key: string;
  api_key_touched: boolean; // true once the user starts typing a new key
  model: string;
  workers_use_local: boolean;
  max_tokens: number;
  extra_headers_json: string;
  /** Explicit context-window override. ``null`` means "auto" — let
   *  the controller resolve via the active client's
   *  ``get_context_length(model)`` lookup (Ollama's ``/api/show`` for
   *  local models, the static OpenAI-compat lookup table otherwise),
   *  with an 8192 last-resort fallback. */
  context_window: number | null;
}

function snapshotToDraft(
  snap: ChatLlmSnapshot,
  presets: LlmProviderPreset[],
): DraftState {
  // Find the preset whose id matches the persisted hint, otherwise
  // fall back to one matching (base_url, provider). When nothing
  // matches we render the "Custom" card.
  const matched =
    presets.find((p) => p.id === snap.provider_preset) ||
    presets.find(
      (p) =>
        p.provider === snap.provider &&
        normalizeUrl(p.base_url) === normalizeUrl(snap.base_url),
    );
  return {
    preset_id: matched?.id ?? CUSTOM_PRESET_ID,
    provider: snap.provider,
    base_url: snap.base_url || matched?.base_url || "",
    api_key: "",
    api_key_touched: false,
    model: snap.model,
    workers_use_local: snap.workers_use_local,
    max_tokens: snap.max_tokens,
    extra_headers_json:
      Object.keys(snap.extra_headers || {}).length > 0
        ? JSON.stringify(snap.extra_headers, null, 2)
        : "",
    context_window: snap.context_window,
  };
}

function normalizeUrl(url: string): string {
  return (url || "").trim().replace(/\/$/, "").toLowerCase();
}

interface ChatProviderSectionProps {
  settings: AssistantSettings;
  /** ``apply`` is the shared PATCH /api/settings helper from
   *  SettingsDrawer. We only use it for the legacy ``chat.model`` path
   *  when the user is on a pure-Ollama setup, so the dropdown change
   *  hits the existing fast path. */
  apply: (patch: Record<string, unknown>) => Promise<void>;
  /** Called after a successful save so the parent can refresh its
   *  cached settings snapshot. Mirrors how IdentitySection notifies
   *  the drawer. */
  onSettingsChanged: () => void;
}

export function ChatProviderSection({
  settings,
  apply,
  onSettingsChanged,
}: ChatProviderSectionProps) {
  const pushToast = useAssistantStore((s) => s.pushToast);
  const [presets, setPresets] = useState<LlmProviderPreset[]>([]);
  const [draft, setDraft] = useState<DraftState | null>(null);
  const [providerModels, setProviderModels] = useState<string[]>([]);
  const [modelsLoading, setModelsLoading] = useState(false);
  const [advancedOpen, setAdvancedOpen] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<
    LlmTestConnectionResult | null
  >(null);
  const [error, setError] = useState<string | null>(null);

  const chatLlm = settings.chat_llm;

  // Load presets once. The catalogue is process-static on the backend
  // so we never re-fetch it.
  useEffect(() => {
    let cancelled = false;
    api
      .getLlmPresets()
      .then((r) => {
        if (!cancelled) setPresets(r.presets);
      })
      .catch(() => {
        if (!cancelled) setPresets([]);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Initialise the draft once both the snapshot AND the preset
  // catalogue are loaded — the draft needs both to figure out which
  // preset card is active.
  useEffect(() => {
    if (draft || !chatLlm || presets.length === 0) return;
    setDraft(snapshotToDraft(chatLlm, presets));
  }, [chatLlm, presets, draft]);

  // Refetch provider models whenever the user picks a new provider.
  // This is purely a UI preview — switching providers doesn't persist.
  useEffect(() => {
    if (!draft) return;
    let cancelled = false;
    const provider = draft.provider;
    setModelsLoading(true);
    api
      .listModels(false, provider)
      .then((models) => {
        if (!cancelled) setProviderModels(models);
      })
      .catch(() => {
        if (!cancelled) setProviderModels([]);
      })
      .finally(() => {
        if (!cancelled) setModelsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [draft?.provider, draft?.base_url, draft?.api_key]);

  // Clear the "tested OK" badge any time the user edits a field.
  // Keeps the badge honest: it only ever reflects the *current* form
  // values, never a stale prior success.
  const editDraft = useCallback(
    (patch: Partial<DraftState>) => {
      setDraft((cur) => (cur ? { ...cur, ...patch } : cur));
      setTestResult(null);
    },
    [],
  );

  const pickPreset = useCallback(
    (preset: LlmProviderPreset | null) => {
      if (!preset) {
        editDraft({
          preset_id: CUSTOM_PRESET_ID,
          provider: "openai_compatible",
        });
        return;
      }
      const recommended = preset.recommended_models[0] || "";
      editDraft({
        preset_id: preset.id,
        provider: preset.provider,
        base_url: preset.base_url,
        model: recommended,
        workers_use_local:
          preset.provider === "ollama"
            ? false
            : preset.default_workers_use_local,
        // Pre-fill the explicit context-window cap from the preset
        // template (131 072 for cloud providers, ``null`` for Ollama
        // so ``/api/show`` auto-detect wins per model).
        context_window: preset.default_context_window,
      });
    },
    [editDraft],
  );

  const cardClass = useCallback(
    (id: string) =>
      [
        "flex flex-col gap-1 rounded-md border px-3 py-2 text-left text-xs transition",
        draft?.preset_id === id
          ? "border-sky-500/60 bg-sky-500/10 text-ink-100"
          : "border-white/10 bg-black/30 text-ink-100/70 hover:bg-white/5",
      ].join(" "),
    [draft?.preset_id],
  );

  const onTestConnection = useCallback(async () => {
    if (!draft || testing) return;
    setTesting(true);
    setError(null);
    try {
      // ``api_key`` is sent only if the user typed a new one. If they
      // left the masked placeholder untouched, send an empty string —
      // the backend will probe with the saved key (it's still in
      // process memory) because the throwaway client is built from the
      // controller's current ``OllamaSettings``. Practically that
      // means: a saved-key + unchanged config -> test against saved
      // key. New key in the field -> test against new key.
      const payload = {
        provider: draft.provider,
        base_url: draft.base_url,
        api_key: draft.api_key_touched ? draft.api_key : "",
        model: draft.model,
        extra_headers: parseHeaders(draft.extra_headers_json),
      };
      const result = await api.testLlmConnection(payload);
      setTestResult(result);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Test failed");
    } finally {
      setTesting(false);
    }
  }, [draft, testing]);

  const onSave = useCallback(async () => {
    if (!draft || saving) return;
    setSaving(true);
    setError(null);
    try {
      // 1. Write credentials FIRST if the user typed a new key or
      //    base_url has changed. The credentials endpoint also writes
      //    ``base_url`` + ``extra_headers`` so we batch them here.
      let extraHeaders: Record<string, string> | undefined;
      try {
        extraHeaders = parseHeaders(draft.extra_headers_json);
      } catch (parseExc) {
        setError(
          parseExc instanceof Error
            ? `extra_headers JSON: ${parseExc.message}`
            : "Invalid extra_headers JSON",
        );
        setSaving(false);
        return;
      }
      const credentialsPatch: Parameters<typeof api.setLlmCredentials>[0] = {};
      if (draft.api_key_touched) credentialsPatch.api_key = draft.api_key;
      if (draft.base_url !== (chatLlm?.base_url || ""))
        credentialsPatch.base_url = draft.base_url;
      if (extraHeaders !== undefined)
        credentialsPatch.extra_headers = extraHeaders;
      if (Object.keys(credentialsPatch).length > 0) {
        await api.setLlmCredentials(credentialsPatch);
      }
      // 2. Then write the non-credential knobs through the generic
      //    PATCH /api/settings path. This triggers reconfigure on the
      //    backend, which broadcasts ``llm_settings_changed`` and
      //    ``model_changed``.
      await apply({
        chat_llm: {
          provider: draft.provider,
          provider_preset: draft.preset_id,
          model: draft.model,
          workers_use_local: draft.workers_use_local,
          max_tokens: draft.max_tokens,
          // ``0`` / empty / null -> server treats as "no explicit
          //  override" and falls back to ``client.get_context_length``
          //  per the precedence in ``_resolve_context_window``.
          context_window: draft.context_window || null,
        },
      });
      // Reset the touched flag so the masked placeholder reappears
      // for the next edit cycle.
      setDraft((cur) =>
        cur ? { ...cur, api_key: "", api_key_touched: false } : cur,
      );
      onSettingsChanged();
      if (testResult?.success) {
        pushToast("info", "Provider saved.");
      } else {
        pushToast(
          "info",
          "Saved (untested). Run Test connection to verify.",
        );
      }
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "Save failed");
    } finally {
      setSaving(false);
    }
  }, [draft, saving, chatLlm, apply, onSettingsChanged, testResult, pushToast]);

  const activePreset = useMemo(
    () => presets.find((p) => p.id === draft?.preset_id) || null,
    [presets, draft?.preset_id],
  );

  // ``modelOptions`` is the autocomplete-suggestion set surfaced
  // through the ``<datalist>`` below. The union of:
  //   1. live ``/v1/models`` response for the chosen provider
  //      (account-specific — only what the API key can actually use),
  //   2. the active preset's curated ``recommended_models`` (so
  //      ``gpt-5-mini`` stays visible even when the live API doesn't
  //      include it for an account that hasn't been verified yet),
  //   3. the current draft.model (so a user-typed custom id shows
  //      up as a one-shot suggestion next to the live ones).
  // The input itself is free-text — the datalist only ever offers
  // suggestions; the user can type anything and Save accepts it.
  //
  // MUST stay above the ``if (!draft)`` early return below — React's
  // Rules of Hooks require an identical hook count on every render,
  // so a useMemo placed after a conditional return crashes the
  // settings drawer the moment ``draft`` flips from null to populated.
  const modelOptions = useMemo(() => {
    const recommended = activePreset?.recommended_models ?? [];
    const seen = new Set<string>();
    const out: string[] = [];
    const draftModel = draft?.model ?? "";
    for (const raw of [...providerModels, ...recommended, draftModel]) {
      const id = (raw ?? "").trim();
      if (id && !seen.has(id)) {
        seen.add(id);
        out.push(id);
      }
    }
    return out;
  }, [providerModels, activePreset?.recommended_models, draft?.model]);

  // ── Render ──────────────────────────────────────────────────────

  if (!chatLlm || !draft) {
    return (
      <Section title="Chat provider">
        <p className="text-xs text-ink-100/50">Loading provider config…</p>
      </Section>
    );
  }

  return (
    <Section title="Chat provider">
      <p className="text-[11px] text-ink-100/50">
        Aiko's main chat path. Background workers (reflection, dream,
        memory extraction, ...) can stay on local Ollama to protect
        your remote provider's free-tier quota — see the toggle below.
      </p>

      {/* Preset picker */}
      <div className="grid grid-cols-2 gap-2 sm:grid-cols-3">
        {presets.map((preset) => (
          <button
            type="button"
            key={preset.id}
            onClick={() => pickPreset(preset)}
            className={cardClass(preset.id)}
          >
            <div className="text-sm font-medium">{preset.label}</div>
            <div className="text-[10px] text-ink-100/50">
              {preset.free_tier}
            </div>
          </button>
        ))}
        <button
          type="button"
          onClick={() => pickPreset(null)}
          className={cardClass(CUSTOM_PRESET_ID)}
        >
          <div className="text-sm font-medium">Custom</div>
          <div className="text-[10px] text-ink-100/50">
            Bring your own base_url
          </div>
        </button>
      </div>

      {activePreset?.docs_url ? (
        <p className="text-[10px] text-ink-100/40">
          Get an API key:{" "}
          <a
            href={activePreset.docs_url}
            target="_blank"
            rel="noreferrer"
            className="underline hover:text-ink-100/70"
          >
            {activePreset.docs_url}
          </a>
        </p>
      ) : null}

      {/* Base URL */}
      <label className="block">
        <span className="block text-xs text-ink-100/60">Endpoint URL</span>
        <input
          type="text"
          value={draft.base_url}
          onChange={(e) => editDraft({ base_url: e.target.value })}
          placeholder="https://..."
          className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-1.5 text-sm text-ink-100"
        />
      </label>

      {/* API key */}
      <label className="block">
        <span className="block text-xs text-ink-100/60">
          API key{" "}
          {activePreset?.env_hint ? (
            <span className="text-ink-100/40">
              (or set ${activePreset.env_hint})
            </span>
          ) : null}
        </span>
        <input
          type="password"
          autoComplete="off"
          value={
            draft.api_key_touched
              ? draft.api_key
              : chatLlm.has_api_key
              ? "••••••••"
              : ""
          }
          onFocus={() => {
            if (!draft.api_key_touched) {
              editDraft({ api_key: "", api_key_touched: true });
            }
          }}
          onChange={(e) =>
            editDraft({ api_key: e.target.value, api_key_touched: true })
          }
          placeholder={
            activePreset?.api_key_required
              ? "Paste your API key"
              : "(not required for local providers)"
          }
          className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-1.5 text-sm text-ink-100"
        />
      </label>

      {/* Model */}
      <div>
        <div className="flex items-center justify-between">
          <label
            htmlFor="chat-provider-model"
            className="block text-xs text-ink-100/60"
          >
            Model
          </label>
          {modelsLoading ? (
            <span className="text-[10px] text-ink-100/40">Loading…</span>
          ) : null}
        </div>
        <input
          id="chat-provider-model"
          type="text"
          list="chat-provider-model-options"
          value={draft.model}
          onChange={(e) => editDraft({ model: e.target.value })}
          placeholder="Type or pick a model id"
          autoComplete="off"
          spellCheck={false}
          className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-1.5 text-sm text-ink-100"
        />
        <datalist id="chat-provider-model-options">
          {modelOptions.map((m) => (
            <option key={m} value={m} />
          ))}
        </datalist>
        {modelOptions.length > 0 ? (
          <p className="mt-1 text-[10px] text-ink-100/40">
            Click the field to see suggestions, or type any model id (e.g.,
            a brand-new release, an experimental preview, an
            OpenRouter-prefixed id, a fine-tuned model). Use{" "}
            <span className="font-medium">Test connection</span> to verify
            the provider accepts the typed id.
          </p>
        ) : null}
      </div>

      {/* Test connection */}
      <div className="space-y-1">
        <button
          type="button"
          disabled={testing || !draft.model}
          onClick={() => void onTestConnection()}
          className="rounded-md border border-white/10 bg-black/40 px-3 py-1.5 text-xs text-ink-100 hover:bg-white/5 disabled:opacity-50"
        >
          {testing ? "Testing…" : "Test connection"}
        </button>
        {testResult?.success ? (
          <p className="text-xs text-emerald-400">
            ✓ Connected in {testResult.latency_ms} ms · {""}
            {testResult.completion_tokens} token
            {testResult.completion_tokens === 1 ? "" : "s"} used
          </p>
        ) : null}
        {testResult && !testResult.success ? (
          <div
            role="alert"
            className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200"
          >
            <span className="font-semibold uppercase">
              {testResult.error_code ?? "error"}:
            </span>{" "}
            {testResult.error_message || "Provider rejected the request."}
          </div>
        ) : null}
      </div>

      {/* Workers fallback toggle */}
      {draft.provider !== "ollama" ? (
        <label className="flex items-start gap-2 text-xs text-ink-100/70">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={draft.workers_use_local}
            onChange={(e) => editDraft({ workers_use_local: e.target.checked })}
          />
          <span>
            Background workers use local Ollama
            <span className="block text-[10px] text-ink-100/40">
              Recommended. Reflection / dream / memory extraction stay
              on your local model so the remote provider's free-tier
              quota survives a long conversation.
            </span>
          </span>
        </label>
      ) : null}

      {/* Advanced */}
      <details
        open={advancedOpen}
        onToggle={(e) => setAdvancedOpen((e.target as HTMLDetailsElement).open)}
      >
        <summary className="cursor-pointer text-xs text-ink-100/60">
          Advanced
        </summary>
        <div className="mt-2 space-y-2 pl-2">
          <label className="block">
            <span className="block text-[11px] text-ink-100/60">
              Max tokens (per reply)
            </span>
            <input
              type="number"
              min={64}
              max={8192}
              value={draft.max_tokens}
              onChange={(e) =>
                editDraft({
                  max_tokens: Math.max(
                    64,
                    Math.min(8192, Number.parseInt(e.target.value, 10) || 512),
                  ),
                })
              }
              className="mt-1 w-32 rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
            />
          </label>
          <label className="block">
            <span className="block text-[11px] text-ink-100/60">
              Context window (tokens)
              <span className="block text-[10px] text-ink-100/40">
                Caps prompt assembly. Lower = cheaper turns + earlier
                compaction. Leave at 0 to auto-detect from the model
                (Ollama via /api/show, OpenAI / Gemini / etc. via a
                conservative per-model lookup; default 8192 if neither
                matches).
              </span>
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
                editDraft({ context_window: cleaned });
              }}
              placeholder="0 (auto)"
              className="mt-1 w-40 rounded-md border border-white/10 bg-black/40 px-2 py-1 text-sm text-ink-100"
            />
          </label>
          <label className="block">
            <span className="block text-[11px] text-ink-100/60">
              Extra request headers (JSON object, optional)
            </span>
            <textarea
              value={draft.extra_headers_json}
              onChange={(e) =>
                editDraft({ extra_headers_json: e.target.value })
              }
              placeholder='{"HTTP-Referer": "https://my-app.example"}'
              rows={3}
              className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-2 py-1 font-mono text-xs text-ink-100"
            />
          </label>
        </div>
      </details>

      {error ? (
        <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      ) : null}

      {/* Save bar */}
      <div className="flex items-center justify-between">
        <div className="text-[10px] text-ink-100/40">
          {testResult?.success ? (
            <span className="text-emerald-400">✓ tested OK</span>
          ) : (
            <span>Save then click Test to verify.</span>
          )}
        </div>
        <button
          type="button"
          disabled={saving}
          onClick={() => void onSave()}
          className="rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-500 disabled:opacity-50"
        >
          {saving ? "Saving…" : "Save provider"}
        </button>
      </div>

      {/* Live snapshot for debugging */}
      <div className="mt-2 space-y-1">
        <Row label="Active model" value={settings.chat.model} />
        <Row
          label="Context window"
          value={settings.chat.context_window.toLocaleString()}
        />
        <Row label="Provider" value={chatLlm.provider} />
      </div>
    </Section>
  );
}

function parseHeaders(json: string): Record<string, string> {
  const trimmed = (json || "").trim();
  if (!trimmed) return {};
  let parsed: unknown;
  try {
    parsed = JSON.parse(trimmed);
  } catch (exc) {
    throw new Error(exc instanceof Error ? exc.message : "invalid JSON");
  }
  if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
    throw new Error("must be a JSON object");
  }
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof v !== "string") {
      throw new Error(`header ${k} must be a string`);
    }
    out[k] = v;
  }
  return out;
}
