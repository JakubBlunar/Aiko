import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api";
import { useAssistantStore } from "@/store";
import type { RequiredModel, RequiredModels } from "@/types";

/** Model we suggest when nothing is installed yet. Matches the
 *  ``main_chat`` route in :file:`config/default.json` — ~6.6 GB of
 *  weights, which leaves room for a 64 k KV cache on a 12 GB card. */
const RECOMMENDED = "qwen3.5:9b";

/** Roles the chat-model choice is written to. Aiko runs background
 *  workers and workflows on their own routes; on a single-GPU box
 *  pointing all three at one model is what keeps it resident in VRAM
 *  instead of thrashing between two sets of weights. */
const CHAT_ROLES = ["main_chat", "worker_default", "workflow"] as const;

/**
 * First-run step 2: make sure a chat model actually exists.
 *
 * The default config names a model rather than shipping one, so a
 * clean install reaches this screen with nothing downloaded. Boot is
 * deliberately non-fatal in that case (see ``prewarm_runtime``) — this
 * step is the recovery path: it lists what Ollama actually has, lets
 * the user pick one, and streams a pull for anything missing.
 *
 * Skipped entirely when the chat route points at a hosted provider,
 * which has nothing to download.
 */
export function ModelStep({
  onDone,
  onSkip,
}: {
  onDone: () => void;
  onSkip: () => void;
}) {
  const pulls = useAssistantStore((s) => s.modelPulls);
  const pushToast = useAssistantStore((s) => s.pushToast);
  const setMissingChatModel = useAssistantStore((s) => s.setMissingChatModel);
  const [state, setState] = useState<RequiredModels | null>(null);
  const [loading, setLoading] = useState(true);
  const [choice, setChoice] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    try {
      const next = await api.getRequiredModels();
      setState(next);
      setChoice((current) => {
        if (current) return current;
        const chat = next.required.find((r) => r.role === "main_chat");
        return chat?.model || RECOMMENDED;
      });
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't reach the backend.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // A finished pull changes what's installed, so re-read the inventory
  // rather than guessing which row just became satisfiable.
  const doneCount = useMemo(
    () =>
      Object.values(pulls ?? {}).filter((p) => p.status === "done").length,
    [pulls],
  );
  useEffect(() => {
    if (doneCount > 0) void refresh();
  }, [doneCount, refresh]);

  const installed = state?.installed ?? [];
  const embedding = state?.required.find((r) => r.role === "embedding");
  const chatInstalled = installed.includes(choice);
  const choicePull = pulls?.[choice];
  const pulling = choicePull != null && choicePull.status !== "done" && choicePull.status !== "error";

  const startPull = async (model: string) => {
    setError(null);
    try {
      await api.pullModel({ model, provider_id: state?.provider_id });
    } catch (err) {
      setError(err instanceof Error ? err.message : `Couldn't start pulling ${model}.`);
    }
  };

  const finish = async () => {
    setSaving(true);
    setError(null);
    try {
      const chat = state?.required.find((r) => r.role === "main_chat");
      if (chat && chat.model !== choice) {
        for (const role of CHAT_ROLES) {
          await api.updateLlmRoute(role, { model: choice });
        }
        pushToast("info", `Aiko will think with ${choice}.`);
      }
      // The banner is a boot-time snapshot keyed to the *old* model, so
      // it would otherwise keep naming a model we just moved off of.
      if (chatInstalled) setMissingChatModel("");
      onDone();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save the model choice.");
      setSaving(false);
    }
  };

  return (
    <div className="w-[min(520px,calc(100vw-2rem))] rounded-2xl border border-white/10 bg-neutral-900 p-6 shadow-2xl">
      <h2 id="first-run-title" className="text-lg font-semibold text-neutral-100">
        Pick the model Aiko thinks with
      </h2>
      <p className="mt-2 text-sm text-neutral-400">
        Everything runs on your machine through Ollama. You can swap
        models later in Settings → Providers.
      </p>

      {loading && state === null ? (
        <p className="mt-6 text-sm text-neutral-500">Checking what's installed…</p>
      ) : state && !state.reachable ? (
        <div className="mt-4 rounded-md border border-amber-300/40 bg-amber-500/10 p-3 text-sm text-amber-100/90">
          <div className="font-medium">Ollama isn't answering</div>
          <p className="mt-1 text-amber-100/70">
            Nothing responded at{" "}
            <code className="text-amber-100">{state.base_url}</code>. Start
            Ollama and try again, or continue and set this up later.
          </p>
          <button
            type="button"
            onClick={() => void refresh()}
            className="mt-2 rounded-md border border-amber-300/60 bg-amber-500/20 px-3 py-1.5 text-amber-100 hover:bg-amber-500/30"
          >
            Check again
          </button>
        </div>
      ) : (
        <>
          <label className="mt-5 block text-xs text-neutral-400">Chat model</label>
          <select
            value={installed.includes(choice) ? choice : "__missing__"}
            onChange={(e) => {
              if (e.target.value !== "__missing__") setChoice(e.target.value);
            }}
            className="mt-1 w-full rounded-md border border-neutral-700 bg-neutral-800 px-3 py-2 text-sm text-neutral-100"
          >
            {!chatInstalled ? (
              <option value="__missing__">{choice} — not downloaded</option>
            ) : null}
            {installed.map((name) => (
              <option key={name} value={name}>
                {name}
              </option>
            ))}
          </select>

          {!chatInstalled ? (
            <div className="mt-3 rounded-md border border-sky-400/30 bg-sky-500/10 p-3 text-sm text-sky-100/90">
              <div className="font-medium">
                {choice === RECOMMENDED
                  ? `${RECOMMENDED} is the recommended starting point`
                  : `${choice} isn't downloaded yet`}
              </div>
              <p className="mt-1 text-sky-100/70">
                About 6.6 GB. It fits a 64 k context on a 12 GB card, which
                is what Aiko's memory and workflow passes are tuned for.
              </p>
              {choicePull ? (
                <PullProgress
                  label={choice}
                  percent={choicePull.percent ?? null}
                  status={choicePull.status}
                  error={choicePull.error}
                />
              ) : (
                <button
                  type="button"
                  onClick={() => void startPull(choice)}
                  className="mt-2 rounded-md border border-sky-400/50 bg-sky-500/20 px-3 py-1.5 text-sky-100 hover:bg-sky-500/30"
                >
                  Download {choice}
                </button>
              )}
            </div>
          ) : null}

          {embedding && !embedding.installed ? (
            <EmbeddingRow
              row={embedding}
              pull={pulls?.[embedding.model] ?? null}
              onPull={() => void startPull(embedding.model)}
            />
          ) : null}
        </>
      )}

      {error ? (
        <p className="mt-3 text-sm text-rose-400" role="alert">
          {error}
        </p>
      ) : null}

      <div className="mt-6 flex items-center justify-between">
        <button
          type="button"
          onClick={onSkip}
          className="text-xs text-neutral-500 underline-offset-2 hover:text-neutral-300 hover:underline"
        >
          Skip for now
        </button>
        <button
          type="button"
          disabled={saving}
          onClick={() => void finish()}
          className="rounded-lg bg-sky-600 px-4 py-2 text-sm font-medium text-white shadow hover:bg-sky-500 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {pulling ? "Continue in background" : "Continue"}
        </button>
      </div>
    </div>
  );
}

/** The embedding model is what makes memory searchable, so it gets its
 *  own row instead of being folded into the chat picker — it's small
 *  and there's no reason to choose anything else. */
function EmbeddingRow({
  row,
  pull,
  onPull,
}: {
  row: RequiredModel;
  pull: { percent?: number | null; status: string; error?: string } | null;
  onPull: () => void;
}) {
  return (
    <div className="mt-3 rounded-md border border-neutral-700 bg-neutral-800/60 p-3 text-sm text-neutral-300">
      <div className="font-medium text-neutral-200">
        Memory search needs {row.model}
      </div>
      <p className="mt-1 text-neutral-400">
        A small (~600 MB) embedding model. Without it Aiko can still
        chat, but she can't search her own memories or your documents.
      </p>
      {pull ? (
        <PullProgress
          label={row.model}
          percent={pull.percent ?? null}
          status={pull.status}
          error={pull.error}
        />
      ) : (
        <button
          type="button"
          onClick={onPull}
          className="mt-2 rounded-md border border-neutral-600 bg-neutral-800 px-3 py-1.5 text-xs text-neutral-100 hover:bg-neutral-700"
        >
          Download {row.model}
        </button>
      )}
    </div>
  );
}

/** Shared progress readout for an in-flight pull. Ollama reports byte
 *  counts per layer, so ``percent`` restarts a few times during a
 *  multi-blob download; the status line explains what it's doing. */
function PullProgress({
  label,
  percent,
  status,
  error,
}: {
  label: string;
  percent: number | null;
  status: string;
  error?: string;
}) {
  if (status === "error") {
    return (
      <p className="mt-2 text-sm text-rose-400" role="alert">
        Couldn't download {label}: {error || "unknown error"}
      </p>
    );
  }
  if (status === "done") {
    return <p className="mt-2 text-sm text-emerald-400">{label} is ready.</p>;
  }
  return (
    <div className="mt-2">
      <div className="flex items-center justify-between text-xs text-neutral-400">
        <span>{status}</span>
        <span>{percent === null ? "" : `${Math.round(percent)}%`}</span>
      </div>
      <div className="mt-1 h-2 overflow-hidden rounded-full bg-neutral-800">
        <div
          className="h-full bg-sky-500 transition-[width] duration-300"
          style={{ width: `${percent === null ? 5 : Math.round(percent)}%` }}
        />
      </div>
    </div>
  );
}
