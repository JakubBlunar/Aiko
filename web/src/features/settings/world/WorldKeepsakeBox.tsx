import { useEffect, useId, useRef, useState } from "react";
import type { FormEvent } from "react";
import { api } from "@/api";
import type { KeepsakeNotesPage, WorldItem } from "@/types";

const dateFormat = new Intl.DateTimeFormat(undefined, { dateStyle: "medium" });
const buttonClass = "rounded border border-amber-300/25 px-2.5 py-1 text-amber-100 hover:bg-amber-200/10 disabled:opacity-50";

export function WorldKeepsakeBox({ item }: { item: WorldItem }) {
  const [open, setOpen] = useState(false);
  const [writing, setWriting] = useState(false);
  const [draft, setDraft] = useState("");
  const [before, setBefore] = useState<number | null>(null);
  const [page, setPage] = useState<KeepsakeNotesPage | null>(null);
  const [refresh, setRefresh] = useState(0);
  const [readError, setReadError] = useState(false);
  const [saveError, setSaveError] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saved, setSaved] = useState(false);
  const pending = useRef<{ text: string; key: string } | null>(null);
  const fieldId = useId();
  const latestNoteId = item.state.keepsake_latest_note_id;

  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    setPage(null);
    setReadError(false);
    api.readKeepsakeNotes(item.id, before, controller.signal).then(
      (result) => { if (!controller.signal.aborted) setPage(result); },
      () => { if (!controller.signal.aborted) setReadError(true); },
    );
    return () => controller.abort();
  }, [open, item.id, latestNoteId, before, refresh]);

  async function saveNote(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = draft.trim();
    if (!text || saving) return;
    if (pending.current?.text !== text) {
      const key = crypto.getRandomValues(new Uint32Array(4)).join("-");
      pending.current = { text, key };
    }
    setSaving(true);
    setSaveError(false);
    try {
      await api.leaveKeepsakeNote(item.id, text, pending.current.key);
      pending.current = null;
      setDraft("");
      setWriting(false);
      setSaved(true);
      setBefore(null);
      setRefresh((value) => value + 1);
    } catch {
      setSaveError(true);
    } finally {
      setSaving(false);
    }
  }

  return (
    <details className="mt-2 rounded border border-amber-300/20 bg-amber-200/[0.04] p-2"
      onToggle={(event) => setOpen(event.currentTarget.open)}>
      <summary className="cursor-pointer text-amber-100">Open the box</summary>
      {open && <div className="mt-2 space-y-2">
        {writing ? (
          <form onSubmit={saveNote} className="space-y-2">
            <label htmlFor={fieldId} className="block text-ink-100/90">Your note</label>
            <textarea id={fieldId} rows={3} maxLength={600} required disabled={saving}
              className="w-full resize-y rounded border border-white/15 bg-black/30 p-2 text-ink-100"
              value={draft} onChange={(event) => setDraft(event.target.value)} />
            <div className="flex gap-2">
              <button type="submit" className={buttonClass} disabled={saving || !draft.trim()}>
                {saving ? "Saving…" : "Put it in the box"}
              </button>
              <button type="button" className={buttonClass} disabled={saving}
                onClick={() => { setWriting(false); setSaveError(false); }}>Close note</button>
            </div>
            {saveError && <p role="alert" className="text-rose-200">
              Couldn’t save your note. Your draft is still here. Try again.
            </p>}
          </form>
        ) : (
          <button type="button" className={buttonClass}
            onClick={() => { setSaved(false); setWriting(true); }}>Write a note</button>
        )}
        {saved && <p role="status" className="text-amber-100/80">Your note is in the box.</p>}
        {readError ? (
          <div className="flex flex-wrap items-center gap-2">
            <p role="alert" className="text-rose-200">Couldn’t open the notes.</p>
            <button type="button" className={buttonClass}
              onClick={() => setRefresh((value) => value + 1)}>Try again</button>
          </div>
        ) : page === null ? (
          <p role="status" className="text-ink-100/65">Opening the box…</p>
        ) : page.notes.length === 0 ? (
          <p className="text-ink-100/65">The box is empty.</p>
        ) : (
          <ul className="space-y-2">
            {page.notes.map((note) => (
              <li key={note.id} className="rounded border border-amber-200/15 bg-amber-200/[0.06] p-2.5">
                <div className="flex flex-wrap items-baseline gap-x-2 text-[11px] text-amber-100/75">
                  <span className="font-medium">{note.author === "aiko" ? "Aiko" : "You"}</span>
                  <time dateTime={note.created_at}>{dateFormat.format(new Date(note.created_at))}</time>
                </div>
                <p className="mt-1 whitespace-pre-wrap break-words text-ink-100">{note.text}</p>
              </li>
            ))}
          </ul>
        )}
        <div className="flex flex-wrap gap-2">
          {before !== null && <button type="button" className={buttonClass}
            onClick={() => setBefore(null)}>Newest notes</button>}
          {page?.next_before_id != null && <button type="button" className={buttonClass}
            onClick={() => setBefore(page.next_before_id)}>Older notes</button>}
        </div>
      </div>}
    </details>
  );
}
