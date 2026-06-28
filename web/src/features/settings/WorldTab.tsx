import { useMemo } from "react";
import { useAssistantStore } from "../../store";
import {
  WORLD_ACTIVITIES,
  WORLD_KINDS,
  WORLD_POSTURES,
} from "../../types";
import type {
  CompanionSettings,
  GroundingLineMode,
  WorldActivity,
  WorldItem,
  WorldKind,
  WorldLocation,
  WorldPosture,
  WorldSnapshot,
} from "../../types";
import { Section } from "./SettingsSection";

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
  /** Companion-feel knobs (proactive room nudges + grounding-line mode).
   * ``null`` until the settings snapshot loads. */
  companion: CompanionSettings | null;
  onPatchCompanion: (patch: Partial<CompanionSettings>) => void;
}

export function buildQuickGivePresets(
  userDisplayName: string,
): ReadonlyArray<{ label: string; draft: GiveDraft }> {
  const giver = (userDisplayName || "").trim() || "you";
  return [
    {
      label: "🍪 Cookie",
      draft: {
        name: "cookies",
        kind: "food",
        quantity: 1,
        description: "a fresh, warm chocolate-chip cookie",
        location_id: null,
        consumable: true,
      },
    },
    {
      label: "🍵 Tea",
      draft: {
        name: "tea",
        kind: "food",
        quantity: 1,
        description: "a cup of jasmine tea",
        location_id: null,
        consumable: true,
      },
    },
    {
      label: "🧸 Plushy",
      draft: {
        name: "plushy",
        kind: "toy",
        quantity: 1,
        description: `a small soft plush, a gift from ${giver}`,
        location_id: null,
        consumable: false,
      },
    },
    {
      label: "🌷 Flower",
      draft: {
        name: "flower",
        kind: "decor",
        quantity: 1,
        description: "a single fresh flower",
        location_id: null,
        consumable: false,
      },
    },
  ];
}

