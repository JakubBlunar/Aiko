import { useCallback, useEffect, useState } from "react";
import { api } from "@/api";
import { desktop as desktopCommands } from "@/desktop/commands";
import { isTauri } from "@/desktop/runtime";
import type {
  AssistantSettings,
  AvatarSettingsKnobs,
  MetricsResponse,
} from "@/types";
import { TabStrip } from "@/components/TabStrip";
import { useAssistantStore } from "@/store";
import { IdentitySection } from "./IdentitySection";
import { ChatProviderSection } from "./ChatProviderSection";
import { LlmProvidersListSection } from "./LlmProvidersListSection";
import { LlmRoutesSection } from "./LlmRoutesSection";
import { VoiceTab } from "./VoiceTab";
import { AvatarTab } from "./AvatarTab";
import { DiagnosticsSection } from "./DiagnosticsSection";
import { MemoryTab } from "./MemoryTab";
import { DiaryTab } from "./DiaryTab";
import { WorldTab } from "./WorldTab";
import { TogetherTab } from "./TogetherTab";
import { TasksTab } from "./TasksTab";
import { ToolsTab } from "./ToolsTab";
import { KnowledgeTab } from "./KnowledgeTab";
import { useDocumentsController } from "./hooks/useDocumentsController";
import { useMemoryController } from "./hooks/useMemoryController";
import { useTasksController } from "./hooks/useTasksController";
import { useTogetherController } from "./hooks/useTogetherController";
import { useWorldController } from "./hooks/useWorldController";

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
  | "diary"
  | "world"
  | "together"
  | "knowledge"
  | "tasks";

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
  { id: "diary", label: "Diary", icon: "📓" },
  { id: "world", label: "World", icon: "🏠" },
  { id: "together", label: "Together", icon: "💞" },
  { id: "knowledge", label: "Knowledge", icon: "📚" },
  { id: "tasks", label: "Tasks", icon: "🧰" },
];

