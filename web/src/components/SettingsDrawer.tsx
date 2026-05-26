import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api, type AudioDevices } from "../api";
import { desktop as desktopCommands } from "../desktop/commands";
import { isTauri } from "../desktop/runtime";
import type {
  AssistantSettings,
  AvatarSettingsKnobs,
  Memory,
  MemoryOrder,
  MetricsResponse,
  PersonaWindowSettings,
  RagDocument,
} from "../types";
import { MEMORY_KINDS } from "../types";
import { useAssistantStore } from "../store";

interface SettingsDrawerProps {
  open: boolean;
  onClose: () => void;
}

type SettingsTabId =
  | "chat"
  | "voice"
  | "tools"
  | "avatar"
  | "memory"
  | "knowledge";

interface TabSpec {
  id: SettingsTabId;
  label: string;
  icon: string;
}

const SETTINGS_TABS: ReadonlyArray<TabSpec> = [
  { id: "chat", label: "Chat", icon: "💬" },
  { id: "voice", label: "Voice", icon: "🎙️" },
  { id: "tools", label: "Tools", icon: "🛠️" },
  { id: "avatar", label: "Avatar", icon: "🌸" },
  { id: "memory", label: "Memory", icon: "📒" },
  { id: "knowledge", label: "Knowledge", icon: "📚" },
];

const MEMORY_PAGE_SIZE = 50;

