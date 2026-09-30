import { useEffect, useState, type FormEvent } from "react";

import { api } from "@/api";
import { formatDateTime } from "@/lib/time";
import type { Reminder } from "@/types";
import { Section } from "./SettingsSection";

export function RemindersPanel() {
  const [items, setItems] = useState<Reminder[]>([]);
  const [text, setText] = useState("");
  const [dueAt, setDueAt] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  async function refresh() {
    try {
      setItems((await api.getReminders()).reminders);
      setError("");
    } catch (reason) {
      setError(String(reason));
    }
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  async function add(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const date = new Date(dueAt);
    const localValue = Number.isNaN(date.getTime()) ? "" : [
      date.getFullYear(), String(date.getMonth() + 1).padStart(2, "0"),
      String(date.getDate()).padStart(2, "0"), "T",
      String(date.getHours()).padStart(2, "0"), ":",
      String(date.getMinutes()).padStart(2, "0"),
    ].join("");
    if (!localValue || localValue !== dueAt.slice(0, 16) || date.getTime() <= Date.now()) {
      setError("Choose a future local date and time.");
      return;
    }
    setBusy(true);
    try {
      await api.setReminder(text.trim(), date.toISOString());
      setText("");
      setDueAt("");
      await refresh();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  async function cancel(id: number) {
    setBusy(true);
    try {
      await api.cancelReminder(id);
      await refresh();
    } catch (reason) {
      setError(String(reason));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Section title="Reminders">
      <form onSubmit={(event) => void add(event)} className="flex flex-wrap items-end gap-2">
        <label className="min-w-[12rem] flex-1 text-xs text-ink-100/70">
          Reminder
          <input
            className="mt-1 w-full rounded border border-white/20 bg-transparent px-2 py-1.5 text-ink-100"
            value={text} onChange={(event) => setText(event.target.value)}
            maxLength={500} required
          />
        </label>
        <label className="text-xs text-ink-100/70">
          Date and time
          <input
            type="datetime-local"
            className="mt-1 block rounded border border-white/20 bg-transparent px-2 py-1.5 text-ink-100"
            value={dueAt} onChange={(event) => setDueAt(event.target.value)} required
          />
        </label>
        <button
          type="submit" disabled={busy}
          className="rounded border border-white/20 px-3 py-1.5 text-xs text-ink-100 disabled:opacity-50"
        >
          Add
        </button>
      </form>
      {error ? <p role="alert" className="text-xs text-rose-300">{error}</p> : null}
      {items.length === 0 ? (
        <p className="text-xs text-ink-100/50">No pending reminders.</p>
      ) : (
        <ul className="divide-y divide-white/10">
          {items.map((item) => (
            <li key={item.id} className="flex items-center gap-3 py-2 text-xs">
              <span className="min-w-0 flex-1 break-words text-ink-100">{item.text}</span>
              <time className="shrink-0 font-mono text-ink-100/60" dateTime={item.due_at}>
                {formatDateTime(item.due_at)}
              </time>
              <button
                type="button" disabled={busy} onClick={() => void cancel(item.id)}
                className="shrink-0 text-rose-300 disabled:opacity-50"
              >
                Cancel
              </button>
            </li>
          ))}
        </ul>
      )}
    </Section>
  );
}
