import { useState } from "react";
import type { WorldItem } from "../../types";

interface Stroke { points: [number, number][]; width: number; shade: number; filled: boolean }
interface Drawing { title: string; note: string; at: string; revision: number; strokes: Stroke[] }
interface Page { id: string; revisions: Drawing[] }
const palette = ["#292524", "#57534e", "#a8a29e", "#d6d3d1"];

export function sketchPages(state: WorldItem["state"]): Page[] {
  const book = state.home_sketchbook as { schema?: number; pages?: unknown[] } | undefined;
  if (book?.schema !== 1 || !Array.isArray(book.pages)) return [];
  return book.pages.slice(0, 16).filter((raw): raw is Page => {
    if (!raw || typeof raw !== "object") return false;
    const page = raw as Partial<Page>;
    return typeof page.id === "string" && Array.isArray(page.revisions) &&
      page.revisions.length > 0 && page.revisions.length <= 4 && page.revisions.every((drawing) =>
        drawing && typeof drawing === "object" && typeof drawing.title === "string" &&
        Number.isInteger(drawing.revision) && Array.isArray(drawing.strokes) &&
        drawing.strokes.length > 0 && drawing.strokes.length <= 24 && drawing.strokes.every((stroke) =>
          stroke && typeof stroke === "object" && Array.isArray(stroke.points) &&
          stroke.points.length >= 2 && stroke.points.length <= 32 &&
          Number.isInteger(stroke.shade) && stroke.shade >= 0 && stroke.shade < palette.length &&
          Number.isFinite(stroke.width) && stroke.width >= 0.5 && stroke.width <= 8 &&
          stroke.points.every((point) => Array.isArray(point) && point.length === 2 &&
            point.every(Number.isFinite) && point[0] >= 0 && point[0] <= 512 &&
            point[1] >= 0 && point[1] <= 320)),
      );
  });
}

export function WorldSketchbook({ item }: { item: WorldItem }) {
  const [selected, setSelected] = useState("latest");
  const [version, setVersion] = useState("latest");
  const book = item.state.home_sketchbook as { schema?: number; reference?: string } | undefined;
  const pages = sketchPages(item.state);
  if (book?.schema !== 1) return null;
  const page = pages.find((entry) => entry.id === selected) ?? pages.at(-1);
  const drawing = page?.revisions.find((entry) => String(entry.revision) === version)
    ?? page?.revisions.at(-1);
  return (
    <details className="mt-2 rounded border border-white/10 p-2 text-xs text-ink-100/80">
      <summary className="cursor-pointer">Open sketchbook · {pages.length} saved pages</summary>
      {!drawing ? <p className="mt-2 text-ink-100/50">Blank sketchbook. No saved drawings yet.</p> : <>
        <div className="my-2 flex flex-wrap gap-2">
          <select aria-label="Sketchbook page" value={selected}
            onChange={(event) => { setSelected(event.target.value); setVersion("latest"); }}
            className="max-w-full rounded bg-black/30 p-1">
            <option value="latest">Latest page</option>
            {pages.map((entry, index) => <option key={entry.id} value={entry.id}>
              {index + 1}. {entry.revisions.at(-1)?.title}
            </option>)}
          </select>
          <select aria-label="Sketch revision" value={version}
            onChange={(event) => setVersion(event.target.value)} className="rounded bg-black/30 p-1">
            <option value="latest">Latest revision</option>
            {page?.revisions.map((entry) => <option key={entry.revision} value={entry.revision}>
              Revision {entry.revision}
            </option>)}
          </select>
        </div>
        <svg viewBox="0 0 512 320" role="img" aria-label={drawing.title}
          className="w-full rounded bg-[#faf7ef]">
          <title>{drawing.title}</title>
          <rect width="512" height="320" fill="#faf7ef" />
          {drawing.strokes.map((stroke, index) => {
            const Shape = stroke.filled ? "polygon" : "polyline";
            return <Shape key={index} points={stroke.points.map((point) => point.join(",")).join(" ")}
              fill={stroke.filled ? palette[stroke.shade] : "none"}
              stroke={palette[stroke.shade]} strokeWidth={stroke.width}
              strokeLinejoin="round" strokeLinecap="round" />;
          })}
        </svg>
        <p className="mt-1">{drawing.title} · revision {drawing.revision}</p>
        {drawing.note ? <p className="mt-1 whitespace-pre-wrap">{drawing.note}</p> : null}
        <a className="mt-1 inline-block underline" target="_blank" rel="noreferrer"
          href={`/api/world/items/${item.id}/sketch.svg?page=${pages.indexOf(page!) + 1}&revision=${drawing.revision}`}>
          Open saved drawing
        </a>
      </>}
      <p className="mt-2 text-[10px] text-ink-100/45">
        Original creative drawing · {book.reference ?? "Authored fictional reference"}
      </p>
    </details>
  );
}
