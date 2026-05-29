import { useEffect, useState } from "react";
import { api } from "../../api";
import { useAssistantStore } from "../../store";
import { Section } from "./SettingsSection";

export function IdentitySection() {
  const identity = useAssistantStore((s) => s.identity);
  const setIdentity = useAssistantStore((s) => s.setIdentity);
  const pushToast = useAssistantStore((s) => s.pushToast);
  const [draft, setDraft] = useState<string>("");
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Keep the local draft in sync when the upstream identity changes
  // (e.g. another window rename, or the hello frame lands late).
  useEffect(() => {
    if (!editing) {
      setDraft(identity?.user_display_name ?? "");
    }
  }, [identity?.user_display_name, editing]);

  const current = identity?.user_display_name ?? "";

  const save = async () => {
    const cleaned = draft.trim();
    if (!cleaned) {
      setError("Name can't be empty.");
      return;
    }
    if (cleaned === current) {
      setEditing(false);
      setError(null);
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const next = await api.setIdentity(cleaned);
      setIdentity(next);
      pushToast("info", `Aiko will call you ${next.user_display_name}.`);
      setEditing(false);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Couldn't save.");
    } finally {
      setSaving(false);
    }
  };

  return (
    <Section title="Identity">
      <div className="rounded-md border border-white/10 bg-black/40 px-3 py-2">
        <div className="text-xs text-ink-100/60">What Aiko calls you</div>
        {editing ? (
          <div className="mt-2 flex items-center gap-2">
            <input
              type="text"
              value={draft}
              maxLength={32}
              autoFocus
              onChange={(e) => {
                setDraft(e.target.value);
                if (error) setError(null);
              }}
              disabled={saving}
              className="flex-1 rounded-md border border-white/10 bg-black/40 px-3 py-1.5 text-sm text-ink-100 focus:border-sky-500 focus:outline-none"
            />
            <button
              type="button"
              onClick={() => void save()}
              disabled={saving || draft.trim().length === 0}
              className="rounded-md bg-sky-600 px-3 py-1.5 text-xs font-medium text-white hover:bg-sky-500 disabled:opacity-50"
            >
              {saving ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={() => {
                setEditing(false);
                setDraft(current);
                setError(null);
              }}
              disabled={saving}
              className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-ink-100/70 hover:bg-white/5"
            >
              Cancel
            </button>
          </div>
        ) : (
          <div className="mt-1 flex items-center justify-between">
            <span className="text-sm text-ink-100">{current || "(not set)"}</span>
            <button
              type="button"
              onClick={() => setEditing(true)}
              className="rounded-md border border-white/10 px-3 py-1 text-xs text-ink-100/70 hover:bg-white/5"
            >
              Change
            </button>
          </div>
        )}
        {error ? (
          <p className="mt-2 text-xs text-rose-400" role="alert">
            {error}
          </p>
        ) : null}
      </div>
    </Section>
  );
}
