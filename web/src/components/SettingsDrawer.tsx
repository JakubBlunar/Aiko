import { useCallback, useEffect, useState } from "react";
import { api, type AudioDevices } from "../api";
import type { AssistantSettings, Memory } from "../types";
import { useAssistantStore } from "../store";

interface SettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

export function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [voices, setVoices] = useState<string[]>([]);
  const [devices, setDevices] = useState<AudioDevices>({ input: [], output: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [memoriesOpen, setMemoriesOpen] = useState(false);
  const memories = useAssistantStore((s) => s.memories);
  const memoriesEnabled = useAssistantStore((s) => s.memoriesEnabled);
  const setMemories = useAssistantStore((s) => s.setMemories);
  const removeMemory = useAssistantStore((s) => s.removeMemory);

  const refreshAll = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const [s, m, v, d] = await Promise.all([
        api.getSettings(),
        api.listModels().catch(() => []),
        api.listVoices().catch(() => []),
        api.listAudioDevices().catch(() => ({ input: [], output: [] })),
      ]);
      setSettings(s);
      setModels(m);
      setVoices(v);
      setDevices(d);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }, []);

  const refreshMemories = useCallback(async () => {
    try {
      const data = await api.listMemories(50, "recent");
      setMemories(data.memories, data.enabled);
    } catch (err) {
      setError(String(err));
    }
  }, [setMemories]);

  useEffect(() => {
    if (open) {
      void refreshAll();
    }
  }, [open, refreshAll]);

  useEffect(() => {
    if (open && memoriesOpen) {
      void refreshMemories();
    }
  }, [open, memoriesOpen, refreshMemories]);

  const onDeleteMemory = async (memory: Memory) => {
    try {
      await api.deleteMemory(memory.id);
      removeMemory(memory.id);
    } catch (err) {
      setError(String(err));
    }
  };

  const apply = async (patch: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    try {
      const next = await api.patchSettings(patch);
      setSettings(next);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-30 flex">
      <div
        className="flex-1 bg-black/60 backdrop-blur-sm"
        onClick={onClose}
        role="presentation"
      />
      <div className="flex h-full w-full max-w-md flex-col border-l border-white/10 bg-[#0f0a1f] shadow-2xl">
        <header className="flex items-center justify-between border-b border-white/5 px-5 py-4">
          <div>
            <h2 className="text-base font-semibold text-ink-100">Settings</h2>
            <p className="text-xs text-ink-100/50">
              Changes apply instantly and persist via the backend.
            </p>
          </div>
          <button
            type="button"
            onClick={onClose}
            className="rounded-md border border-white/10 px-3 py-1 text-xs text-ink-100/70 hover:border-ink-400 hover:text-ink-100"
          >
            Close
          </button>
        </header>

        <div className="flex-1 space-y-6 overflow-y-auto px-5 py-5 text-sm">
          {error ? (
            <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
              {error}
            </div>
          ) : null}
          {busy && !settings ? (
            <div className="text-xs text-ink-100/50">Loading...</div>
          ) : settings ? (
            <>
              <Section title="Chat model">
                <label className="block text-xs text-ink-100/60">Model</label>
                <select
                  value={settings.chat.model}
                  onChange={(e) =>
                    void apply({ chat: { model: e.target.value } })
                  }
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
                >
                  {(models.length > 0 ? models : [settings.chat.model]).map(
                    (model) => (
                      <option key={model} value={model}>
                        {model}
                      </option>
                    ),
                  )}
                </select>
                <Row label="Context window" value={settings.chat.context_window.toLocaleString()} />
                <Row label="Temperature" value={settings.chat.temperature.toFixed(2)} />
                <Row label="Max tokens" value={String(settings.chat.max_tokens)} />
              </Section>

              <Section title="Voice (TTS)">
                <label className="block text-xs text-ink-100/60">Voice</label>
                <select
                  value={settings.tts.voice}
                  onChange={(e) =>
                    void apply({ tts: { voice: e.target.value } })
                  }
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
                >
                  {(voices.length > 0 ? voices : [settings.tts.voice]).map(
                    (v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ),
                  )}
                </select>
                <label className="mt-3 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={settings.tts.enabled}
                    onChange={(e) =>
                      void apply({ tts: { enabled: e.target.checked } })
                    }
                  />
                  Speak responses out loud
                </label>
                <Row label="Provider" value={settings.tts.provider} />
              </Section>

              <Section title="Audio devices">
                <label className="block text-xs text-ink-100/60">Microphone</label>
                <select
                  value={settings.audio.microphone_device ?? ""}
                  onChange={(e) =>
                    void apply({
                      audio: {
                        microphone_device:
                          e.target.value === "" ? null : Number(e.target.value),
                      },
                    })
                  }
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
                >
                  <option value="">System default</option>
                  {devices.input.map((d) => (
                    <option key={d.index} value={d.index}>
                      [{d.index}] {d.name}
                    </option>
                  ))}
                </select>
                <label className="mt-3 block text-xs text-ink-100/60">
                  Output
                </label>
                <select
                  value={settings.audio.output_device ?? ""}
                  onChange={(e) =>
                    void apply({
                      audio: {
                        output_device:
                          e.target.value === "" ? null : Number(e.target.value),
                      },
                    })
                  }
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
                >
                  <option value="">System default</option>
                  {devices.output.map((d) => (
                    <option key={d.index} value={d.index}>
                      [{d.index}] {d.name}
                    </option>
                  ))}
                </select>
                <Row label="STT model" value={settings.stt.model} />
                <label className="mt-3 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={settings.audio.barge_in_enabled}
                    onChange={(e) =>
                      void apply({
                        audio: { barge_in_enabled: e.target.checked },
                      })
                    }
                  />
                  Allow barge-in (interrupt while Aiko is speaking)
                </label>
              </Section>

              <Section title="What Aiko remembers">
                <button
                  type="button"
                  onClick={() => setMemoriesOpen((v) => !v)}
                  className="flex w-full items-center justify-between rounded-md border border-white/10 bg-black/30 px-3 py-2 text-left text-xs text-ink-100/70 hover:border-ink-400 hover:text-ink-100"
                >
                  <span>
                    {memoriesOpen
                      ? "Hide memories"
                      : "Show long-term memories"}
                  </span>
                  <span className="font-mono text-ink-100/40">
                    {memoriesEnabled ? `${memories.length}` : "off"}
                  </span>
                </button>
                {memoriesOpen ? (
                  !memoriesEnabled ? (
                    <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
                      Long-term memory is disabled in config (memory.enabled).
                    </p>
                  ) : memories.length === 0 ? (
                    <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
                      Nothing remembered yet. Memories are mined after a few
                      turns of conversation, or whenever Aiko writes a private
                      [[remember]] tag.
                    </p>
                  ) : (
                    <ul className="space-y-1.5">
                      {memories.map((memory) => (
                        <li
                          key={memory.id}
                          className="flex items-start justify-between gap-2 rounded-md border border-white/5 bg-white/[0.03] px-3 py-2 text-xs text-ink-100/80"
                        >
                          <div className="min-w-0 flex-1">
                            <p className="break-words">{memory.content}</p>
                            <div className="mt-1 flex items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/40">
                              <span className="rounded bg-white/5 px-1.5 py-0.5 text-ink-100/60">
                                {memory.kind}
                              </span>
                              <span>
                                salience {(memory.salience * 100).toFixed(0)}%
                              </span>
                              {memory.use_count > 0 ? (
                                <span>used {memory.use_count}x</span>
                              ) : null}
                            </div>
                          </div>
                          <button
                            type="button"
                            onClick={() => void onDeleteMemory(memory)}
                            className="shrink-0 rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                            aria-label={`Forget memory ${memory.id}`}
                          >
                            forget
                          </button>
                        </li>
                      ))}
                    </ul>
                  )
                ) : null}
              </Section>
            </>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function Section({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-ink-100/50">
        {title}
      </h3>
      <div className="space-y-2">{children}</div>
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center justify-between rounded-md bg-white/[0.02] px-3 py-1.5 text-[11px] text-ink-100/60">
      <span>{label}</span>
      <span className="font-mono text-ink-100/80">{value}</span>
    </div>
  );
}
