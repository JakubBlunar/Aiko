import { useCallback, useEffect, useState } from "react";
import { api } from "@/api";
import { useWorldStore } from "@/stores/useWorldStore";
import type { WorldItem, WorldKind, WorldLocation } from "@/types";

/**
 * Owns all "World" tab state + REST handlers for the SettingsDrawer: the
 * room snapshot store wiring, the give-item / location / item drafts, and
 * the open-on-tab refresh effect. Live ``world_updated`` WS patches land
 * surgically into the store, so most mutations here intentionally don't
 * touch local state. Extracted (phase 4c).
 */
export function useWorldController(open: boolean, activeTab: string) {
  const world = useWorldStore((s) => s.world);
  const setWorld = useWorldStore((s) => s.setWorld);
  const [worldBusy, setWorldBusy] = useState(false);
  const [worldError, setWorldError] = useState<string | null>(null);
  const [worldGiveOpen, setWorldGiveOpen] = useState(false);
  const [worldGiveDraft, setWorldGiveDraft] = useState<{
    name: string;
    kind: WorldKind | string;
    quantity: number;
    description: string;
    location_id: number | null;
    consumable: boolean;
  }>({
    name: "",
    kind: "food",
    quantity: 1,
    description: "",
    location_id: null,
    consumable: true,
  });
  const [worldLocationsOpen, setWorldLocationsOpen] = useState(false);
  const [worldItemsOpen, setWorldItemsOpen] = useState(true);
  const [worldNewLocationOpen, setWorldNewLocationOpen] = useState(false);
  const [worldNewLocationDraft, setWorldNewLocationDraft] = useState<{
    name: string;
    description: string;
  }>({ name: "", description: "" });
  const [worldEditingItemId, setWorldEditingItemId] = useState<number | null>(
    null,
  );
  const [worldItemDraft, setWorldItemDraft] = useState<{
    name: string;
    description: string;
    kind: string;
    location_id: number | null;
    quantity: number;
  }>({
    name: "",
    description: "",
    kind: "other",
    location_id: null,
    quantity: 1,
  });
  const [worldEditingLocationId, setWorldEditingLocationId] = useState<
    number | null
  >(null);
  const [worldLocationDraft, setWorldLocationDraft] = useState<{
    name: string;
    description: string;
  }>({ name: "", description: "" });

  const refreshWorld = useCallback(async () => {
    setWorldBusy(true);
    setWorldError(null);
    try {
      const snapshot = await api.getWorld();
      setWorld(snapshot);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  }, [setWorld]);

  const onPatchWorldState = async (patch: {
    location_id?: number | null;
    posture?: string;
    activity?: string;
    mood_note?: string;
  }) => {
    setWorldError(null);
    try {
      await api.patchWorldState(patch);
      // The WS broadcast will land via applyWorldPatch; no local mutation.
    } catch (err) {
      setWorldError(String(err));
    }
  };

  const onGiveItem = async () => {
    const trimmed = worldGiveDraft.name.trim();
    if (!trimmed) {
      setWorldError("Item name can't be empty.");
      return;
    }
    setWorldBusy(true);
    setWorldError(null);
    try {
      // Default location: first one matching slug "kitchenette" if no
      // explicit choice was made.
      let location_id = worldGiveDraft.location_id;
      if (location_id === null && world?.locations) {
        const kitchen = world.locations.find((l) => l.slug === "kitchenette");
        location_id = kitchen?.id ?? null;
      }
      await api.giveItem({
        name: trimmed,
        kind: worldGiveDraft.kind,
        description: worldGiveDraft.description.trim() || undefined,
        quantity: worldGiveDraft.quantity,
        consumable: worldGiveDraft.consumable,
        location_id,
      });
      setWorldGiveDraft({
        name: "",
        kind: "food",
        quantity: 1,
        description: "",
        location_id: null,
        consumable: true,
      });
      setWorldGiveOpen(false);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  const onAddLocation = async () => {
    const trimmed = worldNewLocationDraft.name.trim();
    if (!trimmed) {
      setWorldError("Location name can't be empty.");
      return;
    }
    setWorldBusy(true);
    setWorldError(null);
    try {
      await api.createWorldLocation({
        name: trimmed,
        description: worldNewLocationDraft.description.trim(),
      });
      setWorldNewLocationDraft({ name: "", description: "" });
      setWorldNewLocationOpen(false);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  const onSaveLocationEdit = async (loc: WorldLocation) => {
    setWorldBusy(true);
    setWorldError(null);
    try {
      const trimmedName = worldLocationDraft.name.trim();
      if (!trimmedName) throw new Error("name can't be empty");
      await api.updateWorldLocation(loc.id, {
        name: trimmedName,
        description: worldLocationDraft.description.trim(),
      });
      setWorldEditingLocationId(null);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  const onDeleteLocation = async (loc: WorldLocation) => {
    setWorldError(null);
    try {
      await api.deleteWorldLocation(loc.id);
    } catch (err) {
      setWorldError(String(err));
    }
  };

  const onSaveItemEdit = async (item: WorldItem) => {
    setWorldBusy(true);
    setWorldError(null);
    try {
      await api.updateWorldItem(item.id, {
        name: worldItemDraft.name.trim() || item.name,
        description: worldItemDraft.description.trim(),
        kind: worldItemDraft.kind,
        location_id: worldItemDraft.location_id,
        quantity: worldItemDraft.quantity,
      });
      setWorldEditingItemId(null);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  const onDeleteItem = async (item: WorldItem) => {
    setWorldError(null);
    try {
      await api.deleteWorldItem(item.id);
    } catch (err) {
      setWorldError(String(err));
    }
  };

  const onConsumeItem = async (item: WorldItem) => {
    setWorldError(null);
    try {
      await api.consumeWorldItem(item.id, 1);
    } catch (err) {
      setWorldError(String(err));
    }
  };

  const onReseedWorld = async () => {
    if (
      !window.confirm(
        "Reset Aiko's room to the default layout? Everything currently in the room will be removed.",
      )
    ) {
      return;
    }
    setWorldBusy(true);
    setWorldError(null);
    try {
      const snapshot = await api.reseedWorld(true);
      setWorld(snapshot);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  // Refresh the world snapshot whenever the World tab opens. After that,
  // ``world_updated`` WS patches keep the store in sync so we don't need
  // to poll.
  useEffect(() => {
    if (!open || activeTab !== "world") return;
    void refreshWorld();
  }, [open, activeTab, refreshWorld]);

  return {
    world,
    worldBusy,
    worldError,
    refreshWorld,
    onPatchWorldState,
    worldGiveOpen,
    setWorldGiveOpen,
    worldGiveDraft,
    setWorldGiveDraft,
    onGiveItem,
    worldLocationsOpen,
    setWorldLocationsOpen,
    worldItemsOpen,
    setWorldItemsOpen,
    worldNewLocationOpen,
    setWorldNewLocationOpen,
    worldNewLocationDraft,
    setWorldNewLocationDraft,
    onAddLocation,
    worldEditingItemId,
    setWorldEditingItemId,
    worldItemDraft,
    setWorldItemDraft,
    onSaveItemEdit,
    onDeleteItem,
    onConsumeItem,
    worldEditingLocationId,
    setWorldEditingLocationId,
    worldLocationDraft,
    setWorldLocationDraft,
    onSaveLocationEdit,
    onDeleteLocation,
    onReseedWorld,
  };
}