export function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  const [models, setModels] = useState<string[]>([]);
  const [voices, setVoices] = useState<string[]>([]);
  const [devices, setDevices] = useState<AudioDevices>({ input: [], output: [] });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<SettingsTabId>("chat");
  const memoryView = useAssistantStore((s) => s.memoryView);
  const memoriesEnabled = useAssistantStore((s) => s.memoriesEnabled);
  const setMemoryView = useAssistantStore((s) => s.setMemoryView);
  const setMemoryPage = useAssistantStore((s) => s.setMemoryPage);
  const setMemoryKindFilter = useAssistantStore((s) => s.setMemoryKindFilter);
  const setMemoryOrder = useAssistantStore((s) => s.setMemoryOrder);
  const applyMemoryUpdated = useAssistantStore((s) => s.applyMemoryUpdated);
  const applyMemoryDeleted = useAssistantStore((s) => s.applyMemoryDeleted);
  const applyMemoryAdded = useAssistantStore((s) => s.applyMemoryAdded);
  const [memoryError, setMemoryError] = useState<string | null>(null);
  const [memoryBusy, setMemoryBusy] = useState(false);
  const [memoryEditingId, setMemoryEditingId] = useState<number | null>(null);
  const [memoryDraft, setMemoryDraft] = useState<{
    content: string;
    kind: string;
    salience: number;
  }>({ content: "", kind: "fact", salience: 0.5 });
  const [memoryNewOpen, setMemoryNewOpen] = useState(false);
  const [memoryNewDraft, setMemoryNewDraft] = useState<{
    content: string;
    kind: string;
    salience: number;
  }>({ content: "", kind: "fact", salience: 0.6 });

  const avatar = useAssistantStore((s) => s.avatar);
  const setAvatar = useAssistantStore((s) => s.setAvatar);
  const setAvatarSettings = useAssistantStore((s) => s.setAvatarSettings);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [avatarError, setAvatarError] = useState<string | null>(null);

  const personaWindow = useAssistantStore(
    (s) => s.desktop?.persona_window ?? null,
  );
  const setPersonaWindow = useAssistantStore((s) => s.setPersonaWindow);
  const [personaBusy, setPersonaBusy] = useState(false);
  const [personaError, setPersonaError] = useState<string | null>(null);
  const tauri = isTauri();

  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [documentsBusy, setDocumentsBusy] = useState(false);
  const [documentsError, setDocumentsError] = useState<string | null>(null);
  const [documentsLoaded, setDocumentsLoaded] = useState(false);
  const documentFileRef = useRef<HTMLInputElement | null>(null);

  const [metrics, setMetricsResp] = useState<MetricsResponse | null>(null);
  const liveMetrics = useAssistantStore((s) => s.metrics);

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

  const refreshMemories = useCallback(
    async (overrides?: {
      page?: number;
      kindFilter?: string | null;
      order?: MemoryOrder;
    }) => {
      const page = overrides?.page ?? memoryView.page;
      const kindFilter =
        overrides?.kindFilter !== undefined
          ? overrides.kindFilter
          : memoryView.kindFilter;
      const order = overrides?.order ?? memoryView.order;
      setMemoryBusy(true);
      setMemoryError(null);
      try {
        const data = await api.listMemories({
          limit: MEMORY_PAGE_SIZE,
          offset: page * MEMORY_PAGE_SIZE,
          order,
          kind: kindFilter,
        });
        setMemoryView({
          items: data.memories,
          total: data.total,
          cap: data.cap,
          enabled: data.enabled,
          page,
          pageSize: MEMORY_PAGE_SIZE,
          kindFilter,
          order,
        });
      } catch (err) {
        setMemoryError(String(err));
      } finally {
        setMemoryBusy(false);
      }
    },
    [
      memoryView.page,
      memoryView.kindFilter,
      memoryView.order,
      setMemoryView,
    ],
  );

  useEffect(() => {
    if (open) {
      void refreshAll();
    }
  }, [open, refreshAll]);

  // Refresh the memory page whenever the user opens the Memory tab or
  // changes filter / sort / page. The dependencies are explicit so a
  // stale ``refreshMemories`` closure can't fire a duplicate fetch.
  useEffect(() => {
    if (!open || activeTab !== "memory") return;
    void refreshMemories();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    open,
    activeTab,
    memoryView.page,
    memoryView.kindFilter,
    memoryView.order,
  ]);

  const onDeleteMemory = async (memory: Memory) => {
    setMemoryError(null);
    try {
      await api.deleteMemory(memory.id);
      applyMemoryDeleted(memory.id);
      // If the page just emptied (and we're not on page 0), step back
      // and re-fetch so the user lands on the now-last page instead of
      // staring at an empty list.
      const remaining = memoryView.items.length - 1;
      if (remaining <= 0 && memoryView.page > 0) {
        setMemoryPage(memoryView.page - 1);
      } else {
        // Re-fetch in place to keep the page topped up to ``pageSize``
        // when there are still rows beyond the current page.
        void refreshMemories();
      }
    } catch (err) {
      setMemoryError(String(err));
    }
  };

  const onStartEditMemory = (memory: Memory) => {
    setMemoryEditingId(memory.id);
    setMemoryDraft({
      content: memory.content,
      kind: memory.kind,
      salience: memory.salience,
    });
  };

  const onCancelEditMemory = () => {
    setMemoryEditingId(null);
  };

  const onSaveEditMemory = async (memory: Memory) => {
    setMemoryBusy(true);
    setMemoryError(null);
    try {
      const patch: {
        content?: string;
        kind?: string;
        salience?: number;
      } = {};
      const trimmed = memoryDraft.content.trim();
      if (trimmed && trimmed !== memory.content) patch.content = trimmed;
      if (memoryDraft.kind && memoryDraft.kind !== memory.kind) {
        patch.kind = memoryDraft.kind;
      }
      if (
        Number.isFinite(memoryDraft.salience) &&
        Math.abs(memoryDraft.salience - memory.salience) > 1e-4
      ) {
        patch.salience = memoryDraft.salience;
      }
      if (Object.keys(patch).length === 0) {
        setMemoryEditingId(null);
        return;
      }
      const result = await api.updateMemory(memory.id, patch);
      applyMemoryUpdated(result.memory);
      setMemoryEditingId(null);
    } catch (err) {
      setMemoryError(String(err));
    } finally {
      setMemoryBusy(false);
    }
  };

  const onPinMemory = async (memory: Memory, pinned: boolean) => {
    setMemoryError(null);
    try {
      const result = await api.pinMemory(memory.id, pinned);
      applyMemoryUpdated(result.memory);
    } catch (err) {
      setMemoryError(String(err));
    }
  };

  const onCreateMemory = async () => {
    const trimmed = memoryNewDraft.content.trim();
    if (trimmed.length < 4) {
      setMemoryError("Memory content needs at least 4 characters.");
      return;
    }
    setMemoryBusy(true);
    setMemoryError(null);
    try {
      const result = await api.createMemory({
        content: trimmed,
        kind: memoryNewDraft.kind,
        salience: memoryNewDraft.salience,
      });
      if (result.memory) {
        applyMemoryAdded(result.memory);
        setMemoryNewDraft({ content: "", kind: "fact", salience: 0.6 });
        setMemoryNewOpen(false);
        // Rerun the fetch so ``total`` and the visible page reflect
        // server-side ordering instead of the client-side prepend.
        void refreshMemories({ page: 0 });
      } else if (result.deduped_into) {
        const head = (result.deduped_into.content || "").slice(0, 80);
        setMemoryError(
          `Looks similar to memory #${result.deduped_into.id}` +
            (head ? ` ("${head}")` : "") +
            " — bumped its salience instead.",
        );
        applyMemoryUpdated(result.deduped_into);
        setMemoryNewDraft({ content: "", kind: "fact", salience: 0.6 });
        setMemoryNewOpen(false);
      }
    } catch (err) {
      setMemoryError(String(err));
    } finally {
      setMemoryBusy(false);
    }
  };

  const memoryPageCount = useMemo(() => {
    if (memoryView.pageSize <= 0) return 1;
    return Math.max(1, Math.ceil(memoryView.total / memoryView.pageSize));
  }, [memoryView.total, memoryView.pageSize]);

  const memoryRangeLabel = useMemo(() => {
    if (memoryView.total === 0) return "0 of 0";
    const start = memoryView.page * memoryView.pageSize + 1;
    const end = Math.min(
      memoryView.total,
      start + memoryView.items.length - 1,
    );
    return `${start}-${end} of ${memoryView.total}`;
  }, [
    memoryView.page,
    memoryView.pageSize,
    memoryView.items.length,
    memoryView.total,
  ]);

  const onPatchAvatarSettings = async (
    patch: Partial<AvatarSettingsKnobs>,
  ) => {
    setAvatarBusy(true);
    setAvatarError(null);
    try {
      const next = await api.patchAvatarSettings(patch);
      setAvatar(next);
    } catch (err) {
      setAvatarError(String(err));
    } finally {
      setAvatarBusy(false);
    }
  };

  const onPatchPersonaWindow = async (
    patch: Partial<PersonaWindowSettings>,
  ) => {
    setPersonaBusy(true);
    setPersonaError(null);
    try {
      const next = await api.patchPersonaWindow(patch);
      setPersonaWindow(next.persona_window);
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

  const refreshMetrics = useCallback(async () => {
    try {
      const res = await api.getMetrics();
      setMetricsResp(res);
    } catch {
      // Metrics are best-effort; backend may be mid-restart.
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void refreshMetrics();
    const id = window.setInterval(() => {
      void refreshMetrics();
    }, 5000);
    return () => window.clearInterval(id);
  }, [open, refreshMetrics]);

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
      <div className="flex h-full w-full max-w-lg flex-col border-l border-white/10 bg-[#0f0a1f] shadow-2xl">
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

        <nav
          className="flex shrink-0 gap-1 overflow-x-auto border-b border-white/5 bg-white/[0.015] px-3 py-2"
          aria-label="Settings sections"
        >
          {SETTINGS_TABS.map((tab) => {
            const isActive = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                type="button"
                onClick={() => setActiveTab(tab.id)}
                aria-pressed={isActive}
                className={`flex shrink-0 items-center gap-1.5 rounded-md px-3 py-1.5 text-xs font-medium transition ${
                  isActive
                    ? "bg-ink-500/30 text-ink-100 ring-1 ring-ink-400/50"
                    : "text-ink-100/60 hover:bg-white/5 hover:text-ink-100/90"
                }`}
              >
                <span aria-hidden="true">{tab.icon}</span>
                <span>{tab.label}</span>
              </button>
            );
          })}
        </nav>

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
              {activeTab === "chat" ? (
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

                  <DiagnosticsSection
                    metrics={metrics}
                    liveLastMetrics={liveMetrics}
                  />
                </>
              ) : null}

              {activeTab === "voice" ? (
                <>
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

              <Section title="Endpointing (when do I stop listening?)">
                <p className="text-[11px] text-ink-100/50">
                  Aiko waits for silence to know your turn is over. With
                  hesitation extension on, words like "um", "and...", or
                  "you know" reset the silence clock so you have the full
                  end-of-thought window to find the next word.
                </p>
                <Row
                  label="End-of-thought wait (s)"
                  value={
                    <input
                      type="number"
                      min={1}
                      max={5}
                      step={0.1}
                      value={
                        settings.endpointing?.turn_silence_seconds ?? 3.0
                      }
                      onChange={(e) =>
                        void apply({
                          endpointing: {
                            turn_silence_seconds: Number(e.target.value),
                          },
                        })
                      }
                      className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
                    />
                  }
                />
                <Row
                  label="Quick-close wait (s)"
                  value={
                    <input
                      type="number"
                      min={0.4}
                      max={2}
                      step={0.1}
                      value={
                        settings.endpointing?.phrase_silence_seconds ?? 1.0
                      }
                      onChange={(e) =>
                        void apply({
                          endpointing: {
                            phrase_silence_seconds: Number(e.target.value),
                          },
                        })
                      }
                      className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
                    />
                  }
                />
                <Row
                  label="Sustained-speech for barge-in (s)"
                  value={
                    <input
                      type="number"
                      min={0.2}
                      max={1.5}
                      step={0.1}
                      value={
                        settings.endpointing?.barge_in_min_speech_seconds ?? 0.7
                      }
                      onChange={(e) =>
                        void apply({
                          endpointing: {
                            barge_in_min_speech_seconds: Number(e.target.value),
                          },
                        })
                      }
                      className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100"
                    />
                  }
                />
                <label className="mt-1 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={
                      settings.endpointing?.hesitation_extend_to_turn ?? true
                    }
                    onChange={(e) =>
                      void apply({
                        endpointing: {
                          hesitation_extend_to_turn: e.target.checked,
                        },
                      })
                    }
                  />
                  Extend capture on hesitation words ("um", "and", "you
                  know"…)
                </label>
              </Section>
                </>
              ) : null}

              {activeTab === "tools" ? (
                <>
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
                </>
              ) : null}

              {activeTab === "avatar" ? (
                <>
                  <Section title="Avatar (Live2D)">
                    {avatarError ? (
                      <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                        {avatarError}
                      </div>
                    ) : null}
                    <div className="flex items-center justify-between rounded-md bg-white/[0.02] px-3 py-2 text-[11px]">
                      <span className="text-ink-100/60">Loaded</span>
                      <span className="font-mono text-ink-100/80">
                        {avatar?.loaded
                          ? `${avatar.display_name} (Cubism v${avatar.cubism_version})`
                          : "Files missing on disk"}
                      </span>
                    </div>
                    <div className="mt-2 space-y-1.5">
                      <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
                        Avatar size
                      </p>
                      <div className="flex items-center gap-3 rounded-md bg-white/[0.02] px-2 py-2">
                        <input
                          type="range"
                          min={0.5}
                          max={4}
                          step={0.05}
                          value={avatar?.settings.scale_multiplier ?? 1}
                          onChange={(e) => {
                            const v = Number(e.target.value);
                            setAvatarSettings({ scale_multiplier: v });
                          }}
                          onPointerUp={(e) =>
                            void onPatchAvatarSettings({
                              scale_multiplier: Number(
                                (e.target as HTMLInputElement).value,
                              ),
                            })
                          }
                          onKeyUp={(e) => {
                            if (
                              e.key === "ArrowLeft" ||
                              e.key === "ArrowRight" ||
                              e.key === "Home" ||
                              e.key === "End"
                            ) {
                              void onPatchAvatarSettings({
                                scale_multiplier: Number(
                                  (e.target as HTMLInputElement).value,
                                ),
                              });
                            }
                          }}
                          disabled={avatarBusy || !avatar}
                          className="flex-1 accent-ink-400"
                          aria-label="Avatar scale multiplier"
                        />
                        <span className="w-10 text-right text-[11px] tabular-nums text-ink-100/70">
                          {(avatar?.settings.scale_multiplier ?? 1).toFixed(2)}x
                        </span>
                        <button
                          type="button"
                          onClick={() =>
                            void onPatchAvatarSettings({ scale_multiplier: 1 })
                          }
                          disabled={avatarBusy || !avatar}
                          className="rounded border border-white/10 px-2 py-0.5 text-[10px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
                        >
                          Reset
                        </button>
                      </div>
                    </div>
                    <div className="mt-2 space-y-1.5">
                      <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
                        Body language intensity
                      </p>
                      <div className="flex items-center gap-3 rounded-md bg-white/[0.02] px-2 py-2">
                        <input
                          type="range"
                          min={0}
                          max={1.5}
                          step={0.05}
                          value={avatar?.settings.expressiveness ?? 1}
                          onChange={(e) => {
                            const v = Number(e.target.value);
                            setAvatarSettings({ expressiveness: v });
                          }}
                          onPointerUp={(e) =>
                            void onPatchAvatarSettings({
                              expressiveness: Number(
                                (e.target as HTMLInputElement).value,
                              ),
                            })
                          }
                          onKeyUp={(e) => {
                            if (
                              e.key === "ArrowLeft" ||
                              e.key === "ArrowRight" ||
                              e.key === "Home" ||
                              e.key === "End"
                            ) {
                              void onPatchAvatarSettings({
                                expressiveness: Number(
                                  (e.target as HTMLInputElement).value,
                                ),
                              });
                            }
                          }}
                          disabled={avatarBusy || !avatar}
                          className="flex-1 accent-ink-400"
                          aria-label="Avatar body language intensity"
                        />
                        <span className="w-10 text-right text-[11px] tabular-nums text-ink-100/70">
                          {(avatar?.settings.expressiveness ?? 1).toFixed(2)}x
                        </span>
                        <button
                          type="button"
                          onClick={() =>
                            void onPatchAvatarSettings({ expressiveness: 1 })
                          }
                          disabled={avatarBusy || !avatar}
                          className="rounded border border-white/10 px-2 py-0.5 text-[10px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
                        >
                          Reset
                        </button>
                      </div>
                      <p className="text-[10px] text-ink-100/40">
                        0 mutes mood-driven body language; 1 is the default; up to 1.5 amplifies.
                      </p>
                    </div>
                    <div className="mt-2 space-y-1.5">
                      <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
                        Outfit
                      </p>
                      <div className="flex flex-col gap-1 rounded-md bg-white/[0.02] px-3 py-2 text-[11px]">
                        {(
                          [
                            "auto",
                            "day",
                            "pajamas",
                            "pajamas_hooded",
                          ] as const
                        ).map((mode) => {
                          const supported =
                            mode === "auto" ||
                            mode === "day" ||
                            (mode === "pajamas" &&
                              (avatar?.capabilities.has_pajamas ?? false)) ||
                            (mode === "pajamas_hooded" &&
                              (avatar?.capabilities.has_pajamas_hooded ?? false));
                          // Friendlier labels for snake_case modes.
                          const label =
                            mode === "pajamas_hooded"
                              ? "Pajamas (hooded)"
                              : mode.charAt(0).toUpperCase() + mode.slice(1);
                          return (
                            <label
                              key={mode}
                              className={`flex items-center gap-2 ${
                                supported ? "text-ink-100/80" : "text-ink-100/30"
                              }`}
                            >
                              <input
                                type="radio"
                                name="auto_outfit"
                                value={mode}
                                checked={avatar?.settings.auto_outfit === mode}
                                onChange={() =>
                                  void onPatchAvatarSettings({ auto_outfit: mode })
                                }
                                disabled={avatarBusy || !avatar || !supported}
                                className="accent-ink-400"
                              />
                              <span>{label}</span>
                              {mode === "auto" ? (
                                <span className="text-ink-100/40">
                                  · circadian-driven
                                </span>
                              ) : null}
                              {(mode === "pajamas" || mode === "pajamas_hooded") &&
                              !supported ? (
                                <span className="text-ink-100/40">
                                  · not supported by current avatar
                                </span>
                              ) : null}
                            </label>
                          );
                        })}
                      </div>
                    </div>
                    {avatar?.loaded ? (
                      <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/50">
                        Capabilities:{" "}
                        {Object.entries(avatar.capabilities)
                          .filter(([, v]) => v)
                          .map(([k]) => k.replace(/^has_/, ""))
                          .join(", ") || "(none detected)"}
                      </p>
                    ) : (
                      <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/50">
                        Place the Alexia model files at{" "}
                        <code>live-2d-models/Alexia/</code>. The bundle is
                        gitignored so each developer drops their own copy in.
                      </p>
                    )}
                  </Section>

                  <Section title="Persona window (desktop)">
                    {personaError ? (
                      <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                        {personaError}
                      </div>
                    ) : null}
                    <p className="text-[11px] text-ink-100/50">
                      Floating, frameless window that shows just the avatar
                      plus a mic toggle and one-line composer. Settings persist
                      across restarts; the controls work in the browser too,
                      but the actual window only opens in the Tauri desktop
                      shell.
                    </p>
                    <div className="space-y-1.5">
                      <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
                        Width — {personaWindow?.width ?? 320}px
                      </p>
                      <input
                        type="range"
                        min={220}
                        max={800}
                        step={10}
                        value={personaWindow?.width ?? 320}
                        onChange={(event) => {
                          const v = Number(event.target.value);
                          setPersonaWindow({ width: v });
                        }}
                        onPointerUp={(event) => {
                          void onPatchPersonaWindow({
                            width: Number(
                              (event.target as HTMLInputElement).value,
                            ),
                          });
                        }}
                        onKeyUp={(event) => {
                          void onPatchPersonaWindow({
                            width: Number(
                              (event.target as HTMLInputElement).value,
                            ),
                          });
                        }}
                        disabled={personaBusy}
                        className="w-full accent-ink-400"
                        aria-label="Persona window width"
                      />
                    </div>
                    <div className="space-y-1.5">
                      <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
                        Height — {personaWindow?.height ?? 480}px
                      </p>
                      <input
                        type="range"
                        min={280}
                        max={1024}
                        step={10}
                        value={personaWindow?.height ?? 480}
                        onChange={(event) => {
                          const v = Number(event.target.value);
                          setPersonaWindow({ height: v });
                        }}
                        onPointerUp={(event) => {
                          void onPatchPersonaWindow({
                            height: Number(
                              (event.target as HTMLInputElement).value,
                            ),
                          });
                        }}
                        onKeyUp={(event) => {
                          void onPatchPersonaWindow({
                            height: Number(
                              (event.target as HTMLInputElement).value,
                            ),
                          });
                        }}
                        disabled={personaBusy}
                        className="w-full accent-ink-400"
                        aria-label="Persona window height"
                      />
                    </div>
                    <label className="flex items-center gap-2 text-[12px] text-ink-100/80">
                      <input
                        type="checkbox"
                        checked={personaWindow?.always_on_top ?? true}
                        onChange={(event) =>
                          void onPatchPersonaWindow({
                            always_on_top: event.target.checked,
                          })
                        }
                        disabled={personaBusy}
                        className="accent-ink-400"
                      />
                      Always on top
                    </label>
                    <button
                      type="button"
                      onClick={() => void desktopCommands.openPersona()}
                      disabled={!tauri}
                      title={
                        tauri
                          ? "Open the floating persona window"
                          : "Persona window is only available in the Tauri desktop shell"
                      }
                      className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-ink-100/80 hover:border-pink-400 hover:text-pink-100 disabled:cursor-not-allowed disabled:opacity-40"
                    >
                      Open persona window
                    </button>
                  </Section>
                </>
              ) : null}

              {activeTab === "knowledge" ? (
                <>
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

              <Section title="Long-term memories">
                <p className="text-[11px] text-ink-100/50">
                  Memories live in their own tab. Switch to{" "}
                  <button
                    type="button"
                    onClick={() => setActiveTab("memory")}
                    className="underline decoration-dotted underline-offset-2 hover:text-ink-100"
                  >
                    Memory
                  </button>{" "}
                  to inspect, edit, pin, or add memories.
                </p>
              </Section>
                </>
              ) : null}

              {activeTab === "memory" ? (
                <MemoryTab
                  view={memoryView}
                  enabled={memoriesEnabled}
                  busy={memoryBusy}
                  error={memoryError}
                  pageCount={memoryPageCount}
                  rangeLabel={memoryRangeLabel}
                  editingId={memoryEditingId}
                  draft={memoryDraft}
                  setDraft={setMemoryDraft}
                  newOpen={memoryNewOpen}
                  setNewOpen={setMemoryNewOpen}
                  newDraft={memoryNewDraft}
                  setNewDraft={setMemoryNewDraft}
                  onSetKindFilter={setMemoryKindFilter}
                  onSetOrder={setMemoryOrder}
                  onSetPage={setMemoryPage}
                  onRefresh={() => {
                    void refreshMemories();
                  }}
                  onStartEdit={onStartEditMemory}
                  onCancelEdit={onCancelEditMemory}
                  onSaveEdit={(memory) => {
                    void onSaveEditMemory(memory);
                  }}
                  onPin={(memory, pinned) => {
                    void onPinMemory(memory, pinned);
                  }}
                  onDelete={(memory) => {
                    void onDeleteMemory(memory);
                  }}
                  onCreate={() => {
                    void onCreateMemory();
                  }}
                />
              ) : null}
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

interface MemoryDraft {
  content: string;
  kind: string;
  salience: number;
}

interface MemoryTabProps {
  view: {
    items: Memory[];
    total: number;
    cap: number;
    page: number;
    pageSize: number;
    kindFilter: string | null;
    order: MemoryOrder;
  };
  enabled: boolean;
  busy: boolean;
  error: string | null;
  pageCount: number;
  rangeLabel: string;
  editingId: number | null;
  draft: MemoryDraft;
  setDraft: (draft: MemoryDraft) => void;
  newOpen: boolean;
  setNewOpen: (open: boolean) => void;
  newDraft: MemoryDraft;
  setNewDraft: (draft: MemoryDraft) => void;
  onSetKindFilter: (kind: string | null) => void;
  onSetOrder: (order: MemoryOrder) => void;
  onSetPage: (page: number) => void;
  onRefresh: () => void;
  onStartEdit: (memory: Memory) => void;
  onCancelEdit: () => void;
  onSaveEdit: (memory: Memory) => void;
  onPin: (memory: Memory, pinned: boolean) => void;
  onDelete: (memory: Memory) => void;
  onCreate: () => void;
}

function MemoryTab({
  view,
  enabled,
  busy,
  error,
  pageCount,
  rangeLabel,
  editingId,
  draft,
  setDraft,
  newOpen,
  setNewOpen,
  newDraft,
  setNewDraft,
  onSetKindFilter,
  onSetOrder,
  onSetPage,
  onRefresh,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onPin,
  onDelete,
  onCreate,
}: MemoryTabProps) {
  if (!enabled) {
    return (
      <Section title="Memory">
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          Long-term memory is disabled in config (memory.enabled).
        </p>
      </Section>
    );
  }

  return (
    <Section title="Memory">
      <div className="flex items-center justify-between gap-2 text-[11px] text-ink-100/50">
        <span>
          Showing {rangeLabel}
          {view.cap ? (
            <span className="text-ink-100/30"> · cap {view.cap}</span>
          ) : null}
        </span>
        <button
          type="button"
          onClick={onRefresh}
          disabled={busy}
          className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {busy ? "Loading..." : "Refresh"}
        </button>
      </div>

      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
          <span>Filter:</span>
          <select
            value={view.kindFilter ?? ""}
            onChange={(e) => onSetKindFilter(e.target.value || null)}
            className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80 focus:border-ink-400 focus:outline-none"
          >
            <option value="">all kinds</option>
            {MEMORY_KINDS.map((k) => (
              <option key={k} value={k}>
                {k}
              </option>
            ))}
          </select>
        </label>
        <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
          <span>Sort:</span>
          <select
            value={view.order}
            onChange={(e) => onSetOrder(e.target.value as MemoryOrder)}
            className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80 focus:border-ink-400 focus:outline-none"
          >
            <option value="recent">recent first</option>
            <option value="top">top salience</option>
          </select>
        </label>
        <button
          type="button"
          onClick={() => setNewOpen(!newOpen)}
          className="ml-auto rounded border border-white/10 px-2 py-1 text-[11px] text-ink-100/70 hover:border-emerald-400/60 hover:text-emerald-100"
        >
          {newOpen ? "Cancel" : "+ Add memory"}
        </button>
      </div>

      {error ? (
        <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      ) : null}

      {newOpen ? (
        <div className="space-y-2 rounded-md border border-emerald-400/30 bg-emerald-500/5 p-3">
          <textarea
            value={newDraft.content}
            onChange={(e) =>
              setNewDraft({ ...newDraft, content: e.target.value })
            }
            placeholder="What should Aiko remember?"
            rows={3}
            className="w-full resize-y rounded border border-white/10 bg-black/30 px-2 py-1.5 text-xs text-ink-100 placeholder-ink-100/30 focus:border-ink-400 focus:outline-none"
          />
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>kind:</span>
              <select
                value={newDraft.kind}
                onChange={(e) =>
                  setNewDraft({ ...newDraft, kind: e.target.value })
                }
                className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              >
                {MEMORY_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>salience {Math.round(newDraft.salience * 100)}%:</span>
              <input
                type="range"
                min={0}
                max={1}
                step={0.05}
                value={newDraft.salience}
                onChange={(e) =>
                  setNewDraft({ ...newDraft, salience: Number(e.target.value) })
                }
              />
            </label>
            <button
              type="button"
              onClick={onCreate}
              disabled={busy || newDraft.content.trim().length < 4}
              className="ml-auto rounded border border-emerald-400/40 bg-emerald-500/10 px-3 py-1 text-[11px] text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              Save
            </button>
          </div>
        </div>
      ) : null}

      {view.items.length === 0 ? (
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          {view.kindFilter
            ? `No memories with kind "${view.kindFilter}".`
            : "Nothing remembered yet. Memories are mined after a few turns of conversation, or whenever Aiko writes a private [[remember]] tag."}
        </p>
      ) : (
        <ul className="space-y-1.5">
          {view.items.map((memory) => {
            const isEditing = editingId === memory.id;
            return (
              <li
                key={memory.id}
                className={`rounded-md border px-3 py-2 text-xs ${
                  memory.pinned
                    ? "border-amber-400/40 bg-amber-500/5"
                    : "border-white/5 bg-white/[0.03]"
                }`}
              >
                {isEditing ? (
                  <div className="space-y-2">
                    <textarea
                      value={draft.content}
                      onChange={(e) =>
                        setDraft({ ...draft, content: e.target.value })
                      }
                      rows={3}
                      className="w-full resize-y rounded border border-white/10 bg-black/30 px-2 py-1.5 text-xs text-ink-100 focus:border-ink-400 focus:outline-none"
                    />
                    <div className="flex flex-wrap items-center gap-2">
                      <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                        <span>kind:</span>
                        <select
                          value={draft.kind}
                          onChange={(e) =>
                            setDraft({ ...draft, kind: e.target.value })
                          }
                          className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
                        >
                          {MEMORY_KINDS.map((k) => (
                            <option key={k} value={k}>
                              {k}
                            </option>
                          ))}
                        </select>
                      </label>
                      <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                        <span>salience {Math.round(draft.salience * 100)}%:</span>
                        <input
                          type="range"
                          min={0}
                          max={1}
                          step={0.05}
                          value={draft.salience}
                          onChange={(e) =>
                            setDraft({
                              ...draft,
                              salience: Number(e.target.value),
                            })
                          }
                        />
                      </label>
                      <div className="ml-auto flex gap-1">
                        <button
                          type="button"
                          onClick={onCancelEdit}
                          className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-white/30 hover:text-ink-100"
                        >
                          Cancel
                        </button>
                        <button
                          type="button"
                          onClick={() => onSaveEdit(memory)}
                          disabled={busy || draft.content.trim().length < 4}
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
                      <p className="break-words text-ink-100/90">{memory.content}</p>
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/40">
                        <span className="rounded bg-white/5 px-1.5 py-0.5 text-ink-100/60">
                          {memory.kind}
                        </span>
                        <span>
                          salience {(memory.salience * 100).toFixed(0)}%
                        </span>
                        {memory.use_count > 0 ? (
                          <span>used {memory.use_count}x</span>
                        ) : null}
                        {memory.pinned ? (
                          <span className="rounded bg-amber-500/20 px-1.5 py-0.5 text-amber-200">
                            pinned
                          </span>
                        ) : null}
                      </div>
                    </div>
                    <div className="flex shrink-0 flex-col gap-1">
                      <button
                        type="button"
                        onClick={() => onPin(memory, !memory.pinned)}
                        className={`rounded border px-2 py-0.5 text-[11px] ${
                          memory.pinned
                            ? "border-amber-400/60 text-amber-200 hover:bg-amber-500/10"
                            : "border-white/10 text-ink-100/60 hover:border-amber-400/60 hover:text-amber-200"
                        }`}
                        aria-label={memory.pinned ? "Unpin memory" : "Pin memory"}
                      >
                        {memory.pinned ? "unpin" : "pin"}
                      </button>
                      <button
                        type="button"
                        onClick={() => onStartEdit(memory)}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
                      >
                        edit
                      </button>
                      <button
                        type="button"
                        onClick={() => onDelete(memory)}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                        aria-label={`Forget memory ${memory.id}`}
                      >
                        forget
                      </button>
                    </div>
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}

      {pageCount > 1 ? (
        <div className="flex items-center justify-center gap-3 pt-1 text-[11px] text-ink-100/60">
          <button
            type="button"
            onClick={() => onSetPage(view.page - 1)}
            disabled={busy || view.page <= 0}
            className="rounded border border-white/10 px-2 py-0.5 text-[11px] hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Prev
          </button>
          <span className="font-mono text-ink-100/40">
            page {view.page + 1} of {pageCount}
          </span>
          <button
            type="button"
            onClick={() => onSetPage(view.page + 1)}
            disabled={busy || view.page + 1 >= pageCount}
            className="rounded border border-white/10 px-2 py-0.5 text-[11px] hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-40"
          >
            Next
          </button>
        </div>
      ) : null}
    </Section>
  );
}

interface DiagnosticsProps {
  metrics: MetricsResponse | null;
  liveLastMetrics: import("../types").MetricsSnapshot;
}

function DiagnosticsSection({ metrics, liveLastMetrics }: DiagnosticsProps) {
  // Prefer the live store metrics (back-filled with tts_ms via WS) over the
  // /api/metrics snapshot for the "last turn" rows; fall back to /api/metrics
  // if the store is empty (e.g. drawer opened pre-first-turn).
  const last =
    Object.keys(liveLastMetrics).length > 0
      ? liveLastMetrics
      : (metrics?.last ?? {});
  const avg = metrics?.average ?? {};
  const config = metrics?.config;

  const ctxWindow = config?.context_window ?? last.context_window ?? 0;
  const ctxSource = config?.context_source ?? last.context_source ?? "fallback";
  const promptTokens = last.prompt_tokens ?? 0;
  const promptPct =
    typeof last.prompt_pct === "number" && last.prompt_pct > 0
      ? last.prompt_pct
      : promptTokens && ctxWindow
        ? promptTokens / ctxWindow
        : 0;
  const fillPct = Math.min(100, Math.round(promptPct * 100));
  const sourceLabel: Record<string, string> = {
    ollama_show: "auto-detected from Ollama",
    config: "from config",
    fallback: "default fallback",
  };

  return (
    <Section title="Diagnostics">
      <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
        <div className="flex items-baseline justify-between gap-2 text-[11px]">
          <span className="font-semibold uppercase tracking-wide text-ink-100/60">
            Context fill
          </span>
          <span className="text-ink-100/50">
            {ctxWindow ? ctxWindow.toLocaleString() : "—"} tokens ·{" "}
            <span className="text-ink-100/40">
              {sourceLabel[ctxSource] ?? ctxSource}
            </span>
          </span>
        </div>
        <div className="mt-2 h-2 w-full overflow-hidden rounded-full bg-white/10">
          <div
            className={`h-full ${
              promptPct < 0.6
                ? "bg-emerald-400"
                : promptPct < 0.85
                  ? "bg-amber-400"
                  : "bg-rose-500"
            }`}
            style={{ width: `${fillPct}%` }}
          />
        </div>
        <div className="mt-1 flex justify-between text-[11px] tabular-nums text-ink-100/60">
          <span>{promptTokens.toLocaleString()} used</span>
          <span>{Math.round(promptPct * 100)}%</span>
        </div>
      </div>

      <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ink-100/60">
          Last turn
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] tabular-nums">
          <Stat label="Capture" value={fmtMs(last.capture_ms)} />
          <Stat label="STT" value={fmtMs(last.stt_ms)} />
          <Stat label="LLM" value={fmtMs(last.llm_ms)} />
          <Stat label="TTS" value={fmtMs(last.tts_ms)} />
          <Stat label="Total" value={fmtMs(last.total_ms)} />
          <Stat
            label="Tokens/sec"
            value={
              last.tokens_per_second
                ? `${last.tokens_per_second.toFixed(1)}`
                : "—"
            }
          />
          <Stat
            label="Prompt"
            value={(last.prompt_tokens ?? 0).toLocaleString()}
          />
          <Stat
            label="Completion"
            value={(last.completion_tokens ?? 0).toLocaleString()}
          />
        </div>
      </div>

      <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
        <div className="mb-2 flex items-baseline justify-between text-[11px] font-semibold uppercase tracking-wide text-ink-100/60">
          <span>Last 10 turns (avg)</span>
          {"window" in avg ? (
            <span className="text-[10px] font-normal normal-case text-ink-100/40">
              window={(avg as { window?: number }).window ?? 0}
            </span>
          ) : null}
        </div>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-[11px] tabular-nums">
          <Stat label="Capture" value={fmtMs(avg.capture_ms)} />
          <Stat label="STT" value={fmtMs(avg.stt_ms)} />
          <Stat label="LLM" value={fmtMs(avg.llm_ms)} />
          <Stat label="TTS" value={fmtMs(avg.tts_ms)} />
          <Stat label="Total" value={fmtMs(avg.total_ms)} />
          <Stat
            label="Tokens/sec"
            value={
              avg.tokens_per_second
                ? `${avg.tokens_per_second.toFixed(1)}`
                : "—"
            }
          />
          <Stat
            label="Prompt avg"
            value={
              avg.prompt_tokens
                ? Math.round(avg.prompt_tokens).toLocaleString()
                : "—"
            }
          />
          <Stat
            label="Fill avg"
            value={
              avg.prompt_pct
                ? `${Math.round(avg.prompt_pct * 100)}%`
                : "—"
            }
          />
        </div>
      </div>

      <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
        <div className="mb-2 text-[11px] font-semibold uppercase tracking-wide text-ink-100/60">
          Summary state
        </div>
        <div className="space-y-1 text-[11px]">
          <RowMini
            label="Active"
            value={last.summary_active ? "yes" : "no"}
          />
          <RowMini
            label="Messages covered"
            value={String(last.summary_messages ?? 0)}
          />
          <RowMini
            label="Compactions this session"
            value={String(last.compactions_total ?? 0)}
          />
          <RowMini
            label="Last turn compacted"
            value={last.compaction_triggered ? "yes" : "no"}
          />
          <RowMini
            label="Dropped from history"
            value={String(last.history_dropped_count ?? 0)}
          />
          {config ? (
            <>
              <RowMini
                label="Compaction threshold"
                value={`${Math.round(config.max_prompt_tokens_pct * 100)}%`}
              />
              <RowMini
                label="Summary idle"
                value={`${config.summary_idle_seconds}s`}
              />
            </>
          ) : null}
        </div>
      </div>
    </Section>
  );
}

function fmtMs(value: number | undefined): string {
  if (!value) return "—";
  if (value < 1000) return `${Math.round(value)} ms`;
  return `${(value / 1000).toFixed(2)} s`;
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-2">
      <span className="text-ink-100/55">{label}</span>
      <span className="text-ink-100/85">{value}</span>
    </div>
  );
}

function RowMini({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between text-[11px]">
      <span className="text-ink-100/55">{label}</span>
      <span className="font-mono text-ink-100/80">{value}</span>
    </div>
  );
}