export function WorldTab({
  world,
  busy,
  error,
  onRefresh,
  onPatchState,
  giveOpen,
  setGiveOpen,
  giveDraft,
  setGiveDraft,
  onGiveItem,
  locationsOpen,
  setLocationsOpen,
  itemsOpen,
  setItemsOpen,
  newLocationOpen,
  setNewLocationOpen,
  newLocationDraft,
  setNewLocationDraft,
  onAddLocation,
  editingItemId,
  setEditingItemId,
  itemDraft,
  setItemDraft,
  onSaveItemEdit,
  onDeleteItem,
  onConsumeItem,
  editingLocationId,
  setEditingLocationId,
  locationDraft,
  setLocationDraft,
  onSaveLocationEdit,
  onDeleteLocation,
  onReseedWorld,
  companion,
  onPatchCompanion,
}: WorldTabProps) {
  const identity = useAssistantStore((s) => s.identity);
  const quickGivePresets = useMemo(
    () => buildQuickGivePresets(identity?.user_display_name ?? ""),
    [identity?.user_display_name],
  );
  if (!world) {
    return (
      <Section title="World">
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          {busy ? "Loading Aiko's room..." : "World snapshot not available."}
        </p>
        {error ? (
          <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
            {error}
          </div>
        ) : null}
      </Section>
    );
  }

  const { state, locations, items } = world;
  const currentLocation =
    locations.find((l) => l.id === state.location_id) ?? null;
  const itemsByLocation = new Map<number | null, WorldItem[]>();
  for (const item of items) {
    const arr = itemsByLocation.get(item.location_id) ?? [];
    arr.push(item);
    itemsByLocation.set(item.location_id, arr);
  }
  for (const arr of itemsByLocation.values()) {
    arr.sort((a, b) => a.name.localeCompare(b.name));
  }
  const carriedItems = itemsByLocation.get(null) ?? [];

  return (
    <div className="space-y-4">
      <Section title="Right now">
        <p className="text-xs text-ink-100/70">
          Aiko is{" "}
          <span className="font-medium text-ink-100">
            {currentLocation
              ? `at ${currentLocation.name}`
              : "somewhere in her room"}
          </span>
          ,{" "}
          <span className="font-medium text-ink-100">
            {(state.posture || "sitting").replace("_", " ")}
          </span>
          ,{" "}
          <span className="font-medium text-ink-100">
            {(state.activity || "idle").replace("_", " ")}
          </span>
          .
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
            <span>Where:</span>
            <select
              value={state.location_id ?? ""}
              onChange={(e) =>
                onPatchState({
                  location_id: e.target.value ? Number(e.target.value) : null,
                })
              }
              className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
            >
              <option value="">(nowhere)</option>
              {locations.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
            <span>Posture:</span>
            <select
              value={state.posture}
              onChange={(e) => onPatchState({ posture: e.target.value })}
              className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
            >
              {WORLD_POSTURES.map((p: WorldPosture) => (
                <option key={p} value={p}>
                  {p.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
            <span>Activity:</span>
            <select
              value={state.activity}
              onChange={(e) => onPatchState({ activity: e.target.value })}
              className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
            >
              {WORLD_ACTIVITIES.map((a: WorldActivity) => (
                <option key={a} value={a}>
                  {a.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={onRefresh}
            disabled={busy}
            className="ml-auto rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "..." : "Refresh"}
          </button>
        </div>
        {state.mood_note ? (
          <p className="text-[11px] italic text-ink-100/50">
            "{state.mood_note}"
          </p>
        ) : null}
      </Section>

      <Section title="Give Aiko something">
        <p className="text-[11px] text-ink-100/50">
          Drops an item into her room, attributed to you. Aiko notices on
          her next reply — no proactive ping.
        </p>
        <div className="flex flex-wrap gap-2">
          {quickGivePresets.map((preset) => (
            <button
              key={preset.label}
              type="button"
              onClick={() => {
                setGiveDraft(preset.draft);
                setGiveOpen(true);
              }}
              disabled={busy}
              className="rounded border border-emerald-400/30 bg-emerald-500/5 px-3 py-1 text-xs text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {preset.label}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setGiveOpen(!giveOpen)}
            className="ml-auto rounded border border-white/10 px-3 py-1 text-xs text-ink-100/70 hover:border-emerald-400/60 hover:text-emerald-100"
          >
            {giveOpen ? "Cancel" : "Custom..."}
          </button>
        </div>
        {giveOpen ? (
          <div className="space-y-2 rounded-md border border-emerald-400/30 bg-emerald-500/5 p-3">
            <label className="block text-[11px] text-ink-100/60">
              <span>Name</span>
              <input
                value={giveDraft.name}
                onChange={(e) =>
                  setGiveDraft({ ...giveDraft, name: e.target.value })
                }
                placeholder="e.g. cookies"
                className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
              />
            </label>
            <label className="block text-[11px] text-ink-100/60">
              <span>Description (optional)</span>
              <input
                value={giveDraft.description}
                onChange={(e) =>
                  setGiveDraft({
                    ...giveDraft,
                    description: e.target.value,
                  })
                }
                placeholder="a fresh, warm chocolate-chip cookie"
                className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
              />
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                <span>Kind:</span>
                <select
                  value={giveDraft.kind}
                  onChange={(e) =>
                    setGiveDraft({ ...giveDraft, kind: e.target.value })
                  }
                  className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
                >
                  {WORLD_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                <span>Quantity:</span>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={giveDraft.quantity}
                  onChange={(e) =>
                    setGiveDraft({
                      ...giveDraft,
                      quantity: Math.max(1, Number(e.target.value) || 1),
                    })
                  }
                  className="w-14 rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
                />
              </label>
              <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                <input
                  type="checkbox"
                  checked={giveDraft.consumable}
                  onChange={(e) =>
                    setGiveDraft({
                      ...giveDraft,
                      consumable: e.target.checked,
                    })
                  }
                />
                <span>Consumable</span>
              </label>
              <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                <span>Where:</span>
                <select
                  value={giveDraft.location_id ?? ""}
                  onChange={(e) =>
                    setGiveDraft({
                      ...giveDraft,
                      location_id: e.target.value
                        ? Number(e.target.value)
                        : null,
                    })
                  }
                  className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
                >
                  <option value="">kitchenette (default)</option>
                  {locations.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={onGiveItem}
                disabled={busy || !giveDraft.name.trim()}
                className="rounded border border-emerald-400/40 bg-emerald-500/10 px-3 py-1 text-[11px] text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Give
              </button>
            </div>
          </div>
        ) : null}
      </Section>

      {companion ? (
        <Section title="Proactive room notices">
          <p className="text-[11px] text-ink-100/50">
            When on, Aiko occasionally reaches out about her room — when
            you've left her something, or after a long quiet stretch.
            Cooldown + daily cap keep it subtle, not chatty.
          </p>
          <label className="flex items-center gap-2 text-xs text-ink-100/70">
            <input
              type="checkbox"
              checked={companion.world_notice_enabled}
              onChange={(e) =>
                onPatchCompanion({ world_notice_enabled: e.target.checked })
              }
            />
            Enable proactive room / gift notices
          </label>
          <div className="grid grid-cols-2 gap-2">
            <label className="flex items-center justify-between gap-2 rounded-md bg-white/[0.02] px-3 py-1.5 text-[11px] text-ink-100/60">
              <span>Daily cap</span>
              <input
                type="number"
                min={0}
                max={24}
                value={companion.world_notice_daily_cap}
                disabled={!companion.world_notice_enabled}
                onChange={(e) =>
                  onPatchCompanion({
                    world_notice_daily_cap: Math.max(
                      0,
                      Number(e.target.value) || 0,
                    ),
                  })
                }
                className="w-16 rounded border border-white/10 bg-black/30 px-2 py-1 text-right text-ink-100/80 disabled:opacity-40"
              />
            </label>
            <label className="flex items-center justify-between gap-2 rounded-md bg-white/[0.02] px-3 py-1.5 text-[11px] text-ink-100/60">
              <span>Cooldown (s)</span>
              <input
                type="number"
                min={0}
                step={60}
                value={companion.world_notice_cooldown_seconds}
                disabled={!companion.world_notice_enabled}
                onChange={(e) =>
                  onPatchCompanion({
                    world_notice_cooldown_seconds: Math.max(
                      0,
                      Number(e.target.value) || 0,
                    ),
                  })
                }
                className="w-20 rounded border border-white/10 bg-black/30 px-2 py-1 text-right text-ink-100/80 disabled:opacity-40"
              />
            </label>
          </div>
        </Section>
      ) : null}

      {companion ? (
        <Section title="Companion feel">
          <label className="flex flex-col gap-1 text-[11px] text-ink-100/60">
            <span>
              Ambient grounding line — how Aiko's surroundings / mood are
              woven into her prompt.
            </span>
            <select
              value={companion.grounding_line_mode}
              onChange={(e) =>
                onPatchCompanion({
                  grounding_line_mode: e.target.value as GroundingLineMode,
                })
              }
              className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
            >
              <option value="off">off — granular blocks (default)</option>
              <option value="replace">replace — one fused line</option>
              <option value="split">split — fuse situational only</option>
            </select>
          </label>
        </Section>
      ) : null}

      {error ? (
        <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      ) : null}

      <Section title="Items">
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={() => setItemsOpen(!itemsOpen)}
            className="text-[11px] text-ink-100/60 hover:text-ink-100"
          >
            {itemsOpen ? "▾ collapse" : "▸ expand"}
          </button>
          <span className="text-[11px] text-ink-100/40">
            {items.length} item{items.length === 1 ? "" : "s"}
          </span>
        </div>
        {itemsOpen ? (
          <div className="space-y-3">
            {locations.map((loc) => {
              const here = itemsByLocation.get(loc.id) ?? [];
              if (here.length === 0) return null;
              return (
                <div key={loc.id} className="space-y-1">
                  <div className="text-[10px] uppercase tracking-wide text-ink-100/40">
                    {loc.name}
                  </div>
                  <ul className="space-y-1">
                    {here.map((item) => (
                      <ItemRow
                        key={item.id}
                        item={item}
                        locations={locations}
                        editing={editingItemId === item.id}
                        draft={itemDraft}
                        setDraft={setItemDraft}
                        onStartEdit={() => {
                          setEditingItemId(item.id);
                          setItemDraft({
                            name: item.name,
                            description: item.description,
                            kind: item.kind,
                            location_id: item.location_id,
                            quantity: item.quantity,
                          });
                        }}
                        onCancelEdit={() => setEditingItemId(null)}
                        onSave={() => onSaveItemEdit(item)}
                        onDelete={() => onDeleteItem(item)}
                        onConsume={() => onConsumeItem(item)}
                        busy={busy}
                      />
                    ))}
                  </ul>
                </div>
              );
            })}
            {carriedItems.length > 0 ? (
              <div className="space-y-1">
                <div className="text-[10px] uppercase tracking-wide text-ink-100/40">
                  carrying
                </div>
                <ul className="space-y-1">
                  {carriedItems.map((item) => (
                    <ItemRow
                      key={item.id}
                      item={item}
                      locations={locations}
                      editing={editingItemId === item.id}
                      draft={itemDraft}
                      setDraft={setItemDraft}
                      onStartEdit={() => {
                        setEditingItemId(item.id);
                        setItemDraft({
                          name: item.name,
                          description: item.description,
                          kind: item.kind,
                          location_id: item.location_id,
                          quantity: item.quantity,
                        });
                      }}
                      onCancelEdit={() => setEditingItemId(null)}
                      onSave={() => onSaveItemEdit(item)}
                      onDelete={() => onDeleteItem(item)}
                      onConsume={() => onConsumeItem(item)}
                      busy={busy}
                    />
                  ))}
                </ul>
              </div>
            ) : null}
            {items.length === 0 ? (
              <p className="text-xs text-ink-100/50">
                Nothing in the room yet.
              </p>
            ) : null}
          </div>
        ) : null}
      </Section>

      <Section title="Locations">
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={() => setLocationsOpen(!locationsOpen)}
            className="text-[11px] text-ink-100/60 hover:text-ink-100"
          >
            {locationsOpen ? "▾ collapse" : "▸ expand"}
          </button>
          <button
            type="button"
            onClick={() => setNewLocationOpen(!newLocationOpen)}
            className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/70 hover:border-emerald-400/60 hover:text-emerald-100"
          >
            {newLocationOpen ? "Cancel" : "+ Add"}
          </button>
        </div>
        {newLocationOpen ? (
          <div className="space-y-2 rounded-md border border-emerald-400/30 bg-emerald-500/5 p-3">
            <input
              value={newLocationDraft.name}
              onChange={(e) =>
                setNewLocationDraft({
                  ...newLocationDraft,
                  name: e.target.value,
                })
              }
              placeholder="Location name (e.g. 'the balcony')"
              className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
            />
            <input
              value={newLocationDraft.description}
              onChange={(e) =>
                setNewLocationDraft({
                  ...newLocationDraft,
                  description: e.target.value,
                })
              }
              placeholder="Description (optional)"
              className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
            />
            <div className="flex justify-end">
              <button
                type="button"
                onClick={onAddLocation}
                disabled={busy || !newLocationDraft.name.trim()}
                className="rounded border border-emerald-400/40 bg-emerald-500/10 px-3 py-1 text-[11px] text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Add
              </button>
            </div>
          </div>
        ) : null}
        {locationsOpen ? (
          <ul className="space-y-1.5">
            {locations.map((loc) => (
              <li
                key={loc.id}
                className="rounded-md border border-white/5 bg-white/[0.03] px-3 py-2 text-xs"
              >
                {editingLocationId === loc.id ? (
                  <div className="space-y-2">
                    <input
                      value={locationDraft.name}
                      onChange={(e) =>
                        setLocationDraft({
                          ...locationDraft,
                          name: e.target.value,
                        })
                      }
                      className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
                    />
                    <input
                      value={locationDraft.description}
                      onChange={(e) =>
                        setLocationDraft({
                          ...locationDraft,
                          description: e.target.value,
                        })
                      }
                      className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
                    />
                    <div className="flex justify-end gap-1">
                      <button
                        type="button"
                        onClick={() => setEditingLocationId(null)}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-white/30"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={() => onSaveLocationEdit(loc)}
                        disabled={busy || !locationDraft.name.trim()}
                        className="rounded border border-ink-400/40 bg-ink-500/20 px-2 py-0.5 text-[11px] text-ink-100 hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Save
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-ink-100/90">
                        {loc.name}
                      </div>
                      {loc.description ? (
                        <div className="text-[11px] text-ink-100/50">
                          {loc.description}
                        </div>
                      ) : null}
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <button
                        type="button"
                        onClick={() => {
                          setEditingLocationId(loc.id);
                          setLocationDraft({
                            name: loc.name,
                            description: loc.description,
                          });
                        }}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
                      >
                        edit
                      </button>
                      <button
                        type="button"
                        onClick={() => onDeleteLocation(loc)}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                      >
                        delete
                      </button>
                    </div>
                  </div>
                )}
              </li>
            ))}
            {locations.length === 0 ? (
              <p className="text-xs text-ink-100/50">No locations yet.</p>
            ) : null}
          </ul>
        ) : null}
      </Section>

      <Section title="Reset">
        <button
          type="button"
          onClick={onReseedWorld}
          disabled={busy}
          className="rounded border border-rose-400/30 bg-rose-500/5 px-3 py-1 text-xs text-rose-200 hover:border-rose-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Reset to default room
        </button>
        <p className="text-[10px] text-ink-100/40">
          Wipes the current room (all locations + items + state) and re-seeds
          the cozy default. Aiko's memories are not affected.
        </p>
      </Section>
    </div>
  );
}

interface ItemRowProps {
  item: WorldItem;
  locations: WorldLocation[];
  editing: boolean;
  draft: ItemDraft;
  setDraft: (draft: ItemDraft) => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSave: () => void;
  onDelete: () => void;
  onConsume: () => void;
  busy: boolean;
}

function ItemRow({
  item,
  locations,
  editing,
  draft,
  setDraft,
  onStartEdit,
  onCancelEdit,
  onSave,
  onDelete,
  onConsume,
  busy,
}: ItemRowProps) {
  return (
    <li
      className={`rounded-md border px-3 py-2 text-xs ${
        item.given_by === "user"
          ? "border-emerald-400/30 bg-emerald-500/5"
          : "border-white/5 bg-white/[0.03]"
      }`}
    >
      {editing ? (
        <div className="space-y-2">
          <input
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
          />
          <input
            value={draft.description}
            onChange={(e) =>
              setDraft({ ...draft, description: e.target.value })
            }
            placeholder="description"
            className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
          />
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>kind:</span>
              <select
                value={draft.kind}
                onChange={(e) => setDraft({ ...draft, kind: e.target.value })}
                className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              >
                {WORLD_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>where:</span>
              <select
                value={draft.location_id ?? ""}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    location_id: e.target.value
                      ? Number(e.target.value)
                      : null,
                  })
                }
                className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              >
                <option value="">carried</option>
                {locations.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>qty:</span>
              <input
                type="number"
                min={0}
                max={99}
                value={draft.quantity}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    quantity: Math.max(0, Number(e.target.value) || 0),
                  })
                }
                className="w-14 rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              />
            </label>
            <div className="ml-auto flex gap-1">
              <button
                type="button"
                onClick={onCancelEdit}
                className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-white/30"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={onSave}
                disabled={busy || !draft.name.trim()}
                className="rounded border border-ink-400/40 bg-ink-500/20 px-2 py-0.5 text-[11px] text-ink-100 hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Save
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="flex items-start justify-between gap-2">
          <div className="min-w-0 flex-1">
            <div className="text-ink-100/90">
              <span className="font-medium">{item.name}</span>
              {item.consumable || item.quantity > 1 ? (
                <span className="ml-1 text-[10px] uppercase tracking-wide text-ink-100/50">
                  ×{item.quantity}
                </span>
              ) : null}
              {item.given_by === "user" ? (
                <span className="ml-1 rounded bg-emerald-500/20 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-emerald-200">
                  gift
                </span>
              ) : null}
            </div>
            {item.description ? (
              <div className="text-[11px] text-ink-100/50">
                {item.description}
              </div>
            ) : null}
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/40">
              <span className="rounded bg-white/5 px-1.5 py-0.5 text-ink-100/60">
                {item.kind}
              </span>
              {item.consumable ? <span>consumable</span> : null}
              {item.kind === "plant" &&
              typeof item.state?.stage === "string" ? (
                <span
                  className={`rounded px-1.5 py-0.5 ${
                    item.state.stage === "mature"
                      ? "bg-amber-500/20 text-amber-200"
                      : "bg-emerald-500/15 text-emerald-200/80"
                  }`}
                >
                  {item.state.stage === "mature"
                    ? "ready to harvest"
                    : String(item.state.stage)}
                </span>
              ) : null}
              {item.kind === "seed" &&
              typeof item.state?.species === "string" ? (
                <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-200/80">
                  {String(item.state.species)} seed
                </span>
              ) : null}
            </div>
          </div>
          <div className="flex shrink-0 flex-col gap-1">
            {item.consumable && item.quantity > 0 ? (
              <button
                type="button"
                onClick={onConsume}
                className="rounded border border-amber-400/30 bg-amber-500/5 px-2 py-0.5 text-[11px] text-amber-200 hover:border-amber-400/60"
              >
                consume
              </button>
            ) : null}
            <button
              type="button"
              onClick={onStartEdit}
              className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
            >
              edit
            </button>
            <button
              type="button"
              onClick={onDelete}
              className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
            >
              remove
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
