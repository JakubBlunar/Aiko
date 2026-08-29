import { useState } from "react";
import type { WorldScene } from "../../types";

export interface SceneDraft {
  name: string;
  description: string;
}

interface WorldSceneBarProps {
  scenes: WorldScene[];
  viewingSceneId: number | null;
  currentSceneId: number | null;
  busy: boolean;
  newOpen: boolean;
  setNewOpen: (open: boolean) => void;
  newDraft: SceneDraft;
  setNewDraft: (draft: SceneDraft) => void;
  onSelect: (sceneId: number) => void;
  onAdd: () => void;
  onTravel: (scene: WorldScene) => void;
  onDelete: (scene: WorldScene) => void;
  onRename: (scene: WorldScene, name: string, description: string) => void;
}

/** Scene picker for the World tab: her apartment plus any places you authored. */
export function WorldSceneBar({
  scenes,
  viewingSceneId,
  currentSceneId,
  busy,
  newOpen,
  setNewOpen,
  newDraft,
  setNewDraft,
  onSelect,
  onAdd,
  onTravel,
  onDelete,
  onRename,
}: WorldSceneBarProps) {
  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editDesc, setEditDesc] = useState("");
  const viewing = scenes.find((s) => s.id === viewingSceneId) ?? null;
  const sheIsHere = viewing != null && viewing.id === currentSceneId;

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5">
        {scenes.map((scene) => {
          const selected = scene.id === viewingSceneId;
          const here = scene.id === currentSceneId;
          return (
            <button
              key={scene.id}
              type="button"
              onClick={() => onSelect(scene.id)}
              className={`rounded-full border px-2.5 py-0.5 text-[11px] ${
                selected
                  ? "border-ink-400/60 bg-ink-500/25 text-ink-100"
                  : "border-white/10 text-ink-100/70 hover:border-white/30"
              }`}
            >
              {scene.name}
              {here ? (
                <span className="ml-1 text-[9px] uppercase tracking-wide text-emerald-200/80">
                  aiko
                </span>
              ) : null}
            </button>
          );
        })}
        <button
          type="button"
          onClick={() => setNewOpen(!newOpen)}
          className="rounded-full border border-dashed border-white/20 px-2.5 py-0.5 text-[11px] text-ink-100/60 hover:border-emerald-400/60 hover:text-emerald-100"
        >
          {newOpen ? "Cancel" : "+ New scene"}
        </button>
      </div>

      {newOpen ? (
        <div className="space-y-2 rounded-md border border-emerald-400/30 bg-emerald-500/5 p-3">
          <input
            value={newDraft.name}
            onChange={(e) => setNewDraft({ ...newDraft, name: e.target.value })}
            placeholder="e.g. Jacob's room"
            className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
          />
          <input
            value={newDraft.description}
            onChange={(e) =>
              setNewDraft({ ...newDraft, description: e.target.value })
            }
            placeholder="What's this place like? (optional)"
            className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
          />
          <div className="flex justify-end">
            <button
              type="button"
              onClick={onAdd}
              disabled={busy || !newDraft.name.trim()}
              className="rounded border border-emerald-400/40 bg-emerald-500/10 px-3 py-1 text-[11px] text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Create scene
            </button>
          </div>
        </div>
      ) : null}

      {viewing ? (
        <div className="flex flex-wrap items-start justify-between gap-2 rounded-md border border-white/5 bg-white/[0.02] px-3 py-2">
          <div className="min-w-0 flex-1">
            {editingId === viewing.id ? (
              <div className="space-y-1.5">
                <input
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
                />
                <input
                  value={editDesc}
                  onChange={(e) => setEditDesc(e.target.value)}
                  placeholder="description"
                  className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
                />
                <div className="flex gap-1">
                  <button
                    type="button"
                    onClick={() => setEditingId(null)}
                    className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60"
                  >
                    Cancel
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      onRename(viewing, editName, editDesc);
                      setEditingId(null);
                    }}
                    disabled={busy || !editName.trim()}
                    className="rounded border border-ink-400/40 bg-ink-500/20 px-2 py-0.5 text-[11px] text-ink-100 disabled:opacity-50"
                  >
                    Save
                  </button>
                </div>
              </div>
            ) : (
              <>
                <div className="text-xs font-medium text-ink-100/90">
                  {viewing.name}
                  {viewing.origin === "builtin" ? (
                    <span className="ml-1.5 text-[9px] uppercase tracking-wide text-ink-100/40">
                      her place
                    </span>
                  ) : null}
                </div>
                {viewing.description ? (
                  <p className="text-[11px] text-ink-100/50">
                    {viewing.description}
                  </p>
                ) : (
                  <p className="text-[11px] text-ink-100/40">
                    {viewing.origin === "builtin"
                      ? "Seeded apartment and garden — add objects, not new rooms."
                      : "A place you authored. Add spots and objects, then invite her over."}
                  </p>
                )}
              </>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap gap-1">
            {sheIsHere ? (
              <span className="rounded border border-emerald-400/30 px-2 py-0.5 text-[11px] text-emerald-200/80">
                She's here
              </span>
            ) : (
              <button
                type="button"
                onClick={() => onTravel(viewing)}
                disabled={busy}
                className="rounded border border-emerald-400/40 bg-emerald-500/10 px-2 py-0.5 text-[11px] text-emerald-100 hover:border-emerald-400 disabled:opacity-50"
              >
                Bring Aiko here
              </button>
            )}
            {viewing.origin !== "builtin" && editingId !== viewing.id ? (
              <>
                <button
                  type="button"
                  onClick={() => {
                    setEditingId(viewing.id);
                    setEditName(viewing.name);
                    setEditDesc(viewing.description);
                  }}
                  className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400"
                >
                  rename
                </button>
                <button
                  type="button"
                  onClick={() => onDelete(viewing)}
                  className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                >
                  delete
                </button>
              </>
            ) : null}
          </div>
        </div>
      ) : null}
    </div>
  );
}
