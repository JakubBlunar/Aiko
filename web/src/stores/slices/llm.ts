import type { LlmProvider, LlmRoute, ModelPullProgress } from "@/types";
import type { SliceCreator } from "../types";

export interface LlmSlice {
  // LLM provider catalogue + role assignments. Loaded when the
  // Settings drawer opens (or on a ``llm_settings_changed`` broadcast).
  // Both null until the first ``GET /api/llm/{providers,routes}`` resolves.
  llmProviders: LlmProvider[] | null;
  llmRoutes: Record<string, LlmRoute> | null;
  setLlmProviders: (providers: LlmProvider[] | null) => void;
  setLlmRoutes: (routes: Record<string, LlmRoute> | null) => void;
  /** Insert / replace a single provider entry (match by ``id``). */
  upsertLlmProvider: (provider: LlmProvider) => void;
  /** Remove a provider by id (used after DELETE). */
  removeLlmProvider: (providerId: string) => void;
  /** Set or replace a route by role. */
  setLlmRoute: (role: string, route: LlmRoute) => void;
  /** Latest ``model_pull_progress`` frame, keyed by model name so two
   * concurrent pulls don't overwrite each other's progress bar. Null
   * until the first pull starts. */
  modelPulls: Record<string, ModelPullProgress> | null;
  setModelPullProgress: (progress: ModelPullProgress) => void;
  /** Forget a finished pull so its bar can be dismissed. */
  clearModelPull: (model: string) => void;
  /** Model named by the ``main_chat`` route that isn't installed
   * locally, from the WS hello envelope. Empty when all is well. */
  missingChatModel: string;
  setMissingChatModel: (model: string) => void;
}

export const createLlmSlice: SliceCreator<LlmSlice> = (set) => ({
  llmProviders: null,
  llmRoutes: null,
  modelPulls: null,
  missingChatModel: "",
  setMissingChatModel: (model) => set({ missingChatModel: model }),
  setModelPullProgress: (progress) =>
    set((state) => ({
      modelPulls: { ...(state.modelPulls ?? {}), [progress.model]: progress },
      // A finished pull clears the "not installed" banner for that model.
      missingChatModel:
        progress.status === "done" && state.missingChatModel === progress.model
          ? ""
          : state.missingChatModel,
    })),
  clearModelPull: (model) =>
    set((state) => {
      if (!state.modelPulls) return {};
      const { [model]: _dropped, ...rest } = state.modelPulls;
      return { modelPulls: rest };
    }),
  setLlmProviders: (providers) => set({ llmProviders: providers }),
  setLlmRoutes: (routes) => set({ llmRoutes: routes }),
  upsertLlmProvider: (provider) =>
    set((state) => {
      const list = state.llmProviders ?? [];
      const idx = list.findIndex((p) => p.id === provider.id);
      const next =
        idx >= 0
          ? [...list.slice(0, idx), provider, ...list.slice(idx + 1)]
          : [...list, provider];
      return { llmProviders: next };
    }),
  removeLlmProvider: (providerId) =>
    set((state) => ({
      llmProviders: (state.llmProviders ?? []).filter(
        (p) => p.id !== providerId,
      ),
    })),
  setLlmRoute: (role, route) =>
    set((state) => ({
      llmRoutes: { ...(state.llmRoutes ?? {}), [role]: route },
    })),
});
