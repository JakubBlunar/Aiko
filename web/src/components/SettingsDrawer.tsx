import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { api, type AudioDevices } from "../api";
import type { AssistantSettings, Memory, RagDocument } from "../types";
import { useAssistantStore } from "../store";

const REACTIONS_FOR_MAPPING = [
  "neutral",
  "cheerful",
  "excited",
  "surprised",
  "sad",
  "angry",
  "calm",
  "serious",
  "friendly",
  "gentle",
  "enthusiastic",
] as const;

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

  const persona = useAssistantStore((s) => s.persona);
  const setPersona = useAssistantStore((s) => s.setPersona);
  const personaFileRef = useRef<HTMLInputElement | null>(null);
  const [personaBusy, setPersonaBusy] = useState(false);
  const [personaError, setPersonaError] = useState<string | null>(null);

  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [documentsBusy, setDocumentsBusy] = useState(false);
  const [documentsError, setDocumentsError] = useState<string | null>(null);
  const [documentsLoaded, setDocumentsLoaded] = useState(false);
  const documentFileRef = useRef<HTMLInputElement | null>(null);

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

  const onUploadPersona = async (file: File) => {
    setPersonaBusy(true);
    setPersonaError(null);
    try {
      const next = await api.uploadPersona(file);
      setPersona(next);
    } catch (err) {
      setPersonaError(String(err));
    } finally {
      setPersonaBusy(false);
      if (personaFileRef.current) {
        personaFileRef.current.value = "";
      }
    }
  };

  const onRemovePersona = async () => {
    setPersonaBusy(true);
    setPersonaError(null);
    try {
      await api.deletePersona();
      setPersona(null);
    } catch (err) {
      setPersonaError(String(err));
    } finally {
      setPersonaBusy(false);
    }
  };

  const refreshDocuments = useCallback(async () => {
    setDocumentsBusy(true);
    setDocumentsError(null);
    try {
      const res = await api.listDocuments();
      setDocuments(res.documents);
      setDocumentsLoaded(true);
    } catch (err) {
      setDocumentsError(String(err));
    } finally {
      setDocumentsBusy(false);
    }
  }, []);

  useEffect(() => {
    if (open && !documentsLoaded) {
      void refreshDocuments();
    }
  }, [open, documentsLoaded, refreshDocuments]);

  const onUploadDocument = async (file: File) => {
    setDocumentsBusy(true);
    setDocumentsError(null);
    try {
      const result = await api.uploadDocument(file);
      setDocuments(result.documents);
    } catch (err) {
      setDocumentsError(String(err));
    } finally {
      setDocumentsBusy(false);
      if (documentFileRef.current) {
        documentFileRef.current.value = "";
      }
    }
  };

  const onDeleteDocument = async (document_id: string) => {
    setDocumentsBusy(true);
    setDocumentsError(null);
    try {
      const result = await api.deleteDocument(document_id);
      setDocuments(result.documents);
    } catch (err) {
      setDocumentsError(String(err));
    } finally {
      setDocumentsBusy(false);
    }
  };

  const onPatchMapping = async (patch: {
    reaction_mapping?: Record<string, string>;
    idle_motion_group?: string | null;
    talk_motion_group?: string | null;
  }) => {
    setPersonaBusy(true);
    setPersonaError(null);
    try {
      const result = await api.patchPersonaMapping(patch);
      setPersona(result.persona);
    } catch (err) {
      setPersonaError(String(err));
    } finally {
      setPersonaBusy(false);
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

              <Section title="Proactive nudges (voice mode)">
                <p className="text-[11px] text-ink-100/50">
                  When voice mode is on and you've been quiet, Aiko can pick
                  up a thread on her own. Tune how patient she is here.
                </p>
                <Row
                  label="Silence threshold (s)"
                  value={
                    <input
                      type="number"
                      min={10}
                      step={5}
                      value={settings.proactive?.silence_seconds ?? 45}
                      onChange={(e) =>
                        void apply({
                          proactive: {
                            silence_seconds: Number(e.target.value),
                          },
                        })
                      }
                      className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
                    />
                  }
                />
                <Row
                  label="Cooldown (s)"
                  value={
                    <input
                      type="number"
                      min={30}
                      step={10}
                      value={settings.proactive?.cooldown_seconds ?? 120}
                      onChange={(e) =>
                        void apply({
                          proactive: {
                            cooldown_seconds: Number(e.target.value),
                          },
                        })
                      }
                      className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
                    />
                  }
                />
              </Section>

              <Section title="Tools">
                <p className="text-[11px] text-ink-100/50">
                  Tools let Aiko reach for fresh facts before answering: the
                  current time, your notebook, or the public web. Disable any
                  she shouldn't use.
                </p>
                <label className="mt-1 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={settings.tools?.enabled ?? true}
                    onChange={(e) =>
                      void apply({ tools: { enabled: e.target.checked } })
                    }
                  />
                  Tools enabled
                </label>
                <label className="ml-4 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={settings.tools?.get_time ?? true}
                    disabled={!(settings.tools?.enabled ?? true)}
                    onChange={(e) =>
                      void apply({ tools: { get_time: e.target.checked } })
                    }
                  />
                  get_time — current date/time
                </label>
                <label className="ml-4 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={settings.tools?.recall ?? true}
                    disabled={!(settings.tools?.enabled ?? true)}
                    onChange={(e) =>
                      void apply({ tools: { recall: e.target.checked } })
                    }
                  />
                  recall — search Aiko's notebook
                </label>
                <label className="ml-4 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={settings.tools?.web_search ?? true}
                    disabled={!(settings.tools?.enabled ?? true)}
                    onChange={(e) =>
                      void apply({ tools: { web_search: e.target.checked } })
                    }
                  />
                  web_search — DuckDuckGo
                </label>
                {settings.tools?.available && settings.tools.available.length > 0 ? (
                  <div className="rounded-md bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/60">
                    Active: {settings.tools.available.join(", ")}
                  </div>
                ) : (
                  <div className="rounded-md bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/50 italic">
                    No tools currently available.
                  </div>
                )}
              </Section>

              <Section title="Persona avatar (Live2D)">
                {personaError ? (
                  <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                    {personaError}
                  </div>
                ) : null}
                <div className="flex items-center justify-between rounded-md bg-white/[0.02] px-3 py-2 text-[11px]">
                  <span className="text-ink-100/60">Active</span>
                  <span className="font-mono text-ink-100/80">
                    {persona
                      ? `${persona.display_name} (Cubism v${persona.cubism_version})`
                      : "No model loaded"}
                  </span>
                </div>
                <div className="flex items-center gap-2">
                  <input
                    ref={personaFileRef}
                    type="file"
                    accept=".zip,application/zip,application/x-zip-compressed"
                    disabled={personaBusy}
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      if (file) {
                        void onUploadPersona(file);
                      }
                    }}
                    className="block w-full text-xs text-ink-100/70 file:mr-3 file:rounded-md file:border-0 file:bg-ink-400/30 file:px-3 file:py-1.5 file:text-xs file:text-ink-100 hover:file:bg-ink-400/50"
                  />
                </div>
                {persona ? (
                  <>
                    <button
                      type="button"
                      onClick={() => void onRemovePersona()}
                      disabled={personaBusy}
                      className="w-full rounded-md border border-white/10 px-3 py-1.5 text-xs text-ink-100/70 hover:border-rose-400/60 hover:text-rose-200 disabled:opacity-50"
                    >
                      Remove model
                    </button>
                    {persona.expressions.length > 0 ? (
                      <div className="mt-2 space-y-1.5">
                        <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
                          Reaction expressions
                        </p>
                        {REACTIONS_FOR_MAPPING.map((reaction) => (
                          <div
                            key={reaction}
                            className="flex items-center justify-between gap-2 rounded-md bg-white/[0.02] px-2 py-1 text-[11px]"
                          >
                            <span className="text-ink-100/60">{reaction}</span>
                            <select
                              value={persona.reaction_mapping[reaction] ?? ""}
                              onChange={(e) =>
                                void onPatchMapping({
                                  reaction_mapping: {
                                    ...persona.reaction_mapping,
                                    [reaction]: e.target.value,
                                  },
                                })
                              }
                              disabled={personaBusy}
                              className="max-w-[55%] rounded-md border border-white/10 bg-black/40 px-2 py-1 text-xs text-ink-100"
                            >
                              <option value="">(none)</option>
                              {persona.expressions.map((expr) => (
                                <option key={expr.name} value={expr.name}>
                                  {expr.name}
                                </option>
                              ))}
                            </select>
                          </div>
                        ))}
                      </div>
                    ) : null}
                    {Object.keys(persona.motions).length > 0 ? (
                      <div className="mt-2 space-y-1.5">
                        <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
                          Motion groups
                        </p>
                        <div className="flex items-center justify-between gap-2 rounded-md bg-white/[0.02] px-2 py-1 text-[11px]">
                          <span className="text-ink-100/60">Idle</span>
                          <select
                            value={persona.idle_motion_group ?? ""}
                            onChange={(e) =>
                              void onPatchMapping({
                                idle_motion_group: e.target.value || null,
                              })
                            }
                            disabled={personaBusy}
                            className="max-w-[55%] rounded-md border border-white/10 bg-black/40 px-2 py-1 text-xs text-ink-100"
                          >
                            <option value="">(none)</option>
                            {Object.keys(persona.motions).map((group) => (
                              <option key={group} value={group}>
                                {group}
                              </option>
                            ))}
                          </select>
                        </div>
                        <div className="flex items-center justify-between gap-2 rounded-md bg-white/[0.02] px-2 py-1 text-[11px]">
                          <span className="text-ink-100/60">Talk</span>
                          <select
                            value={persona.talk_motion_group ?? ""}
                            onChange={(e) =>
                              void onPatchMapping({
                                talk_motion_group: e.target.value || null,
                              })
                            }
                            disabled={personaBusy}
                            className="max-w-[55%] rounded-md border border-white/10 bg-black/40 px-2 py-1 text-xs text-ink-100"
                          >
                            <option value="">(none)</option>
                            {Object.keys(persona.motions).map((group) => (
                              <option key={group} value={group}>
                                {group}
                              </option>
                            ))}
                          </select>
                        </div>
                      </div>
                    ) : null}
                  </>
                ) : (
                  <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/50">
                    Upload a single Live2D model packaged as a .zip. The archive
                    must contain a *.model3.json (Cubism 3+) or *.model.json
                    (Cubism 2.1) entrypoint.
                  </p>
                )}
              </Section>

              <Section title="Documents (RAG)">
                <p className="text-[11px] text-ink-100/50">
                  Drop in notes, docs, or PDFs and Aiko will be able to pull
                  relevant chunks into the conversation. Indexed into the same
                  retrieval substrate as chat history and memories.
                </p>
                {documentsError ? (
                  <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                    {documentsError}
                  </div>
                ) : null}
                <div className="flex items-center gap-2">
                  <input
                    ref={documentFileRef}
                    type="file"
                    accept=".md,.markdown,.txt,.pdf"
                    disabled={documentsBusy}
                    onChange={(e) => {
                      const f = e.target.files?.[0];
                      if (f) void onUploadDocument(f);
                    }}
                    className="block w-full text-xs text-ink-100/70 file:mr-3 file:rounded file:border file:border-white/10 file:bg-white/5 file:px-2 file:py-1 file:text-xs file:text-ink-100"
                  />
                </div>
                {documents.length === 0 ? (
                  <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
                    No documents uploaded yet.
                  </p>
                ) : (
                  <ul className="space-y-1.5">
                    {documents.map((doc) => (
                      <li
                        key={doc.document_id}
                        className="flex items-start justify-between gap-2 rounded-md border border-white/5 bg-white/[0.03] px-3 py-2 text-xs text-ink-100/80"
                      >
                        <div className="min-w-0 flex-1">
                          <p className="truncate font-medium">{doc.title}</p>
                          <div className="mt-1 flex items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/40">
                            <span>{doc.chunk_count} chunks</span>
                            <span className="font-mono">{doc.document_id}</span>
                          </div>
                        </div>
                        <button
                          type="button"
                          disabled={documentsBusy}
                          onClick={() => void onDeleteDocument(doc.document_id)}
                          className="shrink-0 rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                          aria-label={`Remove document ${doc.title}`}
                        >
                          remove
                        </button>
                      </li>
                    ))}
                  </ul>
                )}
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

function Row({
  label,
  value,
}: {
  label: string;
  value: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between rounded-md bg-white/[0.02] px-3 py-1.5 text-[11px] text-ink-100/60">
      <span>{label}</span>
      <span className="font-mono text-ink-100/80">{value}</span>
    </div>
  );
}
