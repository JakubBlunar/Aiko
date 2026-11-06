import { useState } from "react";
import type { WorldItem } from "../../types";

interface Revision {
  revision: number;
  title: string;
  form: string;
  text: string;
  at: string;
}

export function creationRevisions(state: WorldItem["state"]): Revision[] {
  if (state?.creation_schema !== 1 || !Array.isArray(state.revisions)) return [];
  return state.revisions.filter((value): value is Revision => {
    if (!value || typeof value !== "object") return false;
    const row = value as Partial<Revision>;
    return Number.isInteger(row.revision) && typeof row.title === "string" &&
      typeof row.form === "string" && typeof row.text === "string" && typeof row.at === "string";
  });
}

export function WorldCreation({ item }: { item: WorldItem }) {
  const [selected, setSelected] = useState("latest");
  const rows = creationRevisions(item.state);
  const current = rows.find((row) => String(row.revision) === selected) ?? rows.at(-1);
  if (!current) return null;
  const date = new Date(current.at);
  return (
    <details className="mt-2 rounded border border-white/10 p-2 text-xs text-ink-100/80">
      <summary className="cursor-pointer focus-visible:outline focus-visible:outline-ink-400">
        Read Aiko&apos;s writing
      </summary>
      <div className="mt-2 space-y-2">
        <label className="flex flex-wrap items-center gap-2">
          <span>Version</span>
          <select
            aria-label={`Version of ${item.name}`}
            value={selected}
            onChange={(event) => setSelected(event.target.value)}
            className="min-w-0 rounded border border-white/20 bg-ink-950 px-2 py-1"
          >
            <option value="latest">Latest (revision {rows.at(-1)?.revision})</option>
            {rows.map((row) => (
              <option key={row.revision} value={row.revision}>Revision {row.revision}</option>
            ))}
          </select>
        </label>
        <div className="font-medium">{current.title}</div>
        <div className="text-[11px] text-ink-100/50">
          Original {current.form.replaceAll("_", " ")} · creative writing
          {!Number.isNaN(date.getTime()) && <> · <time dateTime={current.at}>{date.toLocaleString()}</time></>}
        </div>
        <p className="whitespace-pre-wrap break-words text-sm leading-relaxed">{current.text}</p>
      </div>
    </details>
  );
}
