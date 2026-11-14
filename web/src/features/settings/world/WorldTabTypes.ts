import type { CompanionSettings, WeatherSettingsSnapshot, WorldItem, WorldKind, WorldLocation, WorldScene, WorldSnapshot } from "@/types";

export interface GiveDraft {
  name: string;
  kind: WorldKind | string;
  quantity: number;
  description: string;
  location_id: number | null;
  consumable: boolean;
}

export interface ItemDraft {
  name: string;
  description: string;
  kind: string;
  location_id: number | null;
  quantity: number;
}

export interface LocationDraft {
  name: string;
  description: string;
}

export interface WorldTabProps {
  world: WorldSnapshot | null;
  busy: boolean;
  error: string | null;
  onRefresh: () => void;
  onPatchState: (patch: {
    location_id?: number | null;
    posture?: string;
    activity?: string;
    mood_note?: string;
  }) => void;
  giveOpen: boolean;
  setGiveOpen: (open: boolean) => void;
  giveDraft: GiveDraft;
  setGiveDraft: (draft: GiveDraft) => void;
  onGiveItem: () => void;
  locationsOpen: boolean;
  setLocationsOpen: (open: boolean) => void;
  itemsOpen: boolean;
  setItemsOpen: (open: boolean) => void;
  newLocationOpen: boolean;
  setNewLocationOpen: (open: boolean) => void;
  newLocationDraft: LocationDraft;
  setNewLocationDraft: (draft: LocationDraft) => void;
  onAddLocation: () => void;
  editingItemId: number | null;
  setEditingItemId: (id: number | null) => void;
  itemDraft: ItemDraft;
  setItemDraft: (draft: ItemDraft) => void;
  onSaveItemEdit: (item: WorldItem) => void;
  onDeleteItem: (item: WorldItem) => void;
  onConsumeItem: (item: WorldItem) => void;
  editingLocationId: number | null;
  setEditingLocationId: (id: number | null) => void;
  locationDraft: LocationDraft;
  setLocationDraft: (draft: LocationDraft) => void;
  onSaveLocationEdit: (loc: WorldLocation) => void;
  onDeleteLocation: (loc: WorldLocation) => void;
  onReseedWorld: () => void;
  viewingSceneId: number | null;
  setViewingSceneId: (id: number | null) => void;
  newSceneOpen: boolean;
  setNewSceneOpen: (open: boolean) => void;
  newSceneDraft: { name: string; description: string };
  setNewSceneDraft: (draft: { name: string; description: string }) => void;
  onAddScene: () => void;
  onSaveSceneEdit: (
    scene: WorldScene,
    name: string,
    description: string,
  ) => void;
  onDeleteScene: (scene: WorldScene) => void;
  onTravelToScene: (scene: WorldScene) => void;
  /** Companion-feel knobs (proactive room nudges + grounding-line mode).
   * ``null`` until the settings snapshot loads. */
  companion: CompanionSettings | null;
  onPatchCompanion: (patch: Partial<CompanionSettings>) => void;
  /** H11 weather sync snapshot (``null`` until settings load). */
  weather: WeatherSettingsSnapshot | null;
  onPatchWeather: (patch: Record<string, unknown>) => void;
}
