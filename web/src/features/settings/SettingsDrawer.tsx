import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api";
import { desktop as desktopCommands } from "@/desktop/commands";
import { isTauri } from "@/desktop/runtime";
import type {
  AssistantSettings,
  AvatarSettingsKnobs,
  Memory,
  MemoryOrder,
  MemoryTier,
  MetricsResponse,
  RagDocument,
  SharedMoment,
  TaskStatus,
  WorldItem,
  WorldKind,
  WorldLocation,
} from "@/types";
import { useAssistantStore } from "@/store";
import { useMemoryStore } from "@/stores/useMemoryStore";
import { useTasksStore } from "@/stores/useTasksStore";
import { useTogetherStore } from "@/stores/useTogetherStore";
import { useWorldStore } from "@/stores/useWorldStore";
import { Section } from "./SettingsSection";
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

const MEMORY_PAGE_SIZE = 50;

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
  const memoryView = useMemoryStore((s) => s.memoryView);
  const memoriesEnabled = useMemoryStore((s) => s.memoriesEnabled);
  const setMemoryView = useMemoryStore((s) => s.setMemoryView);
  const setMemoryPage = useMemoryStore((s) => s.setMemoryPage);
  const setMemoryKindFilter = useMemoryStore((s) => s.setMemoryKindFilter);
  const setMemoryTierFilter = useMemoryStore((s) => s.setMemoryTierFilter);
  const setMemoryOrder = useMemoryStore((s) => s.setMemoryOrder);
  const setMemoryCounts = useMemoryStore((s) => s.setMemoryCounts);
  const applyMemoryUpdated = useMemoryStore((s) => s.applyMemoryUpdated);
  const applyMemoryDeleted = useMemoryStore((s) => s.applyMemoryDeleted);
  const applyMemoryAdded = useMemoryStore((s) => s.applyMemoryAdded);
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

  // ── Tasks tab (chunk 14) ───────────────────────────────────────────
  const tasksView = useTasksStore((s) => s.tasksView);
  const setTasksPage = useTasksStore((s) => s.setTasksPage);
  const setTaskStatusFilter = useTasksStore((s) => s.setTaskStatusFilter);
  const setTasksLoading = useTasksStore((s) => s.setTasksLoading);
  const dismissTaskFromStrip = useTasksStore((s) => s.dismissTaskFromStrip);
  const [tasksError, setTasksError] = useState<string | null>(null);

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

  const [documents, setDocuments] = useState<RagDocument[]>([]);
  const [documentsBusy, setDocumentsBusy] = useState(false);
  const [documentsError, setDocumentsError] = useState<string | null>(null);
  const [documentsLoaded, setDocumentsLoaded] = useState(false);

  // ── World tab state ────────────────────────────────────────────────
  // Keep the world snapshot in the global store so live ``world_updated``
  // WS patches can land surgically without us refetching here.
  const world = useWorldStore((s) => s.world);
  const setWorld = useWorldStore((s) => s.setWorld);
  const [worldBusy, setWorldBusy] = useState(false);
  const [worldError, setWorldError] = useState<string | null>(null);
  const [worldGiveOpen, setWorldGiveOpen] = useState(false);
  const [worldGiveDraft, setWorldGiveDraft] = useState<{
    name: string;
    kind: WorldKind | string;
    quantity: number;
    description: string;
    location_id: number | null;
    consumable: boolean;
  }>({
    name: "",
    kind: "food",
    quantity: 1,
    description: "",
    location_id: null,
    consumable: true,
  });
  const [worldLocationsOpen, setWorldLocationsOpen] = useState(false);
  const [worldItemsOpen, setWorldItemsOpen] = useState(true);
  const [worldNewLocationOpen, setWorldNewLocationOpen] = useState(false);
  const [worldNewLocationDraft, setWorldNewLocationDraft] = useState<{
    name: string;
    description: string;
  }>({ name: "", description: "" });
  const [worldEditingItemId, setWorldEditingItemId] = useState<number | null>(
    null,
  );
  const [worldItemDraft, setWorldItemDraft] = useState<{
    name: string;
    description: string;
    kind: string;
    location_id: number | null;
    quantity: number;
  }>({
    name: "",
    description: "",
    kind: "other",
    location_id: null,
    quantity: 1,
  });
  const [worldEditingLocationId, setWorldEditingLocationId] = useState<
    number | null
  >(null);
  const [worldLocationDraft, setWorldLocationDraft] = useState<{
    name: string;
    description: string;
  }>({ name: "", description: "" });

  const refreshWorld = useCallback(async () => {
    setWorldBusy(true);
    setWorldError(null);
    try {
      const snapshot = await api.getWorld();
      setWorld(snapshot);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  }, [setWorld]);

  // ── Together tab state ────────────────────────────────────────────
  const togetherView = useTogetherStore((s) => s.togetherView);
  const setTogetherSummary = useTogetherStore((s) => s.setTogetherSummary);
  const setSharedMoments = useTogetherStore((s) => s.setSharedMoments);
  const setTogetherLoading = useTogetherStore((s) => s.setTogetherLoading);
  const setTogetherVibeFilter = useTogetherStore(
    (s) => s.setTogetherVibeFilter,
  );
  const upsertSharedMoment = useTogetherStore((s) => s.upsertSharedMoment);
  const removeSharedMoment = useTogetherStore((s) => s.removeSharedMoment);
  const [togetherError, setTogetherError] = useState<string | null>(null);
  const [editingMomentId, setEditingMomentId] = useState<number | null>(null);
  const [momentDraft, setMomentDraft] = useState<{
    summary: string;
    vibe: string;
    when: string;
  }>({ summary: "", vibe: "general", when: "" });
  const [newMomentOpen, setNewMomentOpen] = useState(false);
  const [newMomentDraft, setNewMomentDraft] = useState<{
    summary: string;
    vibe: string;
    when: string;
  }>({ summary: "", vibe: "general", when: "" });

  const refreshTogether = useCallback(async () => {
    setTogetherLoading(true);
    setTogetherError(null);
    try {
      const [summary, list] = await Promise.all([
        api.getTogether(),
        api.listSharedMoments(
          togetherView.page * togetherView.pageSize,
          togetherView.pageSize,
          togetherView.vibeFilter,
        ),
      ]);
      setTogetherSummary(summary);
      setSharedMoments(
        list.items,
        list.total,
        togetherView.page,
        togetherView.pageSize,
        togetherView.vibeFilter,
      );
    } catch (err) {
      setTogetherError(String(err));
    } finally {
      setTogetherLoading(false);
    }
  }, [
    setTogetherLoading,
    setTogetherSummary,
    setSharedMoments,
    togetherView.page,
    togetherView.pageSize,
    togetherView.vibeFilter,
  ]);

  const onCreateMoment = useCallback(async () => {
    setTogetherError(null);
    try {
      const result = await api.createSharedMoment({
        summary: newMomentDraft.summary.trim(),
        vibe: newMomentDraft.vibe,
        when: newMomentDraft.when || undefined,
      });
      if (result.moment) {
        upsertSharedMoment(result.moment);
      }
      setNewMomentOpen(false);
      setNewMomentDraft({ summary: "", vibe: "general", when: "" });
    } catch (err) {
      setTogetherError(String(err));
    }
  }, [newMomentDraft, upsertSharedMoment]);

  const onSaveMomentEdit = useCallback(async () => {
    if (editingMomentId == null) return;
    setTogetherError(null);
    try {
      const result = await api.updateSharedMoment(editingMomentId, {
        summary: momentDraft.summary.trim(),
        vibe: momentDraft.vibe,
        when: momentDraft.when || undefined,
      });
      if (result.moment) upsertSharedMoment(result.moment);
      setEditingMomentId(null);
    } catch (err) {
      setTogetherError(String(err));
    }
  }, [editingMomentId, momentDraft, upsertSharedMoment]);

  const onDeleteMoment = useCallback(
    async (moment: SharedMoment) => {
      setTogetherError(null);
      try {
        await api.deleteSharedMoment(moment.id);
        removeSharedMoment(moment.id);
      } catch (err) {
        setTogetherError(String(err));
      }
    },
    [removeSharedMoment],
  );

  const onTogglePinMoment = useCallback(
    async (moment: SharedMoment) => {
      setTogetherError(null);
      try {
        const result = await api.updateSharedMoment(moment.id, {
          pinned: !moment.pinned,
        });
        if (result.moment) upsertSharedMoment(result.moment);
      } catch (err) {
        setTogetherError(String(err));
      }
    },
    [upsertSharedMoment],
  );
  const documentFileRef = useRef<HTMLInputElement | null>(null);
  const tabsBarRef = useRef<HTMLElement | null>(null);

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

  const refreshMemories = useCallback(
    async (overrides?: {
      page?: number;
      kindFilter?: string | null;
      tierFilter?: MemoryTier | null;
      order?: MemoryOrder;
    }) => {
      const page = overrides?.page ?? memoryView.page;
      const kindFilter =
        overrides?.kindFilter !== undefined
          ? overrides.kindFilter
          : memoryView.kindFilter;
      const tierFilter =
        overrides?.tierFilter !== undefined
          ? overrides.tierFilter
          : memoryView.tierFilter;
      const order = overrides?.order ?? memoryView.order;
      setMemoryBusy(true);
      setMemoryError(null);
      try {
        const [data, counts] = await Promise.all([
          api.listMemories({
            limit: MEMORY_PAGE_SIZE,
            offset: page * MEMORY_PAGE_SIZE,
            order,
            kind: kindFilter,
            tier: tierFilter,
          }),
          // Counts fetch is independent of pagination -- always
          // shows total population per tier so the header reflects
          // the truth even while the page-1 list is filtered down.
          api.getMemoryCounts().catch(() => null),
        ]);
        setMemoryView({
          items: data.memories,
          total: data.total,
          cap: data.cap,
          enabled: data.enabled,
          page,
          pageSize: MEMORY_PAGE_SIZE,
          kindFilter,
          tierFilter,
          order,
        });
        if (counts) setMemoryCounts(counts);
      } catch (err) {
        setMemoryError(String(err));
      } finally {
        setMemoryBusy(false);
      }
    },
    [
      memoryView.page,
      memoryView.kindFilter,
      memoryView.tierFilter,
      memoryView.order,
      setMemoryView,
      setMemoryCounts,
    ],
  );

  // ── Tasks refresh (chunk 14) ─────────────────────────────────────
  //
  // Mirrors ``refreshMemories``: paginated REST fetch with an
  // optional override for page / status filter so the user actions
  // (page → / ← / filter pill click) can pass the next desired
  // value without waiting for the previous setState to flush.
  const refreshTasks = useCallback(
    async (overrides?: {
      page?: number;
      statusFilter?: TaskStatus | null;
    }) => {
      const page = overrides?.page ?? tasksView.page;
      const statusFilter =
        overrides?.statusFilter !== undefined
          ? overrides.statusFilter
          : tasksView.statusFilter;
      setTasksLoading(true);
      setTasksError(null);
      try {
        const data = await api.listTasks({
          limit: tasksView.pageSize,
          offset: page * tasksView.pageSize,
          status: statusFilter,
          rootsOnly: true,
        });
        setTasksPage({
          tasks: data.tasks,
          total: data.total,
          page,
          pageSize: tasksView.pageSize,
          enabled: data.enabled,
        });
      } catch (err) {
        setTasksError(String(err));
        setTasksLoading(false);
      }
    },
    [
      tasksView.page,
      tasksView.statusFilter,
      tasksView.pageSize,
      setTasksPage,
      setTasksLoading,
    ],
  );

  const handleTaskCancel = useCallback(
    async (taskId: number) => {
      setTasksError(null);
      try {
        await api.cancelTask(taskId);
        // The orchestrator listener will fire ``task_completed``
        // through the WS so the store updates without a refetch.
      } catch (err) {
        setTasksError(String(err));
      }
    },
    [],
  );

  const handleTaskAnswer = useCallback(
    async (taskId: number, answer: string) => {
      setTasksError(null);
      try {
        await api.answerTask(taskId, answer);
        // Server fires ``task_progress`` / ``task_completed`` once
        // the handler resumes.
      } catch (err) {
        setTasksError(String(err));
      }
    },
    [],
  );

  useEffect(() => {
    if (open) {
      void refreshAll();
    }
  }, [open, refreshAll]);

  // Refresh the tasks page whenever the user opens the Tasks tab or
  // flips the status filter / page. Same shape as the memory hook.
  useEffect(() => {
    if (!open || activeTab !== "tasks") return;
    void refreshTasks();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, activeTab, tasksView.page, tasksView.statusFilter]);

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
    memoryView.tierFilter,
    memoryView.order,
  ]);

  // Refresh the world snapshot whenever the World tab opens. After that,
  // ``world_updated`` WS patches keep the store in sync so we don't need
  // to poll.
  useEffect(() => {
    if (!open || activeTab !== "world") return;
    void refreshWorld();
  }, [open, activeTab, refreshWorld]);

  // Translate vertical mouse-wheel into horizontal scroll on the tab bar
  // so users don't have to drag the scrollbar when tabs overflow. React's
  // synthetic onWheel is passive on the root container, so preventDefault
  // there is a no-op; attach a native non-passive listener instead.
  useEffect(() => {
    if (!open) return;
    const el = tabsBarRef.current;
    if (!el) return;
    const onWheel = (e: WheelEvent) => {
      if (e.deltaY === 0) return;
      el.scrollLeft += e.deltaY;
      e.preventDefault();
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [open]);

  // Refresh the Together tab whenever it opens or the user changes the
  // vibe filter / page. WS patches handle live moments + axes between
  // refetches so we don't need to poll.
  useEffect(() => {
    if (!open || activeTab !== "together") return;
    void refreshTogether();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    open,
    activeTab,
    togetherView.page,
    togetherView.vibeFilter,
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

  const onPatchWorldState = async (patch: {
    location_id?: number | null;
    posture?: string;
    activity?: string;
    mood_note?: string;
  }) => {
    setWorldError(null);
    try {
      await api.patchWorldState(patch);
      // The WS broadcast will land via applyWorldPatch; no local mutation.
    } catch (err) {
      setWorldError(String(err));
    }
  };

  const onGiveItem = async () => {
    const trimmed = worldGiveDraft.name.trim();
    if (!trimmed) {
      setWorldError("Item name can't be empty.");
      return;
    }
    setWorldBusy(true);
    setWorldError(null);
    try {
      // Default location: first one matching slug "kitchenette" if no
      // explicit choice was made.
      let location_id = worldGiveDraft.location_id;
      if (location_id === null && world?.locations) {
        const kitchen = world.locations.find((l) => l.slug === "kitchenette");
        location_id = kitchen?.id ?? null;
      }
      await api.giveItem({
        name: trimmed,
        kind: worldGiveDraft.kind,
        description: worldGiveDraft.description.trim() || undefined,
        quantity: worldGiveDraft.quantity,
        consumable: worldGiveDraft.consumable,
        location_id,
      });
      setWorldGiveDraft({
        name: "",
        kind: "food",
        quantity: 1,
        description: "",
        location_id: null,
        consumable: true,
      });
      setWorldGiveOpen(false);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  const onAddLocation = async () => {
    const trimmed = worldNewLocationDraft.name.trim();
    if (!trimmed) {
      setWorldError("Location name can't be empty.");
      return;
    }
    setWorldBusy(true);
    setWorldError(null);
    try {
      await api.createWorldLocation({
        name: trimmed,
        description: worldNewLocationDraft.description.trim(),
      });
      setWorldNewLocationDraft({ name: "", description: "" });
      setWorldNewLocationOpen(false);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  const onSaveLocationEdit = async (loc: WorldLocation) => {
    setWorldBusy(true);
    setWorldError(null);
    try {
      const trimmedName = worldLocationDraft.name.trim();
      if (!trimmedName) throw new Error("name can't be empty");
      await api.updateWorldLocation(loc.id, {
        name: trimmedName,
        description: worldLocationDraft.description.trim(),
      });
      setWorldEditingLocationId(null);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  const onDeleteLocation = async (loc: WorldLocation) => {
    setWorldError(null);
    try {
      await api.deleteWorldLocation(loc.id);
    } catch (err) {
      setWorldError(String(err));
    }
  };

  const onSaveItemEdit = async (item: WorldItem) => {
    setWorldBusy(true);
    setWorldError(null);
    try {
      await api.updateWorldItem(item.id, {
        name: worldItemDraft.name.trim() || item.name,
        description: worldItemDraft.description.trim(),
        kind: worldItemDraft.kind,
        location_id: worldItemDraft.location_id,
        quantity: worldItemDraft.quantity,
      });
      setWorldEditingItemId(null);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
    }
  };

  const onDeleteItem = async (item: WorldItem) => {
    setWorldError(null);
    try {
      await api.deleteWorldItem(item.id);
    } catch (err) {
      setWorldError(String(err));
    }
  };

  const onConsumeItem = async (item: WorldItem) => {
    setWorldError(null);
    try {
      await api.consumeWorldItem(item.id, 1);
    } catch (err) {
      setWorldError(String(err));
    }
  };

  const onReseedWorld = async () => {
    if (
      !window.confirm(
        "Reset Aiko's room to the default layout? Everything currently in the room will be removed.",
      )
    ) {
      return;
    }
    setWorldBusy(true);
    setWorldError(null);
    try {
      const snapshot = await api.reseedWorld(true);
      setWorld(snapshot);
    } catch (err) {
      setWorldError(String(err));
    } finally {
      setWorldBusy(false);
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

        <nav
          ref={tabsBarRef}
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