export function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  const [voices, setVoices] = useState<string[]>([]);
  const [deviceLists, setDeviceLists] = useState<{
    inputs: { deviceId: string; label: string; groupId: string }[];
    outputs: { deviceId: string; label: string; groupId: string }[];
  }>({ inputs: [], outputs: [] });
  const [inputDeviceId, setInputDeviceId] = useState<string>("");
  const [outputDeviceId, setOutputDeviceId] = useState<string>("");
  const [micPermission, setMicPermission] = useState<
    "granted" | "denied" | "prompt" | "unknown"
  >("unknown");
  const [dspPrefs, setDspPrefs] = useState<{
    echoCancellation: boolean;
    noiseSuppression: boolean;
    autoGainControl: boolean;
  }>({
    echoCancellation: true,
    noiseSuppression: true,
    autoGainControl: true,
  });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [activeTab, setActiveTab] = useState<SettingsTabId>("chat");

  // ── Memory tab ────────────────────────────────────────────────────
  const {
    memoryView,
    memoriesEnabled,
    memoryBusy,
    memoryError,
    memoryPageCount,
    memoryRangeLabel,
    memoryEditingId,
    memoryDraft,
    setMemoryDraft,
    memoryNewOpen,
    setMemoryNewOpen,
    memoryNewDraft,
    setMemoryNewDraft,
    setMemoryKindFilter,
    setMemoryTierFilter,
    setMemoryOrder,
    setMemoryPage,
    refreshMemories,
    onStartEditMemory,
    onCancelEditMemory,
    onSaveEditMemory,
    onPinMemory,
    onDeleteMemory,
    onCreateMemory,
  } = useMemoryController(open, activeTab);

  // ── Tasks tab ─────────────────────────────────────────────────────
  const {
    tasksView,
    tasksError,
    setTasksPage,
    setTaskStatusFilter,
    dismissTaskFromStrip,
    refreshTasks,
    handleTaskCancel,
    handleTaskAnswer,
  } = useTasksController(open, activeTab);

  const avatar = useAssistantStore((s) => s.avatar);
  const setAvatar = useAssistantStore((s) => s.setAvatar);
  const setAvatarSettings = useAssistantStore((s) => s.setAvatarSettings);
  const [avatarBusy, setAvatarBusy] = useState(false);
  const [avatarError, setAvatarError] = useState<string | null>(null);

  const personaAlwaysOnTop = useAssistantStore((s) => s.personaAlwaysOnTop);
  const setPersonaAlwaysOnTop = useAssistantStore(
    (s) => s.setPersonaAlwaysOnTop,
  );
  const [personaError, setPersonaError] = useState<string | null>(null);
  const tauri = isTauri();

  // Activity awareness store wiring. Mirrors the toggle into the
  // global store so the activity reporter hook in App.tsx
  // starts/stops its polling loop in lockstep with the checkbox.
  const setActivityAwarenessEnabled = useAssistantStore(
    (s) => s.setActivityAwarenessEnabled,
  );
  const liveActiveApp = useAssistantStore((s) => s.liveActiveApp);

  // ── Knowledge tab (RAG documents) ─────────────────────────────────
  const {
    documents,
    documentsBusy,
    documentsError,
    documentFileRef,
    onUploadDocument,
    onDeleteDocument,
  } = useDocumentsController(open);

  // ── World tab ─────────────────────────────────────────────────────
  const {
    world,
    worldBusy,
    worldError,
    refreshWorld,
    onPatchWorldState,
    worldGiveOpen,
    setWorldGiveOpen,
    worldGiveDraft,
    setWorldGiveDraft,
    onGiveItem,
    worldLocationsOpen,
    setWorldLocationsOpen,
    worldItemsOpen,
    setWorldItemsOpen,
    worldNewLocationOpen,
    setWorldNewLocationOpen,
    worldNewLocationDraft,
    setWorldNewLocationDraft,
    onAddLocation,
    worldEditingItemId,
    setWorldEditingItemId,
    worldItemDraft,
    setWorldItemDraft,
    onSaveItemEdit,
    onDeleteItem,
    onConsumeItem,
    worldEditingLocationId,
    setWorldEditingLocationId,
    worldLocationDraft,
    setWorldLocationDraft,
    onSaveLocationEdit,
    onDeleteLocation,
    onReseedWorld,
  } = useWorldController(open, activeTab);

  // ── Together tab ──────────────────────────────────────────────────
  const {
    togetherView,
    togetherError,
    setTogetherVibeFilter,
    setSharedMoments,
    refreshTogether,
    editingMomentId,
    setEditingMomentId,
    momentDraft,
    setMomentDraft,
    newMomentOpen,
    setNewMomentOpen,
    newMomentDraft,
    setNewMomentDraft,
    onCreateMoment,
    onSaveMomentEdit,
    onDeleteMoment,
    onTogglePinMoment,
  } = useTogetherController(open, activeTab);

  const [metrics, setMetricsResp] = useState<MetricsResponse | null>(null);
  const liveMetrics = useAssistantStore((s) => s.metrics);

  const refreshAll = useCallback(async () => {
    setBusy(true);
    setError(null);
    try {
      const [s, v] = await Promise.all([
        api.getSettings(),
        api.listVoices().catch(() => []),
      ]);
      setSettings(s);
      // Keep the activity-awareness toggle in sync with the store so
      // App.tsx's ``useActivityReporter`` reflects whatever the
      // backend reports (handles the case where ``user.json`` has the
      // toggle persisted from a previous session).
      setActivityAwarenessEnabled(
        Boolean(s.activity?.awareness_enabled),
      );
      setVoices(v);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  }, [setActivityAwarenessEnabled]);

  useEffect(() => {
    if (open) {
      void refreshAll();
    }
  }, [open, refreshAll]);

  // Hydrate the client-side audio device pickers + DSP toggles from
  // localStorage and the browser's device enumeration API. Devices
  // only return useful labels after the user has granted microphone
  // permission, so the UI degrades gracefully (we show "Microphone N"
  // stubs and prompt the user to enable permission before saving a
  // preference).
  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    const dm = import("@/audio/DeviceManager");
    void dm.then(async (mod) => {
      if (cancelled) return;
      setInputDeviceId(mod.getStoredInputDeviceId());
      setOutputDeviceId(mod.getStoredOutputDeviceId());
      setDspPrefs(mod.getStoredDspPreferences());
      const permission = await mod.queryMicPermission();
      if (cancelled) return;
      setMicPermission(permission);
      const lists = await mod.listDevices();
      if (cancelled) return;
      setDeviceLists(lists);
      const unsub = mod.onDeviceListChange(async () => {
        const next = await mod.listDevices();
        if (!cancelled) setDeviceLists(next);
      });
      return () => unsub();
    });
    return () => {
      cancelled = true;
    };
  }, [open]);

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

  const onPatchPersonaWindow = async (always_on_top: boolean) => {
    setPersonaError(null);
    setPersonaAlwaysOnTop(always_on_top);
    try {
      // Tauri-only: the floating window doesn't exist in the
      // browser, so we silently no-op there. The toggle is still
      // recorded in localStorage so the next desktop launch picks
      // it up.
      const result = await desktopCommands.setPersonaAlwaysOnTop(
        always_on_top,
      );
      if (result === null && tauri) {
        // ``null`` means the bridge declined (window missing or
        // capability denied). Surface a hint so the user isn't
        // confused by a checkbox that "did nothing".
        setPersonaError(
          "Could not toggle always-on-top. Try opening the persona window first.",
        );
      }
    } catch (err) {
      setPersonaError(String(err));
    }
  };

  const onResetPersonaWindow = async () => {
    setPersonaError(null);
    try {
      const result = await desktopCommands.resetPersonaWindowPosition();
      if (result === null && tauri) {
        setPersonaError(
          "Could not reset window. Open the persona window first.",
        );
      }
    } catch (err) {
      setPersonaError(String(err));
    }
  };

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

  const apply = async (patch: Record<string, unknown>) => {
    setBusy(true);
    setError(null);
    try {
      const next = await api.patchSettings(patch);
      setSettings(next);
      setActivityAwarenessEnabled(
        Boolean(next.activity?.awareness_enabled),
      );
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
      <div className="flex h-full w-full max-w-2xl lg:max-w-3xl flex-col border-l border-white/10 bg-[#0f0a1f] shadow-2xl">
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

        <TabStrip
          tabs={SETTINGS_TABS}
          activeId={activeTab}
          onSelect={setActiveTab}
          ariaLabel="Settings sections"
        />

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
                  <IdentitySection />

                  <ChatProviderSection
                    settings={settings}
                    apply={apply}
                    onSettingsChanged={refreshAll}
                  />

                  <LlmProvidersListSection />

                  <LlmRoutesSection />

                  <DiagnosticsSection
                    metrics={metrics}
                    liveLastMetrics={liveMetrics}
                    onApplyPatch={apply}
                    busy={busy}
                  />
                </>
              ) : null}

              {activeTab === "voice" ? (
                <VoiceTab
                  settings={settings}
                  voices={voices}
                  deviceLists={deviceLists}
                  setDeviceLists={setDeviceLists}
                  inputDeviceId={inputDeviceId}
                  setInputDeviceId={setInputDeviceId}
                  outputDeviceId={outputDeviceId}
                  setOutputDeviceId={setOutputDeviceId}
                  micPermission={micPermission}
                  setMicPermission={setMicPermission}
                  dspPrefs={dspPrefs}
                  setDspPrefs={setDspPrefs}
                  tauri={tauri}
                  liveActiveApp={liveActiveApp}
                  apply={apply}
                />
              ) : null}

              {activeTab === "tools" ? (
                <ToolsTab settings={settings} apply={apply} />
              ) : null}

              {activeTab === "avatar" ? (
                <AvatarTab
                  avatar={avatar}
                  setAvatarSettings={setAvatarSettings}
                  avatarBusy={avatarBusy}
                  avatarError={avatarError}
                  onPatchAvatarSettings={onPatchAvatarSettings}
                  personaAlwaysOnTop={personaAlwaysOnTop}
                  personaError={personaError}
                  onPatchPersonaWindow={onPatchPersonaWindow}
                  onResetPersonaWindow={onResetPersonaWindow}
                  tauri={tauri}
                  companion={settings.companion ?? null}
                  onPatchCompanion={(patch) => {
                    void apply({ companion: patch });
                  }}
                />
              ) : null}

              {activeTab === "knowledge" ? (
                <KnowledgeTab
                  documents={documents}
                  documentsError={documentsError}
                  documentsBusy={documentsBusy}
                  documentFileRef={documentFileRef}
                  onUploadDocument={(f) => void onUploadDocument(f)}
                  onDeleteDocument={(id) => void onDeleteDocument(id)}
                  onGoToMemory={() => setActiveTab("memory")}
                />
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
                  onSetTierFilter={setMemoryTierFilter}
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

              {activeTab === "diary" ? <DiaryTab /> : null}

              {activeTab === "world" ? (
                <WorldTab
                  world={world}
                  busy={worldBusy}
                  error={worldError}
                  onRefresh={() => {
                    void refreshWorld();
                  }}
                  onPatchState={(patch) => {
                    void onPatchWorldState(patch);
                  }}
                  giveOpen={worldGiveOpen}
                  setGiveOpen={setWorldGiveOpen}
                  giveDraft={worldGiveDraft}
                  setGiveDraft={setWorldGiveDraft}
                  onGiveItem={() => {
                    void onGiveItem();
                  }}
                  locationsOpen={worldLocationsOpen}
                  setLocationsOpen={setWorldLocationsOpen}
                  itemsOpen={worldItemsOpen}
                  setItemsOpen={setWorldItemsOpen}
                  newLocationOpen={worldNewLocationOpen}
                  setNewLocationOpen={setWorldNewLocationOpen}
                  newLocationDraft={worldNewLocationDraft}
                  setNewLocationDraft={setWorldNewLocationDraft}
                  onAddLocation={() => {
                    void onAddLocation();
                  }}
                  editingItemId={worldEditingItemId}
                  setEditingItemId={setWorldEditingItemId}
                  itemDraft={worldItemDraft}
                  setItemDraft={setWorldItemDraft}
                  onSaveItemEdit={(item) => {
                    void onSaveItemEdit(item);
                  }}
                  onDeleteItem={(item) => {
                    void onDeleteItem(item);
                  }}
                  onConsumeItem={(item) => {
                    void onConsumeItem(item);
                  }}
                  editingLocationId={worldEditingLocationId}
                  setEditingLocationId={setWorldEditingLocationId}
                  locationDraft={worldLocationDraft}
                  setLocationDraft={setWorldLocationDraft}
                  onSaveLocationEdit={(loc) => {
                    void onSaveLocationEdit(loc);
                  }}
                  onDeleteLocation={(loc) => {
                    void onDeleteLocation(loc);
                  }}
                  onReseedWorld={() => {
                    void onReseedWorld();
                  }}
                  companion={settings.companion ?? null}
                  onPatchCompanion={(patch) => {
                    void apply({ companion: patch });
                  }}
                />
              ) : null}

              {activeTab === "together" ? (
                <TogetherTab
                  summary={togetherView.summary}
                  moments={togetherView.moments}
                  total={togetherView.total}
                  page={togetherView.page}
                  pageSize={togetherView.pageSize}
                  vibeFilter={togetherView.vibeFilter}
                  loading={togetherView.loading}
                  error={togetherError}
                  onSetVibeFilter={setTogetherVibeFilter}
                  onSetPage={(p) =>
                    setSharedMoments(
                      togetherView.moments,
                      togetherView.total,
                      p,
                      togetherView.pageSize,
                      togetherView.vibeFilter,
                    )
                  }
                  newOpen={newMomentOpen}
                  setNewOpen={setNewMomentOpen}
                  newDraft={newMomentDraft}
                  setNewDraft={setNewMomentDraft}
                  onCreate={() => {
                    void onCreateMoment();
                  }}
                  editingId={editingMomentId}
                  setEditingId={setEditingMomentId}
                  editDraft={momentDraft}
                  setEditDraft={setMomentDraft}
                  onSaveEdit={() => {
                    void onSaveMomentEdit();
                  }}
                  onDelete={(moment) => {
                    void onDeleteMoment(moment);
                  }}
                  onTogglePin={(moment) => {
                    void onTogglePinMoment(moment);
                  }}
                  onRefresh={() => {
                    void refreshTogether();
                  }}
                />
              ) : null}

              {activeTab === "tasks" ? (
                <TasksTab
                  tasks={tasksView.historyOrder
                    .map((id) => tasksView.tasksById[id])
                    .filter((t): t is NonNullable<typeof t> => Boolean(t))}
                  total={tasksView.total}
                  page={tasksView.page}
                  pageSize={tasksView.pageSize}
                  statusFilter={tasksView.statusFilter}
                  loading={tasksView.loading}
                  enabled={tasksView.enabled}
                  error={tasksError}
                  onSetStatusFilter={(status) => {
                    setTaskStatusFilter(status);
                  }}
                  onSetPage={(p) => {
                    // Page changes go through the store so the
                    // effect-watcher re-fires the REST fetch with
                    // the new offset on the next render tick.
                    setTasksPage({
                      tasks: tasksView.historyOrder
                        .map((id) => tasksView.tasksById[id])
                        .filter(
                          (t): t is NonNullable<typeof t> => Boolean(t),
                        ),
                      total: tasksView.total,
                      page: Math.max(0, p),
                      pageSize: tasksView.pageSize,
                      enabled: tasksView.enabled,
                    });
                  }}
                  onCancel={(taskId) => {
                    // Dismiss from strip optimistically so the chip
                    // disappears even if the user is staring at the
                    // strip; the broadcast-driven completion will
                    // still drop the row through the regular path.
                    dismissTaskFromStrip(taskId);
                    void handleTaskCancel(taskId);
                  }}
                  onAnswer={(taskId, answer) => {
                    void handleTaskAnswer(taskId, answer);
                  }}
                  onRefresh={() => {
                    void refreshTasks();
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


// Back-compat re-export so the older `TogetherTab.test.tsx` import
// path keeps working unchanged after the file-size refactor.
export { TogetherTab } from "./TogetherTab";
