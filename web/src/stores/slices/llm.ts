import type { LlmProvider, LlmRoute } from "@/types";
import type { SliceCreator } from "../types";

export interface LlmSlice {
  // PR 2: LLM provider catalogue + role assignments. Loaded when the
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
}

export const createLlmSlice: SliceCreator<LlmSlice> = (set) => ({
  llmProviders: null,
  llmRoutes: null,
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
