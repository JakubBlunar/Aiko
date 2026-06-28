import { create } from "zustand";
import type {
  WorldItem,
  WorldLocation,
  WorldPatch,
  WorldSnapshot,
  WorldState,
} from "@/types";

/**
 * Standalone store for Aiko's room (virtual world). Extracted from the
 * composed ``useAssistantStore`` (phase 4a) so ``world_updated`` patches
 * only re-run the World tab + avatar-panel selectors.
 */
export interface WorldSlice {
  world: WorldSnapshot | null;
  setWorld: (snapshot: WorldSnapshot | null) => void;
  /** Reducer for ``world_updated``: surgically merges the patch. */
  applyWorldPatch: (patch: WorldPatch) => void;
}

export const useWorldStore = create<WorldSlice>()((set) => ({
  world: null,
  setWorld: (snapshot) => set({ world: snapshot }),
  applyWorldPatch: (patch) =>
    set((state) => {
      const current = state.world;
      if (!current) {
        // Patches landing before the initial snapshot are dropped on the
        // floor — the World tab refetches on mount so we'll catch up.
        if ("snapshot" in patch) {
          return {
            world: {
              state: patch.snapshot.state,
              locations: patch.snapshot.locations,
              items: patch.snapshot.items,
              enabled: true,
            },
          };
        }
        return {};
      }
      if ("snapshot" in patch) {
        return {
          world: {
            state: patch.snapshot.state,
            locations: patch.snapshot.locations,
            items: patch.snapshot.items,
            enabled: true,
          },
        };
      }
      if ("state" in patch) {
        return { world: { ...current, state: patch.state as WorldState } };
      }
      if ("location" in patch) {
        const next = (patch as { location: WorldLocation }).location;
        const idx = current.locations.findIndex((l) => l.id === next.id);
        const locations =
          idx >= 0
            ? current.locations.map((l) => (l.id === next.id ? next : l))
            : [...current.locations, next];
        locations.sort((a, b) => a.position - b.position || a.id - b.id);
        return { world: { ...current, locations } };
      }
      if ("item" in patch) {
        const next = (patch as { item: WorldItem }).item;
        const idx = current.items.findIndex((i) => i.id === next.id);
        const items =
          idx >= 0
            ? current.items.map((i) => (i.id === next.id ? next : i))
            : [...current.items, next];
        return { world: { ...current, items } };
      }
      if ("deleted_location_id" in patch) {
        const lid = patch.deleted_location_id;
        return {
          world: {
            ...current,
            locations: current.locations.filter((l) => l.id !== lid),
            // Items that lived in this location now have their
            // location_id cleared. The backend has already done this in
            // SQLite; mirror it here so the UI doesn't flash a stale
            // location reference until the next snapshot arrives.
            items: current.items.map((i) =>
              i.location_id === lid ? { ...i, location_id: null } : i,
            ),
            state:
              current.state.location_id === lid
                ? { ...current.state, location_id: null }
                : current.state,
          },
        };
      }
      if ("deleted_item_id" in patch) {
        const iid = patch.deleted_item_id;
        return {
          world: {
            ...current,
            items: current.items.filter((i) => i.id !== iid),
          },
        };
      }
      return {};
    }),
}));
