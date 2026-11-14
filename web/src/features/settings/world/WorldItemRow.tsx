import { WORLD_KINDS, isWorldPortableKind } from "@/types";
import type { WorldItem, WorldLocation } from "@/types";
import type { ItemDraft } from "./WorldTabTypes";
import { WorldCreation } from "../WorldCreation";
import { WorldDrink } from "../WorldDrink";
import { WorldBathroom } from "../WorldBathroom";
import { WorldSketchbook } from "../WorldSketchbook";

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

export function WorldItemRow({
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
                onChange={(e) => {
                  const kind = e.target.value;
                  const next: ItemDraft = { ...draft, kind };
                  if (
                    !isWorldPortableKind(kind) &&
                    next.location_id == null &&
                    locations[0]
                  ) {
                    next.location_id = locations[0].id;
                  }
                  setDraft(next);
                }}
                className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              >
                {WORLD_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k.replace(/_/g, " ")}
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
                {locations.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
                {isWorldPortableKind(draft.kind) ? (
                  <option value="">in her hands</option>
                ) : null}
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
            <WorldCreation item={item} />
            <WorldSketchbook item={item} />
            <WorldDrink item={item} />
            <WorldBathroom item={item} />
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/40">
              <span className="rounded bg-white/5 px-1.5 py-0.5 text-ink-100/60">
                {item.kind.replace(/_/g, " ")}
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
              disabled={Boolean(item.state.home_essential)}
              title={item.state.home_essential ? "Managed household fixture" : undefined}
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
