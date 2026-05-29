import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { api } from "../api";
import { desktop as desktopCommands } from "../desktop/commands";
import { isTauri } from "../desktop/runtime";
import type {
  AccessoryCatalogue,
  AssistantSettings,
  AvatarSettingsKnobs,
  Belief,
  BeliefKind,
  BeliefStatus,
  BeliefsResponse,
  Memory,
  MemoryConflictPair,
  MemoryConflictsResponse,
  MemoryOrder,
  MemoryTier,
  MetricsResponse,
  RagDocument,
  SharedMoment,
  TogetherSummary,
  WorldItem,
  WorldKind,
  WorldLocation,
  WorldPosture,
  WorldSnapshot,
  WorldActivity,
} from "../types";
import {
  MEMORY_KINDS,
  MEMORY_TIERS,
  SHARED_MOMENT_VIBES,
  WORLD_ACTIVITIES,
  WORLD_KINDS,
  WORLD_POSTURES,
} from "../types";
import { debugLog } from "../log";
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
  | "world"
  | "together"
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
  { id: "world", label: "World", icon: "🏠" },
  { id: "together", label: "Together", icon: "💞" },
  { id: "knowledge", label: "Knowledge", icon: "📚" },
];

const MEMORY_PAGE_SIZE = 50;

export function SettingsDrawer({ open, onClose }: SettingsDrawerProps) {
  const [settings, setSettings] = useState<AssistantSettings | null>(null);
  const [models, setModels] = useState<string[]>([]);
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
  const memoryView = useAssistantStore((s) => s.memoryView);
  const memoriesEnabled = useAssistantStore((s) => s.memoriesEnabled);
  const setMemoryView = useAssistantStore((s) => s.setMemoryView);
  const setMemoryPage = useAssistantStore((s) => s.setMemoryPage);
  const setMemoryKindFilter = useAssistantStore((s) => s.setMemoryKindFilter);
  const setMemoryTierFilter = useAssistantStore((s) => s.setMemoryTierFilter);
  const setMemoryOrder = useAssistantStore((s) => s.setMemoryOrder);
  const setMemoryCounts = useAssistantStore((s) => s.setMemoryCounts);
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
  const world = useAssistantStore((s) => s.world);
  const setWorld = useAssistantStore((s) => s.setWorld);
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
  const togetherView = useAssistantStore((s) => s.togetherView);
  const setTogetherSummary = useAssistantStore((s) => s.setTogetherSummary);
  const setSharedMoments = useAssistantStore((s) => s.setSharedMoments);
  const setTogetherLoading = useAssistantStore((s) => s.setTogetherLoading);
  const setTogetherVibeFilter = useAssistantStore(
    (s) => s.setTogetherVibeFilter,
  );
  const upsertSharedMoment = useAssistantStore((s) => s.upsertSharedMoment);
  const removeSharedMoment = useAssistantStore((s) => s.removeSharedMoment);
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
      const [s, m, v] = await Promise.all([
        api.getSettings(),
        api.listModels().catch(() => []),
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
      setModels(m);
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
    const dm = import("../audio/DeviceManager");
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
                    onApplyPatch={apply}
                    busy={busy}
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
                <p className="mb-3 text-[11px] text-ink-100/50">
                  Audio capture and playback now run in this browser /
                  desktop window. Aiko's voice plays through the device
                  you pick here, and the microphone is shared across
                  all connected windows with a one-at-a-time lock
                  (whoever clicks the mic last gets the floor).
                </p>
                {micPermission !== "granted" ? (
                  <div className="mb-3 rounded-md border border-amber-300/40 bg-amber-500/10 p-3 text-[11px] text-amber-100/90">
                    <div className="font-medium">
                      Microphone permission required
                    </div>
                    <div className="mt-1 text-amber-100/70">
                      Click the mic button (or grant access here) so we
                      can list the available input devices with their
                      real names.
                    </div>
                    <button
                      type="button"
                      onClick={async () => {
                        const mod = await import("../audio/DeviceManager");
                        const ok = await mod.requestMicPermission();
                        if (ok) {
                          setMicPermission("granted");
                          setDeviceLists(await mod.listDevices());
                        } else {
                          setMicPermission("denied");
                        }
                      }}
                      className="mt-2 rounded-md border border-amber-300/60 bg-amber-500/20 px-3 py-1 text-amber-100 hover:bg-amber-500/30"
                    >
                      Grant microphone access
                    </button>
                  </div>
                ) : null}
                <label className="block text-xs text-ink-100/60">
                  Microphone
                </label>
                <select
                  value={inputDeviceId}
                  onChange={async (e) => {
                    const id = e.target.value;
                    setInputDeviceId(id);
                    const mod = await import("../audio/DeviceManager");
                    mod.setStoredInputDeviceId(id);
                  }}
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
                >
                  <option value="">System default</option>
                  {deviceLists.inputs.map((d, idx) => (
                    <option key={d.deviceId || `in-${idx}`} value={d.deviceId}>
                      {d.label || `Microphone ${idx + 1}`}
                    </option>
                  ))}
                </select>
                <label className="mt-3 block text-xs text-ink-100/60">
                  Output
                </label>
                <select
                  value={outputDeviceId}
                  onChange={async (e) => {
                    const id = e.target.value;
                    setOutputDeviceId(id);
                    const mod = await import("../audio/DeviceManager");
                    mod.setStoredOutputDeviceId(id);
                  }}
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/40 px-3 py-2 text-sm text-ink-100"
                >
                  <option value="">System default</option>
                  {deviceLists.outputs.map((d, idx) => (
                    <option key={d.deviceId || `out-${idx}`} value={d.deviceId}>
                      {d.label || `Speaker ${idx + 1}`}
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

              <Section title="Microphone DSP">
                <p className="text-[11px] text-ink-100/50">
                  Browser-level audio processing applied before frames
                  are sent to the server. Defaults match modern
                  videoconferencing clients; turn one off if your model
                  already cleans up the signal itself.
                </p>
                <label className="mt-3 flex items-center justify-between gap-3 text-xs text-ink-100/80">
                  <div>
                    <div className="font-medium">Echo cancellation</div>
                    <div className="text-[10px] text-ink-100/50">
                      Removes the assistant's voice from the
                      microphone feed when you have her on speakers.
                    </div>
                  </div>
                  <input
                    type="checkbox"
                    checked={dspPrefs.echoCancellation}
                    onChange={async (e) => {
                      const next = {
                        ...dspPrefs,
                        echoCancellation: e.target.checked,
                      };
                      setDspPrefs(next);
                      const mod = await import("../audio/DeviceManager");
                      mod.setStoredDspPreferences({
                        echoCancellation: next.echoCancellation,
                      });
                    }}
                  />
                </label>
                <label className="mt-2 flex items-center justify-between gap-3 text-xs text-ink-100/80">
                  <div>
                    <div className="font-medium">Noise suppression</div>
                    <div className="text-[10px] text-ink-100/50">
                      Cuts steady background noise (fans, traffic) at
                      the cost of softer breaths/whispers.
                    </div>
                  </div>
                  <input
                    type="checkbox"
                    checked={dspPrefs.noiseSuppression}
                    onChange={async (e) => {
                      const next = {
                        ...dspPrefs,
                        noiseSuppression: e.target.checked,
                      };
                      setDspPrefs(next);
                      const mod = await import("../audio/DeviceManager");
                      mod.setStoredDspPreferences({
                        noiseSuppression: next.noiseSuppression,
                      });
                    }}
                  />
                </label>
                <label className="mt-2 flex items-center justify-between gap-3 text-xs text-ink-100/80">
                  <div>
                    <div className="font-medium">Auto gain control</div>
                    <div className="text-[10px] text-ink-100/50">
                      Keeps your level steady as you move toward / away
                      from the mic. Disable for studio-quality input.
                    </div>
                  </div>
                  <input
                    type="checkbox"
                    checked={dspPrefs.autoGainControl}
                    onChange={async (e) => {
                      const next = {
                        ...dspPrefs,
                        autoGainControl: e.target.checked,
                      };
                      setDspPrefs(next);
                      const mod = await import("../audio/DeviceManager");
                      mod.setStoredDspPreferences({
                        autoGainControl: next.autoGainControl,
                      });
                    }}
                  />
                </label>
                <VoiceOwnerRow />
              </Section>

              <Section title="Proactive nudges">
                <p className="text-[11px] text-ink-100/50">
                  When you've been quiet, Aiko can pick up a thread on her
                  own. Voice mode and typed chat are tuned independently
                  because the right cadence is very different.
                </p>
                <p className="mt-3 text-[11px] font-semibold text-ink-100/70">
                  In voice mode
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
                <p className="mt-4 text-[11px] font-semibold text-ink-100/70">
                  In typed chat
                </p>
                <p className="text-[11px] text-ink-100/50">
                  Aiko may speak first when you've been quiet a while. Only
                  fires while the app window is in focus — backgrounded
                  tabs and alt-tabbed windows pause the timer.
                </p>
                <label className="mt-2 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={settings.proactive?.typed_enabled ?? true}
                    onChange={(e) =>
                      void apply({
                        proactive: { typed_enabled: e.target.checked },
                      })
                    }
                  />
                  Let Aiko speak first in typed chat
                </label>
                <Row
                  label="Silence threshold (s)"
                  value={
                    <input
                      type="number"
                      min={60}
                      step={10}
                      value={
                        settings.proactive?.silence_seconds_typed ?? 240
                      }
                      onChange={(e) =>
                        void apply({
                          proactive: {
                            silence_seconds_typed: Number(e.target.value),
                          },
                        })
                      }
                      disabled={
                        !(settings.proactive?.typed_enabled ?? true)
                      }
                      className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100 disabled:opacity-40"
                    />
                  }
                />
                <Row
                  label="Cooldown (s)"
                  value={
                    <input
                      type="number"
                      min={120}
                      step={30}
                      value={
                        settings.proactive?.cooldown_seconds_typed ?? 600
                      }
                      onChange={(e) =>
                        void apply({
                          proactive: {
                            cooldown_seconds_typed: Number(e.target.value),
                          },
                        })
                      }
                      disabled={
                        !(settings.proactive?.typed_enabled ?? true)
                      }
                      className="w-24 rounded border border-white/10 bg-black/40 px-2 py-1 text-right text-xs text-ink-100 disabled:opacity-40"
                    />
                  }
                />
                {/*
                 * Opt-in to "talk even when I'm not at the window".
                 * Default is OFF: typed proactive respects window
                 * visibility / focus and disarms when every Aiko
                 * window is hidden or alt-tabbed away. Flipping this
                 * on bypasses the presence gate so the silence
                 * timer fires regardless. See
                 * ``SessionController._is_typed_proactive_eligible``
                 * for the exact gate.
                 */}
                <label className="mt-2 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={settings.proactive?.typed_when_away ?? false}
                    onChange={(e) =>
                      void apply({
                        proactive: { typed_when_away: e.target.checked },
                      })
                    }
                    disabled={
                      !(settings.proactive?.typed_enabled ?? true)
                    }
                  />
                  Chime in even when I'm not at the window
                </label>
              </Section>

              <Section title="Activity awareness (desktop)">
                <p className="text-[11px] text-ink-100/50">
                  Desktop only. Aiko can see which app is in the foreground
                  (just the app name — no window titles, no URLs) and may
                  reference it when natural. Off by default. Browser tabs
                  see the toggle but it's a no-op there.
                </p>
                <label className="mt-2 flex items-center gap-2 text-xs text-ink-100/70">
                  <input
                    type="checkbox"
                    checked={
                      settings.activity?.awareness_enabled ?? false
                    }
                    onChange={(e) =>
                      void apply({
                        activity: { awareness_enabled: e.target.checked },
                      })
                    }
                  />
                  Share the foreground app name with Aiko
                </label>
                {settings.activity?.awareness_enabled ? (
                  <p className="mt-2 text-[11px] text-ink-100/60">
                    {tauri ? (
                      <>
                        Currently sees:{" "}
                        <span className="font-mono text-ink-100/80">
                          {liveActiveApp ?? "—"}
                        </span>
                      </>
                    ) : (
                      <em>
                        Browser shell — desktop app required to share
                        foreground state.
                      </em>
                    )}
                  </p>
                ) : null}
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
                    <AccessoriesSubSection avatarLoaded={!!avatar?.loaded} />
                  </Section>

                  <Section title="Persona window (desktop)">
                    {personaError ? (
                      <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
                        {personaError}
                      </div>
                    ) : null}
                    <p className="text-[11px] text-ink-100/50">
                      Floating, frameless window that shows just the avatar
                      plus a mic toggle and one-line composer. Position and
                      size are remembered automatically by the desktop
                      shell -- drag and resize the window itself instead of
                      using sliders here. The browser build ignores this
                      section entirely (no floating window exists outside
                      Tauri).
                    </p>
                    <label
                      className={`flex items-center gap-2 text-[12px] ${
                        tauri ? "text-ink-100/80" : "text-ink-100/40"
                      }`}
                      title={
                        tauri
                          ? "Keep the persona window above other apps"
                          : "Only available in the Tauri desktop shell"
                      }
                    >
                      <input
                        type="checkbox"
                        checked={personaAlwaysOnTop}
                        onChange={(event) =>
                          void onPatchPersonaWindow(event.target.checked)
                        }
                        disabled={!tauri}
                        className="accent-ink-400 disabled:cursor-not-allowed"
                      />
                      Always on top
                    </label>
                    <div className="flex flex-wrap items-center gap-2">
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
                      <button
                        type="button"
                        onClick={() => void onResetPersonaWindow()}
                        disabled={!tauri}
                        title={
                          tauri
                            ? "Snap the persona window back to the default size, centered on this monitor"
                            : "Only available in the Tauri desktop shell"
                        }
                        className="rounded-md border border-white/10 px-3 py-1.5 text-xs text-ink-100/80 hover:border-amber-400 hover:text-amber-100 disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        Reset window position
                      </button>
                    </div>
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

/**
 * Mini status pill describing the current voice ownership state.
 * "Owned by this window" / "Owned by another window" / "Idle".
 */
function VoiceOwnerRow() {
  const clientId = useAssistantStore((s) => s.clientId);
  const voiceOwnerId = useAssistantStore((s) => s.voiceOwnerId);
  const label = !voiceOwnerId
    ? "No active microphone"
    : clientId && voiceOwnerId === clientId
      ? "This window"
      : "Another window";
  const tint = !voiceOwnerId
    ? "border-white/10 bg-black/30 text-ink-100/60"
    : clientId && voiceOwnerId === clientId
      ? "border-pink-300/50 bg-pink-500/10 text-pink-100/90"
      : "border-amber-300/50 bg-amber-500/10 text-amber-100/90";
  return (
    <div
      className={`mt-3 flex items-center justify-between rounded-md border px-3 py-2 text-xs ${tint}`}
    >
      <span className="font-medium">Voice owner</span>
      <span className="text-[10px] uppercase tracking-[0.2em]">{label}</span>
    </div>
  );
}

/**
 * Phase 4 (expression overhaul): persistent accessory toggles.
 *
 * Fetches ``GET /api/avatar/accessories`` on mount and re-fetches
 * whenever the WS pushes an ``avatar_settings_changed`` event (so a
 * PATCH from another window propagates here). Each catalogue entry
 * becomes either a toggle (lollipop / eyeglasses / head_sunglasses /
 * crossed_arms) or a radio group (``eye_color``).
 *
 * Outfit gating: rows whose ``allowed_outfits`` doesn't include the
 * current ``active_outfit`` render as disabled with a hint string,
 * so the user sees *why* crossed-arms is greyed out in pajamas
 * instead of just toggling it on and seeing nothing happen.
 *
 * The component is intentionally lightweight — no error toast, no
 * busy spinner. A failed PATCH refreshes the catalogue so the UI
 * snaps back to the server's authoritative state.
 */
function AccessoriesSubSection({ avatarLoaded }: { avatarLoaded: boolean }) {
  const [catalogue, setCatalogue] = useState<AccessoryCatalogue | null>(null);
  const [busy, setBusy] = useState(false);
  const refresh = useCallback(async () => {
    if (!avatarLoaded) {
      setCatalogue(null);
      return;
    }
    try {
      const next = await api.getAvatarAccessories();
      setCatalogue(next);
    } catch {
      setCatalogue(null);
    }
  }, [avatarLoaded]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // WS bridge: re-fetch on any avatar_settings_changed broadcast so
  // a PATCH from another tab / window stays in sync.
  const lastAvatarSettings = useAssistantStore((s) => s.avatar?.settings);
  useEffect(() => {
    void refresh();
  }, [lastAvatarSettings, refresh]);

  const onPatch = async (patch: Record<string, string | boolean>) => {
    setBusy(true);
    try {
      const next = await api.patchAvatarAccessories(patch);
      setCatalogue(next);
    } catch {
      void refresh();
    } finally {
      setBusy(false);
    }
  };

  if (!avatarLoaded || !catalogue) {
    return null;
  }
  const entries = catalogue.accessories.filter((e) => e.available);
  if (entries.length === 0) {
    return null;
  }
  return (
    <div className="space-y-1.5 rounded-md border border-white/5 bg-white/[0.02] px-3 py-2">
      <p className="text-[11px] uppercase tracking-wide text-ink-100/50">
        Accessories
      </p>
      <div className="space-y-1.5">
        {entries.map((entry) => {
          const gated =
            entry.allowed_outfits.length > 0 &&
            !!catalogue.active_outfit &&
            !entry.allowed_outfits.includes(
              catalogue.active_outfit === "day" ? "day_clothes" : catalogue.active_outfit,
            );
          const disabled = busy || gated;
          if (entry.kind === "toggle") {
            return (
              <label
                key={entry.key}
                className={`flex items-center gap-2 text-xs ${
                  disabled ? "text-ink-100/30" : "text-ink-100/80"
                }`}
              >
                <input
                  type="checkbox"
                  checked={entry.value === true}
                  disabled={disabled}
                  onChange={(ev) =>
                    void onPatch({ [entry.key]: ev.currentTarget.checked })
                  }
                  className="accent-ink-400"
                />
                <span>{prettyAccessoryLabel(entry.key)}</span>
                {gated ? (
                  <span className="text-[11px] text-ink-100/40">
                    · {gateHint(entry.allowed_outfits)}
                  </span>
                ) : null}
              </label>
            );
          }
          // ``eye_color`` enum — render as a labelled radio group.
          return (
            <div key={entry.key} className="space-y-1">
              <p className="text-xs text-ink-100/80">
                {prettyAccessoryLabel(entry.key)}
              </p>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                {(entry.options ?? []).map((opt) => (
                  <label
                    key={opt}
                    className={`flex items-center gap-1.5 text-[11px] ${
                      disabled ? "text-ink-100/30" : "text-ink-100/70"
                    }`}
                  >
                    <input
                      type="radio"
                      name={`accessory-${entry.key}`}
                      value={opt}
                      checked={entry.value === opt}
                      disabled={disabled}
                      onChange={() =>
                        void onPatch({ [entry.key]: opt })
                      }
                      className="accent-ink-400"
                    />
                    <span>{prettyEnumLabel(opt)}</span>
                  </label>
                ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function prettyAccessoryLabel(key: string): string {
  switch (key) {
    case "lollipop":
      return "Lollipop";
    case "eyeglasses":
      return "Eyeglasses (face)";
    case "head_sunglasses":
      return "Sunglasses (on head)";
    case "crossed_arms":
      return "Crossed-arms pose";
    case "eye_color":
      return "Eye color";
    default:
      return key.replace(/_/g, " ");
  }
}

function prettyEnumLabel(value: string): string {
  switch (value) {
    case "default":
      return "Default";
    case "both_purple":
      return "Both purple";
    case "left_purple":
      return "Left purple";
    case "right_purple":
      return "Right purple";
    default:
      return value.replace(/_/g, " ");
  }
}

function gateHint(allowedOutfits: string[]): string {
  if (allowedOutfits.length === 0) return "";
  const pretty = allowedOutfits
    .map((o) => (o === "day_clothes" ? "day clothes" : o.replace(/_/g, " ")))
    .join(" / ");
  return `only with ${pretty}`;
}

/**
 * Lets the user rename themselves after the first-run onboarding. Reads
 * the current name from the identity store slice (which the WS hello
 * primes) and PUTs the new value back through the same identity
 * endpoint the modal uses. The ``identity_changed`` broadcast then
 * flows back via WS, so every other open window updates without a
 * reload.
 */
function IdentitySection() {
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

// Schema v9 — confidence filter for the Memory tab. Pure client-side
// filter applied to the rendered page; doesn't change the API query
// (so per-tier totals in the header stay accurate). "Conflicted" is
// derived from ``metadata.flags.conflict`` (set by F1's fact-checker
// on contradiction).
type ConfidenceBand = "all" | "high" | "medium" | "low" | "conflicted";

const CONFIDENCE_BANDS: ReadonlyArray<{ id: ConfidenceBand; label: string }> = [
  { id: "all", label: "all" },
  { id: "high", label: "high (≥0.85)" },
  { id: "medium", label: "medium (0.5–0.85)" },
  { id: "low", label: "low (<0.5)" },
  { id: "conflicted", label: "conflicted" },
];

function memoryIsConflicted(memory: Memory): boolean {
  const flags = (memory.metadata as { flags?: { conflict?: unknown } } | undefined)?.flags;
  return Boolean(flags?.conflict);
}

function memoryMatchesConfidenceBand(memory: Memory, band: ConfidenceBand): boolean {
  if (band === "all") return true;
  if (band === "conflicted") return memoryIsConflicted(memory);
  const value = typeof memory.confidence === "number" ? memory.confidence : 0.7;
  if (band === "high") return value >= 0.85;
  if (band === "medium") return value >= 0.5 && value < 0.85;
  if (band === "low") return value < 0.5;
  return true;
}

interface ConfidencePipProps {
  confidence: number | undefined;
  conflicted: boolean;
  verifiedAt?: string | null;
}

function memoryVerifiedAt(item: Memory): string | null {
  const metadata = (item.metadata ?? {}) as Record<string, unknown>;
  const value = metadata["last_verified_at"];
  return typeof value === "string" && value.length > 0 ? value : null;
}

function ConfidencePip({ confidence, conflicted, verifiedAt }: ConfidencePipProps) {
  const value = typeof confidence === "number" ? confidence : 0.7;
  const pct = Math.round(value * 100);
  if (conflicted) {
    return (
      <span
        className="rounded bg-rose-500/20 px-1.5 py-0.5 text-rose-200"
        title={`Confidence ${pct}% · F1 fact-checker flagged a conflict (metadata.flags.conflict).`}
      >
        conflict · {pct}%
      </span>
    );
  }
  let cls = "rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-200";
  let label = "high";
  if (value < 0.5) {
    cls = "rounded bg-rose-500/15 px-1.5 py-0.5 text-rose-200";
    label = "low";
  } else if (value < 0.85) {
    cls = "rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-200";
    label = "med";
  }
  // Surface the F1 verified state next to high-confidence rows so the
  // user can tell apart "Aiko just believes this" from "an outside
  // source confirmed it within the last verify pass".
  const verifiedBadge = verifiedAt && value >= 0.85 ? (
    <span
      className="ml-1 rounded bg-emerald-500/25 px-1 py-0.5 text-[10px] text-emerald-100"
      title={`Verified by F1 fact-checker at ${verifiedAt}.`}
    >
      ✓
    </span>
  ) : null;
  return (
    <span
      className={cls}
      title={
        `Confidence ${pct}%. ` +
        "<0.5 demotes the memory in RAG and tags it (uncertain) in the prompt. " +
        "F1's background fact-checker pushes this up on positive verification."
      }
    >
      {label} · {pct}%
      {verifiedBadge}
    </span>
  );
}

interface MemoryTabProps {
  view: {
    items: Memory[];
    total: number;
    cap: number;
    page: number;
    pageSize: number;
    kindFilter: string | null;
    tierFilter: MemoryTier | null;
    order: MemoryOrder;
    counts: { scratchpad: number; long_term: number; archive: number; total: number } | null;
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
  onSetTierFilter: (tier: MemoryTier | null) => void;
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
  onSetTierFilter,
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
  // Schema v9 — confidence band filter. Pure client-side post-fetch
  // filter so we don't need backend query support for it; per-tier
  // totals stay accurate because the API call is unchanged.
  const [confidenceBand, setConfidenceBand] = useState<ConfidenceBand>("all");
  const visibleItems = useMemo(
    () => view.items.filter((m) => memoryMatchesConfidenceBand(m, confidenceBand)),
    [view.items, confidenceBand],
  );
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

      {view.counts ? (
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-ink-100/55">
          <span className="text-ink-100/40">Tiers:</span>
          <span title="Probationary lane — fast decay, gets promoted on use">
            scratchpad <span className="text-ink-100/80">{view.counts.scratchpad}</span>
          </span>
          <span className="text-ink-100/30">·</span>
          <span title="Verified anchors — normal decay">
            long_term <span className="text-ink-100/80">{view.counts.long_term}</span>
          </span>
          <span className="text-ink-100/30">·</span>
          <span title="Cold history — zero decay, needs a strong match to surface">
            archive <span className="text-ink-100/80">{view.counts.archive}</span>
          </span>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center gap-2">
        <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
          <span>Kind:</span>
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
          <span>Tier:</span>
          <select
            value={view.tierFilter ?? ""}
            onChange={(e) =>
              onSetTierFilter((e.target.value || null) as MemoryTier | null)
            }
            className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80 focus:border-ink-400 focus:outline-none"
          >
            <option value="">all tiers</option>
            {MEMORY_TIERS.map((t) => (
              <option key={t} value={t}>
                {t}
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
        <label
          className="flex items-center gap-1 text-[11px] text-ink-100/60"
          title="Schema v9 confidence band. Pure client-side filter on the current page; doesn't change the per-tier counts above."
        >
          <span>Confidence:</span>
          <select
            value={confidenceBand}
            onChange={(e) => setConfidenceBand(e.target.value as ConfidenceBand)}
            className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80 focus:border-ink-400 focus:outline-none"
          >
            {CONFIDENCE_BANDS.map((b) => (
              <option key={b.id} value={b.id}>
                {b.label}
              </option>
            ))}
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

      {visibleItems.length === 0 ? (
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          {confidenceBand !== "all" && view.items.length > 0
            ? `No memories on this page match the "${confidenceBand}" confidence filter.`
            : view.kindFilter
            ? `No memories with kind "${view.kindFilter}".`
            : "Nothing remembered yet. Memories are mined after a few turns of conversation, or whenever Aiko writes a private [[remember]] tag."}
        </p>
      ) : (
        <ul className="space-y-1.5">
          {visibleItems.map((memory) => {
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
                        {memory.tier ? (
                          <span
                            className={
                              memory.tier === "scratchpad"
                                ? "rounded bg-amber-500/15 px-1.5 py-0.5 text-amber-200"
                                : memory.tier === "archive"
                                ? "rounded bg-slate-500/20 px-1.5 py-0.5 text-slate-200"
                                : "rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-200"
                            }
                            title={
                              memory.tier === "scratchpad"
                                ? "Probationary — promoted to long_term on use or revival, deleted after TTL"
                                : memory.tier === "archive"
                                ? "Cold history — zero decay, only surfaces on strong matches"
                                : "Verified anchor — normal decay"
                            }
                          >
                            {memory.tier}
                          </span>
                        ) : null}
                        <span>
                          salience {(memory.salience * 100).toFixed(0)}%
                        </span>
                        <ConfidencePip
                          confidence={memory.confidence}
                          conflicted={memoryIsConflicted(memory)}
                          verifiedAt={memoryVerifiedAt(memory)}
                        />
                        {typeof memory.revival_score === "number" && memory.revival_score > 0.05 ? (
                          <span
                            className="text-fuchsia-300/80"
                            title="Revival score: how often Aiko cites this memory in her replies. Drives a small salience rebate on every decay tick."
                          >
                            revival {(memory.revival_score * 100).toFixed(0)}%
                          </span>
                        ) : null}
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

      <KnowledgeGapsPanel />

      <MemoryConflictsPanel />

      <BeliefsPanel />

      <FactCheckerStatusFooter />
    </Section>
  );
}

// ── F2: knowledge-gap "things I'm not sure about" panel ─────────────

interface KnowledgeGapRow extends Memory {
  metadata?: {
    topic?: string;
    question?: string;
    resolved_at?: string | null;
    resolved_by_memory_id?: number | null;
    flags?: { conflict?: boolean };
    [key: string]: unknown;
  };
}

function KnowledgeGapsPanel() {
  const [gaps, setGaps] = useState<KnowledgeGapRow[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [includeResolved, setIncludeResolved] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await api.listKnowledgeGaps(includeResolved);
      setGaps((data.gaps as KnowledgeGapRow[]) || []);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [includeResolved]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onDismiss = useCallback(async (id: number) => {
    try {
      await api.deleteKnowledgeGap(id);
      setGaps((rows) => rows.filter((r) => r.id !== id));
    } catch (err) {
      setError(String(err));
    }
  }, []);

  const onResolve = useCallback(
    async (id: number) => {
      const answer = window.prompt(
        "Quick answer (optional). Leave blank to just dismiss without writing a memory:",
        "",
      );
      if (answer === null) return;
      try {
        await api.resolveKnowledgeGap(id, answer.trim() || undefined);
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Open questions Aiko emitted via [[gap:topic:question]] tags. F1's background fact-checker may resolve them automatically; otherwise dismiss or answer manually."
        >
          Things I'm not sure about
          <span className="ml-2 text-ink-100/40">({gaps.length})</span>
        </span>
        <div className="flex items-center gap-2 text-ink-100/50">
          <label className="flex items-center gap-1">
            <input
              type="checkbox"
              checked={includeResolved}
              onChange={(e) => setIncludeResolved(e.target.checked)}
            />
            <span>show resolved</span>
          </label>
          <button
            type="button"
            onClick={refresh}
            disabled={loading}
            className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
          >
            {loading ? "..." : "refresh"}
          </button>
        </div>
      </div>
      {error ? (
        <div className="rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200">
          {error}
        </div>
      ) : null}
      {gaps.length === 0 ? (
        <p className="text-[11px] text-ink-100/40">
          No open questions. Aiko will jot uncertainties here as
          [[gap:topic:question]] tags from her replies.
        </p>
      ) : (
        <ul className="space-y-1">
          {gaps.map((gap) => {
            const meta = gap.metadata || {};
            const topic = typeof meta.topic === "string" ? meta.topic : "";
            const question =
              typeof meta.question === "string"
                ? meta.question
                : (gap.content || "").trim();
            const resolved = Boolean(meta.resolved_at);
            return (
              <li
                key={gap.id}
                className={`flex items-start justify-between gap-2 rounded border px-2 py-1.5 text-[11px] ${
                  resolved
                    ? "border-emerald-400/30 bg-emerald-500/5 text-ink-100/60"
                    : "border-white/5 bg-white/[0.03]"
                }`}
              >
                <div className="min-w-0 flex-1">
                  {topic ? (
                    <span className="mr-1 inline-block rounded bg-white/10 px-1 text-ink-100/70 uppercase tracking-wide">
                      {topic}
                    </span>
                  ) : null}
                  <span className={resolved ? "line-through" : ""}>
                    {question}
                  </span>
                  {resolved ? (
                    <span className="ml-2 text-emerald-300/80">resolved</span>
                  ) : null}
                </div>
                {!resolved ? (
                  <div className="flex shrink-0 gap-1">
                    <button
                      type="button"
                      onClick={() => onResolve(gap.id)}
                      className="rounded border border-emerald-400/40 px-1.5 py-0.5 text-emerald-200 hover:bg-emerald-500/10"
                      title="Mark this gap resolved. You can optionally provide a short answer that will be written as a memory."
                    >
                      answer
                    </button>
                    <button
                      type="button"
                      onClick={() => onDismiss(gap.id)}
                      className="rounded border border-white/10 px-1.5 py-0.5 text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                    >
                      dismiss
                    </button>
                  </div>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

// ── F5: memory conflicts panel ──────────────────────────────────────

function MemoryConflictsPanel() {
  const [data, setData] = useState<MemoryConflictsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showResolved, setShowResolved] = useState(false);

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snapshot = await api.listMemoryConflicts({
        limit: 50,
        includeRecent: true,
      });
      setData(snapshot);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const onResolve = useCallback(
    async (pair: MemoryConflictPair, winnerId: number) => {
      try {
        await api.resolveMemoryConflict(pair.id, {
          winner_id: winnerId,
          action: "demote",
        });
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const onDismiss = useCallback(
    async (pair: MemoryConflictPair) => {
      try {
        await api.dismissMemoryConflict(pair.id);
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const open = data?.open ?? [];
  const resolved = data?.recently_auto_resolved ?? [];
  const counts = data?.counts ?? {
    open: 0,
    auto_resolved: 0,
    user_resolved: 0,
    dismissed: 0,
  };

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="Pairs of memories the F5 detector flagged as contradicting. Pick which side to keep -- the loser is moved to archive at low confidence so RAG stops surfacing it."
        >
          Conflicts
          <span className="ml-2 text-ink-100/40">({counts.open})</span>
        </span>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
        >
          {loading ? "..." : "refresh"}
        </button>
      </div>
      {error ? (
        <div className="rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200">
          {error}
        </div>
      ) : null}
      {open.length === 0 ? (
        <p className="text-[11px] text-ink-100/40">
          No open conflicts. Aiko's F5 detector will flag pairs here when
          two memories disagree about the same topic.
        </p>
      ) : (
        <ul className="space-y-2">
          {open.map((pair) => (
            <ConflictPairCard
              key={pair.id}
              pair={pair}
              onResolve={onResolve}
              onDismiss={onDismiss}
            />
          ))}
        </ul>
      )}
      {resolved.length > 0 ? (
        <div className="mt-2 rounded border border-white/5 bg-white/[0.02] p-2 text-[11px]">
          <button
            type="button"
            onClick={() => setShowResolved((v) => !v)}
            className="flex w-full items-center justify-between text-left text-ink-100/60 hover:text-ink-100"
          >
            <span>
              Recently auto-resolved
              <span className="ml-2 text-ink-100/40">
                ({counts.auto_resolved})
              </span>
            </span>
            <span className="text-ink-100/40">
              {showResolved ? "hide" : "show"}
            </span>
          </button>
          {showResolved ? (
            <ul className="mt-2 space-y-1">
              {resolved.map((pair) => (
                <li
                  key={pair.id}
                  className="rounded border border-white/5 bg-white/[0.02] px-2 py-1 text-ink-100/50"
                >
                  <div className="text-[10px] uppercase text-ink-100/40">
                    auto-demoted #{pair.loser_id} · kept #{pair.winner_id}
                    {" · "}sim {pair.similarity.toFixed(2)} · Δconf{" "}
                    {pair.confidence_delta.toFixed(2)}
                  </div>
                  <div className="text-ink-100/70">
                    A: {pair.memory_a?.content ?? "(deleted)"}
                  </div>
                  <div className="text-ink-100/70">
                    B: {pair.memory_b?.content ?? "(deleted)"}
                  </div>
                </li>
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}

interface ConflictPairCardProps {
  pair: MemoryConflictPair;
  onResolve: (pair: MemoryConflictPair, winnerId: number) => void | Promise<void>;
  onDismiss: (pair: MemoryConflictPair) => void | Promise<void>;
}

function ConflictPairCard({
  pair,
  onResolve,
  onDismiss,
}: ConflictPairCardProps) {
  const a = pair.memory_a;
  const b = pair.memory_b;
  return (
    <li className="rounded border border-amber-400/30 bg-amber-500/5 p-2 text-[11px]">
      <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-amber-200/80">
        <span>
          sim {pair.similarity.toFixed(2)}
        </span>
        <span>·</span>
        <span>{pair.heuristic_label}</span>
        {pair.heuristic_signals.length > 0 ? (
          <>
            <span>·</span>
            {pair.heuristic_signals.map((signal) => (
              <span
                key={signal}
                className="rounded bg-amber-500/10 px-1 py-px text-[9px] normal-case"
              >
                {signal}
              </span>
            ))}
          </>
        ) : null}
        {pair.llm_verdict ? (
          <>
            <span>·</span>
            <span>LLM: {pair.llm_verdict}</span>
          </>
        ) : null}
        {pair.flagged_by === "aiko" ? (
          <span className="rounded bg-violet-500/30 px-1 text-[9px] text-violet-100">
            aiko-flagged
          </span>
        ) : null}
      </div>
      <div className="grid gap-2 sm:grid-cols-2">
        <ConflictMemorySide
          memory={a}
          isWinner={false}
          onPick={() => onResolve(pair, pair.memory_a_id)}
        />
        <ConflictMemorySide
          memory={b}
          isWinner={false}
          onPick={() => onResolve(pair, pair.memory_b_id)}
        />
      </div>
      <div className="mt-2 flex justify-end">
        <button
          type="button"
          onClick={() => onDismiss(pair)}
          className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
          title="Mark as not actually a conflict; keep both memories untouched."
        >
          not a conflict
        </button>
      </div>
    </li>
  );
}

interface ConflictMemorySideProps {
  memory: Memory | null;
  isWinner: boolean;
  onPick: () => void | Promise<void>;
}

function ConflictMemorySide({
  memory,
  onPick,
}: ConflictMemorySideProps) {
  if (memory === null) {
    return (
      <div className="rounded border border-white/5 bg-white/[0.03] p-2 text-ink-100/50 italic">
        (memory missing)
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-1 rounded border border-white/5 bg-white/[0.03] p-2">
      <div className="text-[10px] uppercase tracking-wide text-ink-100/40">
        #{memory.id} · {memory.kind} · conf{" "}
        {memory.confidence?.toFixed(2) ?? "—"}
      </div>
      <div className="text-ink-100/90">{memory.content}</div>
      <button
        type="button"
        onClick={onPick}
        className="mt-1 self-end rounded border border-emerald-400/40 px-2 py-0.5 text-[11px] text-emerald-200 hover:bg-emerald-500/10"
        title="Keep this side; the other becomes archived at low confidence."
      >
        keep this
      </button>
    </div>
  );
}

// ── F1: background fact-checker status footer ──────────────────────

interface FactCheckerSnapshot {
  enabled: boolean;
  pending: number;
  queue_total: number;
  last_verified_at: string | null;
  hour_used: number;
  hour_cap: number;
  day_used: number;
  day_cap: number;
}

function formatRelative(iso: string | null): string {
  if (!iso) return "never";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "never";
  const delta = Math.max(0, (Date.now() - t) / 1000);
  if (delta < 60) return `${Math.round(delta)}s ago`;
  if (delta < 3600) return `${Math.round(delta / 60)}m ago`;
  if (delta < 86400) return `${Math.round(delta / 3600)}h ago`;
  return `${Math.round(delta / 86400)}d ago`;
}

// ── K2: beliefs panel ────────────────────────────────────────────────

const BELIEF_STATUS_FILTERS: { id: BeliefStatus | "all"; label: string }[] = [
  { id: "active", label: "Active" },
  { id: "contradicted", label: "Contradicted" },
  { id: "confirmed", label: "Confirmed" },
  { id: "stale", label: "Stale" },
  { id: "all", label: "All" },
];

function BeliefsPanel() {
  const [data, setData] = useState<BeliefsResponse | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<BeliefStatus | "all">("active");
  const [kindFilter, setKindFilter] = useState<BeliefKind | "all">("all");

  const refresh = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const snapshot = await api.listBeliefs({
        limit: 100,
        kind: kindFilter === "all" ? undefined : kindFilter,
        status: statusFilter === "all" ? undefined : statusFilter,
      });
      setData(snapshot);
    } catch (err) {
      setError(String(err));
    } finally {
      setLoading(false);
    }
  }, [kindFilter, statusFilter]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const handleContradict = useCallback(
    async (belief: Belief) => {
      try {
        await api.updateBelief(belief.id, { status: "contradicted" });
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const handleConfirm = useCallback(
    async (belief: Belief) => {
      try {
        await api.updateBelief(belief.id, { status: "confirmed" });
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const handleDelete = useCallback(
    async (belief: Belief) => {
      try {
        await api.deleteBelief(belief.id);
        void refresh();
      } catch (err) {
        setError(String(err));
      }
    },
    [refresh],
  );

  const beliefs = data?.beliefs ?? [];
  const counts = data?.counts;
  const enabled = data?.enabled ?? true;
  const grouped = useMemo(() => {
    const mood: Belief[] = [];
    const opinion: Belief[] = [];
    for (const b of beliefs) {
      if (b.kind === "mood") mood.push(b);
      else opinion.push(b);
    }
    return { mood, opinion };
  }, [beliefs]);

  if (!enabled) {
    return (
      <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3 text-[11px] text-ink-100/40">
        Belief tracking is disabled. Enable
        <code className="mx-1">belief_tracking_enabled</code>
        in agent settings to surface theory-of-mind beliefs here.
      </div>
    );
  }

  return (
    <div className="mt-4 space-y-2 rounded-md border border-white/5 bg-white/[0.02] p-3">
      <div className="flex items-center justify-between gap-2 text-[11px]">
        <span
          className="font-medium text-ink-100/70"
          title="What Aiko currently thinks you feel about specific topics (mood) or what you think about them (opinion). Mood beliefs flip to contradicted when the live affect read disagrees; opinion beliefs flip when your message lexically contradicts the prediction."
        >
          Beliefs
          {counts ? (
            <span className="ml-2 text-ink-100/40">
              ({counts.active} active · {counts.contradicted} contradicted)
            </span>
          ) : null}
        </span>
        <button
          type="button"
          onClick={refresh}
          disabled={loading}
          className="rounded border border-white/10 px-2 py-0.5 hover:border-ink-400 disabled:opacity-40"
        >
          {loading ? "..." : "refresh"}
        </button>
      </div>
      <div className="flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/40">
        <span>kind:</span>
        {(["all", "mood", "opinion"] as const).map((k) => (
          <button
            key={k}
            type="button"
            onClick={() => setKindFilter(k as BeliefKind | "all")}
            className={
              "rounded border px-1.5 py-0.5 " +
              (kindFilter === k
                ? "border-ink-400 bg-ink-400/10 text-ink-100"
                : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
            }
          >
            {k}
          </button>
        ))}
        <span className="ml-2">status:</span>
        {BELIEF_STATUS_FILTERS.map((opt) => (
          <button
            key={opt.id}
            type="button"
            onClick={() => setStatusFilter(opt.id)}
            className={
              "rounded border px-1.5 py-0.5 " +
              (statusFilter === opt.id
                ? "border-ink-400 bg-ink-400/10 text-ink-100"
                : "border-white/10 text-ink-100/60 hover:border-ink-400/60")
            }
          >
            {opt.label}
          </button>
        ))}
      </div>
      {error ? (
        <div className="rounded border border-rose-400/40 bg-rose-500/10 px-2 py-1 text-[11px] text-rose-200">
          {error}
        </div>
      ) : null}
      {beliefs.length === 0 ? (
        <p className="text-[11px] text-ink-100/40">
          No beliefs in this view. Aiko's K2 worker mines fresh predictions
          from recent turns; she can also tag them inline.
        </p>
      ) : (
        <div className="space-y-3">
          {grouped.mood.length > 0 ? (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-ink-100/40">
                Mood ({grouped.mood.length})
              </div>
              <ul className="space-y-1">
                {grouped.mood.map((b) => (
                  <BeliefCard
                    key={b.id}
                    belief={b}
                    onContradict={handleContradict}
                    onConfirm={handleConfirm}
                    onDelete={handleDelete}
                  />
                ))}
              </ul>
            </div>
          ) : null}
          {grouped.opinion.length > 0 ? (
            <div>
              <div className="mb-1 text-[10px] uppercase tracking-wide text-ink-100/40">
                Opinion ({grouped.opinion.length})
              </div>
              <ul className="space-y-1">
                {grouped.opinion.map((b) => (
                  <BeliefCard
                    key={b.id}
                    belief={b}
                    onContradict={handleContradict}
                    onConfirm={handleConfirm}
                    onDelete={handleDelete}
                  />
                ))}
              </ul>
            </div>
          ) : null}
        </div>
      )}
    </div>
  );
}

interface BeliefCardProps {
  belief: Belief;
  onContradict: (b: Belief) => void | Promise<void>;
  onConfirm: (b: Belief) => void | Promise<void>;
  onDelete: (b: Belief) => void | Promise<void>;
}

function BeliefCard({
  belief,
  onContradict,
  onConfirm,
  onDelete,
}: BeliefCardProps) {
  const statusTone =
    belief.status === "contradicted"
      ? "border-rose-400/30 bg-rose-500/5"
      : belief.status === "confirmed"
      ? "border-emerald-400/30 bg-emerald-500/5"
      : belief.status === "stale"
      ? "border-white/10 bg-white/[0.02] opacity-70"
      : "border-amber-400/30 bg-amber-500/5";
  const gapPing =
    belief.gap_seen_at && belief.status === "contradicted"
      ? "ring-1 ring-rose-400/40"
      : "";
  return (
    <li
      className={`rounded border p-2 text-[11px] ${statusTone} ${gapPing}`}
    >
      <div className="mb-1 flex flex-wrap items-center gap-1 text-[10px] uppercase tracking-wide text-ink-100/60">
        <span>{belief.kind}</span>
        <span>·</span>
        <span>{belief.status}</span>
        <span>·</span>
        <span>conf {belief.confidence.toFixed(2)}</span>
        <span>·</span>
        <span>source {belief.source}</span>
        <span>·</span>
        <span>{formatRelative(belief.observed_at)}</span>
      </div>
      <div className="text-ink-100/80">
        <span className="font-medium">{belief.topic}</span>
        <span className="text-ink-100/40"> — </span>
        <span>{belief.predicted_state}</span>
      </div>
      {belief.kind === "mood" && belief.valence !== null ? (
        <div className="mt-1 text-[10px] text-ink-100/50">
          predicted valence {belief.valence.toFixed(2)}
          {belief.arousal !== null
            ? ` · arousal ${belief.arousal.toFixed(2)}`
            : ""}
        </div>
      ) : null}
      {belief.gap_seen_at ? (
        <div className="mt-1 text-[10px] text-rose-200/80">
          gap seen {formatRelative(belief.gap_seen_at)}
        </div>
      ) : null}
      <div className="mt-2 flex flex-wrap items-center gap-1 text-[10px]">
        <button
          type="button"
          onClick={() => void onContradict(belief)}
          className="rounded border border-white/10 px-1.5 py-0.5 hover:border-rose-300 hover:text-rose-200"
        >
          mark contradicted
        </button>
        <button
          type="button"
          onClick={() => void onConfirm(belief)}
          className="rounded border border-white/10 px-1.5 py-0.5 hover:border-emerald-300 hover:text-emerald-200"
        >
          mark confirmed
        </button>
        <button
          type="button"
          onClick={() => void onDelete(belief)}
          className="rounded border border-white/10 px-1.5 py-0.5 hover:border-rose-400 hover:text-rose-200"
        >
          delete
        </button>
      </div>
    </li>
  );
}

function FactCheckerStatusFooter() {
  const [status, setStatus] = useState<FactCheckerSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const data = await api.factCheckerStatus();
      setStatus(data);
      setError(null);
    } catch (err) {
      setError(String(err));
    }
  }, []);

  useEffect(() => {
    void refresh();
    // Re-poll every 30 seconds while the drawer is open. Cheap (one
    // GET) and gives a live view of the queue draining.
    const t = window.setInterval(refresh, 30_000);
    return () => window.clearInterval(t);
  }, [refresh]);

  if (status === null) {
    return null;
  }
  return (
    <div
      className="mt-3 rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[11px] text-ink-100/60"
      title={
        "F1 background fact-checker: pops one claim per idle tick, " +
        "calls web_search, then distils a JSON verdict via the main chat model. " +
        "Cancels cleanly on the next user turn (the claim requeues at the front)."
      }
    >
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
        <span className={status.enabled ? "text-emerald-300/80" : "text-rose-300/80"}>
          fact-checker {status.enabled ? "on" : "off"}
        </span>
        <span
          className="rounded bg-emerald-500/10 px-1.5 py-0.5 text-emerald-200/80"
          title={
            "Privacy gate is always on. Memories containing your name, " +
            "first-person pronouns, emails, phone numbers, URLs, or " +
            "street addresses never enter the fact-check queue. " +
            "Claims with the user/assistant name embedded are redacted " +
            "before the web query is sent. See app/core/fact_check_privacy.py."
          }
        >
          private
        </span>
        <span>queue: {status.pending} pending</span>
        <span>last verified: {formatRelative(status.last_verified_at)}</span>
        <span>
          {status.hour_used}/{status.hour_cap} this hour ·{" "}
          {status.day_used}/{status.day_cap} today
        </span>
        <button
          type="button"
          onClick={refresh}
          className="ml-auto rounded border border-white/10 px-2 py-0.5 text-ink-100/50 hover:border-ink-400"
        >
          refresh
        </button>
      </div>
      {error ? (
        <div className="mt-1 text-rose-200/70">{error}</div>
      ) : null}
    </div>
  );
}

// ── World tab (Aiko's room) ─────────────────────────────────────────────

interface GiveDraft {
  name: string;
  kind: WorldKind | string;
  quantity: number;
  description: string;
  location_id: number | null;
  consumable: boolean;
}

interface ItemDraft {
  name: string;
  description: string;
  kind: string;
  location_id: number | null;
  quantity: number;
}

interface LocationDraft {
  name: string;
  description: string;
}

interface WorldTabProps {
  world: WorldSnapshot | null;
  busy: boolean;
  error: string | null;
  onRefresh: () => void;
  onPatchState: (patch: {
    location_id?: number | null;
    posture?: string;
    activity?: string;
    mood_note?: string;
  }) => void;
  giveOpen: boolean;
  setGiveOpen: (open: boolean) => void;
  giveDraft: GiveDraft;
  setGiveDraft: (draft: GiveDraft) => void;
  onGiveItem: () => void;
  locationsOpen: boolean;
  setLocationsOpen: (open: boolean) => void;
  itemsOpen: boolean;
  setItemsOpen: (open: boolean) => void;
  newLocationOpen: boolean;
  setNewLocationOpen: (open: boolean) => void;
  newLocationDraft: LocationDraft;
  setNewLocationDraft: (draft: LocationDraft) => void;
  onAddLocation: () => void;
  editingItemId: number | null;
  setEditingItemId: (id: number | null) => void;
  itemDraft: ItemDraft;
  setItemDraft: (draft: ItemDraft) => void;
  onSaveItemEdit: (item: WorldItem) => void;
  onDeleteItem: (item: WorldItem) => void;
  onConsumeItem: (item: WorldItem) => void;
  editingLocationId: number | null;
  setEditingLocationId: (id: number | null) => void;
  locationDraft: LocationDraft;
  setLocationDraft: (draft: LocationDraft) => void;
  onSaveLocationEdit: (loc: WorldLocation) => void;
  onDeleteLocation: (loc: WorldLocation) => void;
  onReseedWorld: () => void;
}

function buildQuickGivePresets(
  userDisplayName: string,
): ReadonlyArray<{ label: string; draft: GiveDraft }> {
  const giver = (userDisplayName || "").trim() || "you";
  return [
    {
      label: "🍪 Cookie",
      draft: {
        name: "cookies",
        kind: "food",
        quantity: 1,
        description: "a fresh, warm chocolate-chip cookie",
        location_id: null,
        consumable: true,
      },
    },
    {
      label: "🍵 Tea",
      draft: {
        name: "tea",
        kind: "food",
        quantity: 1,
        description: "a cup of jasmine tea",
        location_id: null,
        consumable: true,
      },
    },
    {
      label: "🧸 Plushy",
      draft: {
        name: "plushy",
        kind: "toy",
        quantity: 1,
        description: `a small soft plush, a gift from ${giver}`,
        location_id: null,
        consumable: false,
      },
    },
    {
      label: "🌷 Flower",
      draft: {
        name: "flower",
        kind: "decor",
        quantity: 1,
        description: "a single fresh flower",
        location_id: null,
        consumable: false,
      },
    },
  ];
}

function WorldTab({
  world,
  busy,
  error,
  onRefresh,
  onPatchState,
  giveOpen,
  setGiveOpen,
  giveDraft,
  setGiveDraft,
  onGiveItem,
  locationsOpen,
  setLocationsOpen,
  itemsOpen,
  setItemsOpen,
  newLocationOpen,
  setNewLocationOpen,
  newLocationDraft,
  setNewLocationDraft,
  onAddLocation,
  editingItemId,
  setEditingItemId,
  itemDraft,
  setItemDraft,
  onSaveItemEdit,
  onDeleteItem,
  onConsumeItem,
  editingLocationId,
  setEditingLocationId,
  locationDraft,
  setLocationDraft,
  onSaveLocationEdit,
  onDeleteLocation,
  onReseedWorld,
}: WorldTabProps) {
  const identity = useAssistantStore((s) => s.identity);
  const quickGivePresets = useMemo(
    () => buildQuickGivePresets(identity?.user_display_name ?? ""),
    [identity?.user_display_name],
  );
  if (!world) {
    return (
      <Section title="World">
        <p className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-xs text-ink-100/50">
          {busy ? "Loading Aiko's room..." : "World snapshot not available."}
        </p>
        {error ? (
          <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
            {error}
          </div>
        ) : null}
      </Section>
    );
  }

  const { state, locations, items } = world;
  const currentLocation =
    locations.find((l) => l.id === state.location_id) ?? null;
  const itemsByLocation = new Map<number | null, WorldItem[]>();
  for (const item of items) {
    const arr = itemsByLocation.get(item.location_id) ?? [];
    arr.push(item);
    itemsByLocation.set(item.location_id, arr);
  }
  for (const arr of itemsByLocation.values()) {
    arr.sort((a, b) => a.name.localeCompare(b.name));
  }
  const carriedItems = itemsByLocation.get(null) ?? [];

  return (
    <div className="space-y-4">
      <Section title="Right now">
        <p className="text-xs text-ink-100/70">
          Aiko is{" "}
          <span className="font-medium text-ink-100">
            {currentLocation
              ? `at ${currentLocation.name}`
              : "somewhere in her room"}
          </span>
          ,{" "}
          <span className="font-medium text-ink-100">
            {(state.posture || "sitting").replace("_", " ")}
          </span>
          ,{" "}
          <span className="font-medium text-ink-100">
            {(state.activity || "idle").replace("_", " ")}
          </span>
          .
        </p>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
            <span>Where:</span>
            <select
              value={state.location_id ?? ""}
              onChange={(e) =>
                onPatchState({
                  location_id: e.target.value ? Number(e.target.value) : null,
                })
              }
              className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
            >
              <option value="">(nowhere)</option>
              {locations.map((l) => (
                <option key={l.id} value={l.id}>
                  {l.name}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
            <span>Posture:</span>
            <select
              value={state.posture}
              onChange={(e) => onPatchState({ posture: e.target.value })}
              className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
            >
              {WORLD_POSTURES.map((p: WorldPosture) => (
                <option key={p} value={p}>
                  {p.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
            <span>Activity:</span>
            <select
              value={state.activity}
              onChange={(e) => onPatchState({ activity: e.target.value })}
              className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
            >
              {WORLD_ACTIVITIES.map((a: WorldActivity) => (
                <option key={a} value={a}>
                  {a.replace("_", " ")}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={onRefresh}
            disabled={busy}
            className="ml-auto rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100 disabled:cursor-not-allowed disabled:opacity-50"
          >
            {busy ? "..." : "Refresh"}
          </button>
        </div>
        {state.mood_note ? (
          <p className="text-[11px] italic text-ink-100/50">
            "{state.mood_note}"
          </p>
        ) : null}
      </Section>

      <Section title="Give Aiko something">
        <p className="text-[11px] text-ink-100/50">
          Drops an item into her room, attributed to you. Aiko notices on
          her next reply — no proactive ping.
        </p>
        <div className="flex flex-wrap gap-2">
          {quickGivePresets.map((preset) => (
            <button
              key={preset.label}
              type="button"
              onClick={() => {
                setGiveDraft(preset.draft);
                setGiveOpen(true);
              }}
              disabled={busy}
              className="rounded border border-emerald-400/30 bg-emerald-500/5 px-3 py-1 text-xs text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {preset.label}
            </button>
          ))}
          <button
            type="button"
            onClick={() => setGiveOpen(!giveOpen)}
            className="ml-auto rounded border border-white/10 px-3 py-1 text-xs text-ink-100/70 hover:border-emerald-400/60 hover:text-emerald-100"
          >
            {giveOpen ? "Cancel" : "Custom..."}
          </button>
        </div>
        {giveOpen ? (
          <div className="space-y-2 rounded-md border border-emerald-400/30 bg-emerald-500/5 p-3">
            <label className="block text-[11px] text-ink-100/60">
              <span>Name</span>
              <input
                value={giveDraft.name}
                onChange={(e) =>
                  setGiveDraft({ ...giveDraft, name: e.target.value })
                }
                placeholder="e.g. cookies"
                className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
              />
            </label>
            <label className="block text-[11px] text-ink-100/60">
              <span>Description (optional)</span>
              <input
                value={giveDraft.description}
                onChange={(e) =>
                  setGiveDraft({
                    ...giveDraft,
                    description: e.target.value,
                  })
                }
                placeholder="a fresh, warm chocolate-chip cookie"
                className="mt-1 w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
              />
            </label>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                <span>Kind:</span>
                <select
                  value={giveDraft.kind}
                  onChange={(e) =>
                    setGiveDraft({ ...giveDraft, kind: e.target.value })
                  }
                  className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
                >
                  {WORLD_KINDS.map((k) => (
                    <option key={k} value={k}>
                      {k}
                    </option>
                  ))}
                </select>
              </label>
              <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                <span>Quantity:</span>
                <input
                  type="number"
                  min={1}
                  max={20}
                  value={giveDraft.quantity}
                  onChange={(e) =>
                    setGiveDraft({
                      ...giveDraft,
                      quantity: Math.max(1, Number(e.target.value) || 1),
                    })
                  }
                  className="w-14 rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
                />
              </label>
              <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                <input
                  type="checkbox"
                  checked={giveDraft.consumable}
                  onChange={(e) =>
                    setGiveDraft({
                      ...giveDraft,
                      consumable: e.target.checked,
                    })
                  }
                />
                <span>Consumable</span>
              </label>
              <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
                <span>Where:</span>
                <select
                  value={giveDraft.location_id ?? ""}
                  onChange={(e) =>
                    setGiveDraft({
                      ...giveDraft,
                      location_id: e.target.value
                        ? Number(e.target.value)
                        : null,
                    })
                  }
                  className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
                >
                  <option value="">kitchenette (default)</option>
                  {locations.map((l) => (
                    <option key={l.id} value={l.id}>
                      {l.name}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            <div className="flex justify-end">
              <button
                type="button"
                onClick={onGiveItem}
                disabled={busy || !giveDraft.name.trim()}
                className="rounded border border-emerald-400/40 bg-emerald-500/10 px-3 py-1 text-[11px] text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Give
              </button>
            </div>
          </div>
        ) : null}
      </Section>

      {error ? (
        <div className="rounded-md border border-rose-400/40 bg-rose-500/10 px-3 py-2 text-xs text-rose-200">
          {error}
        </div>
      ) : null}

      <Section title="Items">
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={() => setItemsOpen(!itemsOpen)}
            className="text-[11px] text-ink-100/60 hover:text-ink-100"
          >
            {itemsOpen ? "▾ collapse" : "▸ expand"}
          </button>
          <span className="text-[11px] text-ink-100/40">
            {items.length} item{items.length === 1 ? "" : "s"}
          </span>
        </div>
        {itemsOpen ? (
          <div className="space-y-3">
            {locations.map((loc) => {
              const here = itemsByLocation.get(loc.id) ?? [];
              if (here.length === 0) return null;
              return (
                <div key={loc.id} className="space-y-1">
                  <div className="text-[10px] uppercase tracking-wide text-ink-100/40">
                    {loc.name}
                  </div>
                  <ul className="space-y-1">
                    {here.map((item) => (
                      <ItemRow
                        key={item.id}
                        item={item}
                        locations={locations}
                        editing={editingItemId === item.id}
                        draft={itemDraft}
                        setDraft={setItemDraft}
                        onStartEdit={() => {
                          setEditingItemId(item.id);
                          setItemDraft({
                            name: item.name,
                            description: item.description,
                            kind: item.kind,
                            location_id: item.location_id,
                            quantity: item.quantity,
                          });
                        }}
                        onCancelEdit={() => setEditingItemId(null)}
                        onSave={() => onSaveItemEdit(item)}
                        onDelete={() => onDeleteItem(item)}
                        onConsume={() => onConsumeItem(item)}
                        busy={busy}
                      />
                    ))}
                  </ul>
                </div>
              );
            })}
            {carriedItems.length > 0 ? (
              <div className="space-y-1">
                <div className="text-[10px] uppercase tracking-wide text-ink-100/40">
                  carrying
                </div>
                <ul className="space-y-1">
                  {carriedItems.map((item) => (
                    <ItemRow
                      key={item.id}
                      item={item}
                      locations={locations}
                      editing={editingItemId === item.id}
                      draft={itemDraft}
                      setDraft={setItemDraft}
                      onStartEdit={() => {
                        setEditingItemId(item.id);
                        setItemDraft({
                          name: item.name,
                          description: item.description,
                          kind: item.kind,
                          location_id: item.location_id,
                          quantity: item.quantity,
                        });
                      }}
                      onCancelEdit={() => setEditingItemId(null)}
                      onSave={() => onSaveItemEdit(item)}
                      onDelete={() => onDeleteItem(item)}
                      onConsume={() => onConsumeItem(item)}
                      busy={busy}
                    />
                  ))}
                </ul>
              </div>
            ) : null}
            {items.length === 0 ? (
              <p className="text-xs text-ink-100/50">
                Nothing in the room yet.
              </p>
            ) : null}
          </div>
        ) : null}
      </Section>

      <Section title="Locations">
        <div className="flex items-center justify-between">
          <button
            type="button"
            onClick={() => setLocationsOpen(!locationsOpen)}
            className="text-[11px] text-ink-100/60 hover:text-ink-100"
          >
            {locationsOpen ? "▾ collapse" : "▸ expand"}
          </button>
          <button
            type="button"
            onClick={() => setNewLocationOpen(!newLocationOpen)}
            className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/70 hover:border-emerald-400/60 hover:text-emerald-100"
          >
            {newLocationOpen ? "Cancel" : "+ Add"}
          </button>
        </div>
        {newLocationOpen ? (
          <div className="space-y-2 rounded-md border border-emerald-400/30 bg-emerald-500/5 p-3">
            <input
              value={newLocationDraft.name}
              onChange={(e) =>
                setNewLocationDraft({
                  ...newLocationDraft,
                  name: e.target.value,
                })
              }
              placeholder="Location name (e.g. 'the balcony')"
              className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
            />
            <input
              value={newLocationDraft.description}
              onChange={(e) =>
                setNewLocationDraft({
                  ...newLocationDraft,
                  description: e.target.value,
                })
              }
              placeholder="Description (optional)"
              className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
            />
            <div className="flex justify-end">
              <button
                type="button"
                onClick={onAddLocation}
                disabled={busy || !newLocationDraft.name.trim()}
                className="rounded border border-emerald-400/40 bg-emerald-500/10 px-3 py-1 text-[11px] text-emerald-100 hover:border-emerald-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                Add
              </button>
            </div>
          </div>
        ) : null}
        {locationsOpen ? (
          <ul className="space-y-1.5">
            {locations.map((loc) => (
              <li
                key={loc.id}
                className="rounded-md border border-white/5 bg-white/[0.03] px-3 py-2 text-xs"
              >
                {editingLocationId === loc.id ? (
                  <div className="space-y-2">
                    <input
                      value={locationDraft.name}
                      onChange={(e) =>
                        setLocationDraft({
                          ...locationDraft,
                          name: e.target.value,
                        })
                      }
                      className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
                    />
                    <input
                      value={locationDraft.description}
                      onChange={(e) =>
                        setLocationDraft({
                          ...locationDraft,
                          description: e.target.value,
                        })
                      }
                      className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
                    />
                    <div className="flex justify-end gap-1">
                      <button
                        type="button"
                        onClick={() => setEditingLocationId(null)}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-white/30"
                      >
                        Cancel
                      </button>
                      <button
                        type="button"
                        onClick={() => onSaveLocationEdit(loc)}
                        disabled={busy || !locationDraft.name.trim()}
                        className="rounded border border-ink-400/40 bg-ink-500/20 px-2 py-0.5 text-[11px] text-ink-100 hover:border-ink-400 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        Save
                      </button>
                    </div>
                  </div>
                ) : (
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1">
                      <div className="font-medium text-ink-100/90">
                        {loc.name}
                      </div>
                      {loc.description ? (
                        <div className="text-[11px] text-ink-100/50">
                          {loc.description}
                        </div>
                      ) : null}
                    </div>
                    <div className="flex shrink-0 gap-1">
                      <button
                        type="button"
                        onClick={() => {
                          setEditingLocationId(loc.id);
                          setLocationDraft({
                            name: loc.name,
                            description: loc.description,
                          });
                        }}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
                      >
                        edit
                      </button>
                      <button
                        type="button"
                        onClick={() => onDeleteLocation(loc)}
                        className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
                      >
                        delete
                      </button>
                    </div>
                  </div>
                )}
              </li>
            ))}
            {locations.length === 0 ? (
              <p className="text-xs text-ink-100/50">No locations yet.</p>
            ) : null}
          </ul>
        ) : null}
      </Section>

      <Section title="Reset">
        <button
          type="button"
          onClick={onReseedWorld}
          disabled={busy}
          className="rounded border border-rose-400/30 bg-rose-500/5 px-3 py-1 text-xs text-rose-200 hover:border-rose-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          Reset to default room
        </button>
        <p className="text-[10px] text-ink-100/40">
          Wipes the current room (all locations + items + state) and re-seeds
          the cozy default. Aiko's memories are not affected.
        </p>
      </Section>
    </div>
  );
}

interface ItemRowProps {
  item: WorldItem;
  locations: WorldLocation[];
  editing: boolean;
  draft: ItemDraft;
  setDraft: (draft: ItemDraft) => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSave: () => void;
  onDelete: () => void;
  onConsume: () => void;
  busy: boolean;
}

function ItemRow({
  item,
  locations,
  editing,
  draft,
  setDraft,
  onStartEdit,
  onCancelEdit,
  onSave,
  onDelete,
  onConsume,
  busy,
}: ItemRowProps) {
  return (
    <li
      className={`rounded-md border px-3 py-2 text-xs ${
        item.given_by === "user"
          ? "border-emerald-400/30 bg-emerald-500/5"
          : "border-white/5 bg-white/[0.03]"
      }`}
    >
      {editing ? (
        <div className="space-y-2">
          <input
            value={draft.name}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
          />
          <input
            value={draft.description}
            onChange={(e) =>
              setDraft({ ...draft, description: e.target.value })
            }
            placeholder="description"
            className="w-full rounded border border-white/10 bg-black/30 px-2 py-1 text-xs text-ink-100"
          />
          <div className="flex flex-wrap items-center gap-2">
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>kind:</span>
              <select
                value={draft.kind}
                onChange={(e) => setDraft({ ...draft, kind: e.target.value })}
                className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              >
                {WORLD_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {k}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>where:</span>
              <select
                value={draft.location_id ?? ""}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    location_id: e.target.value
                      ? Number(e.target.value)
                      : null,
                  })
                }
                className="rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              >
                <option value="">carried</option>
                {locations.map((l) => (
                  <option key={l.id} value={l.id}>
                    {l.name}
                  </option>
                ))}
              </select>
            </label>
            <label className="flex items-center gap-1 text-[11px] text-ink-100/60">
              <span>qty:</span>
              <input
                type="number"
                min={0}
                max={99}
                value={draft.quantity}
                onChange={(e) =>
                  setDraft({
                    ...draft,
                    quantity: Math.max(0, Number(e.target.value) || 0),
                  })
                }
                className="w-14 rounded border border-white/10 bg-black/30 px-2 py-1 text-[11px] text-ink-100/80"
              />
            </label>
            <div className="ml-auto flex gap-1">
              <button
                type="button"
                onClick={onCancelEdit}
                className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-white/30"
              >
                Cancel
              </button>
              <button
                type="button"
                onClick={onSave}
                disabled={busy || !draft.name.trim()}
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
            <div className="text-ink-100/90">
              <span className="font-medium">{item.name}</span>
              {item.consumable || item.quantity > 1 ? (
                <span className="ml-1 text-[10px] uppercase tracking-wide text-ink-100/50">
                  ×{item.quantity}
                </span>
              ) : null}
              {item.given_by === "user" ? (
                <span className="ml-1 rounded bg-emerald-500/20 px-1.5 py-0.5 text-[9px] uppercase tracking-wide text-emerald-200">
                  gift
                </span>
              ) : null}
            </div>
            {item.description ? (
              <div className="text-[11px] text-ink-100/50">
                {item.description}
              </div>
            ) : null}
            <div className="mt-1 flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/40">
              <span className="rounded bg-white/5 px-1.5 py-0.5 text-ink-100/60">
                {item.kind}
              </span>
              {item.consumable ? <span>consumable</span> : null}
              {item.kind === "plant" &&
              typeof item.state?.stage === "string" ? (
                <span
                  className={`rounded px-1.5 py-0.5 ${
                    item.state.stage === "mature"
                      ? "bg-amber-500/20 text-amber-200"
                      : "bg-emerald-500/15 text-emerald-200/80"
                  }`}
                >
                  {item.state.stage === "mature"
                    ? "ready to harvest"
                    : String(item.state.stage)}
                </span>
              ) : null}
              {item.kind === "seed" &&
              typeof item.state?.species === "string" ? (
                <span className="rounded bg-emerald-500/15 px-1.5 py-0.5 text-emerald-200/80">
                  {String(item.state.species)} seed
                </span>
              ) : null}
            </div>
          </div>
          <div className="flex shrink-0 flex-col gap-1">
            {item.consumable && item.quantity > 0 ? (
              <button
                type="button"
                onClick={onConsume}
                className="rounded border border-amber-400/30 bg-amber-500/5 px-2 py-0.5 text-[11px] text-amber-200 hover:border-amber-400/60"
              >
                consume
              </button>
            ) : null}
            <button
              type="button"
              onClick={onStartEdit}
              className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-ink-400 hover:text-ink-100"
            >
              edit
            </button>
            <button
              type="button"
              onClick={onDelete}
              className="rounded border border-white/10 px-2 py-0.5 text-[11px] text-ink-100/60 hover:border-rose-400/60 hover:text-rose-200"
            >
              remove
            </button>
          </div>
        </div>
      )}
    </li>
  );
}

interface DiagnosticsProps {
  metrics: MetricsResponse | null;
  liveLastMetrics: import("../types").MetricsSnapshot;
  /** Apply a partial settings patch (mirrors the outer drawer's
   * ``apply`` helper). Used by the debug-logging toggle to PATCH
   * ``logging.ui_log_enabled``. */
  onApplyPatch: (patch: Record<string, unknown>) => Promise<void> | void;
  busy: boolean;
}

function DiagnosticsSection({
  metrics,
  liveLastMetrics,
  onApplyPatch,
  busy,
}: DiagnosticsProps) {
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

      <DebugLoggingBlock onApplyPatch={onApplyPatch} busy={busy} />
    </Section>
  );
}

interface DebugLoggingBlockProps {
  onApplyPatch: (patch: Record<string, unknown>) => Promise<void> | void;
  busy: boolean;
}

/**
 * Debug-logging block inside ``Diagnostics``.
 *
 * The toggle PATCHes ``logging.ui_log_enabled`` so the change persists
 * on the backend; the WS ``logging_settings_changed`` broadcast then
 * flips :func:`debugLog.setEnabled` on every connected tab (this one
 * included, via :file:`useAssistantSocket.ts`). The local "Download"
 * + "Clear" buttons operate on the in-memory ring buffer so they work
 * even when the backend is offline.
 *
 * We poll :func:`debugLog.size` once a second to drive the entry
 * counter without subscribing every keystroke; the cost is one
 * function call per render frame instead of a Zustand subscription
 * that would re-render the whole drawer on every push.
 */
function DebugLoggingBlock({ onApplyPatch, busy }: DebugLoggingBlockProps) {
  const loggingSettings = useAssistantStore((s) => s.loggingSettings);
  const enabled = loggingSettings.ui_log_enabled;

  // Counter ticks at ~1Hz when the toggle is on so the user sees the
  // buffer grow as they reproduce. When off we still refresh once so
  // the displayed count matches whatever the ring had at the moment
  // of disabling.
  const [tick, setTick] = useState(0);
  useEffect(() => {
    if (!enabled) {
      setTick((t) => t + 1);
      return;
    }
    const handle = window.setInterval(() => setTick((t) => t + 1), 1000);
    return () => window.clearInterval(handle);
  }, [enabled]);
  // `tick` is read implicitly via the snapshot below; the variable is
  // referenced here so the linter doesn't warn it's unused.
  void tick;

  const size = debugLog.size();
  const lastFlush = debugLog.lastFlushAt();
  const lastFlushLabel = lastFlush
    ? `${Math.max(0, Math.round((Date.now() - lastFlush) / 1000))}s ago`
    : "—";

  const handleToggle = (next: boolean) => {
    void onApplyPatch({ logging: { ui_log_enabled: next } });
  };

  return (
    <div className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-3">
      <div className="mb-2 flex items-baseline justify-between text-[11px] font-semibold uppercase tracking-wide text-ink-100/60">
        <span>Debug logging</span>
        <span className="text-[10px] font-normal normal-case text-ink-100/40">
          UI → app.log
        </span>
      </div>
      <p className="mb-3 text-[11px] leading-snug text-ink-100/55">
        Captures WS events, avatar channel decisions, and settings
        changes into <code className="font-mono text-ink-100/70">data/app.log</code> with a{" "}
        <code className="font-mono text-ink-100/70">[ui]</code> prefix. Leave off in normal use;
        flip on, reproduce a bug, then share the log file.
      </p>
      <label className="flex cursor-pointer items-center gap-2 text-[12px] text-ink-100/85">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => handleToggle(e.target.checked)}
          disabled={busy}
          className="h-4 w-4 accent-violet-400"
        />
        Enable debug logging
      </label>
      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px]">
        <button
          type="button"
          onClick={() => debugLog.download()}
          disabled={size === 0}
          className="rounded border border-white/10 bg-white/5 px-2 py-1 text-ink-100/80 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Download buffer
        </button>
        <button
          type="button"
          onClick={() => {
            debugLog.clear();
            setTick((t) => t + 1);
          }}
          disabled={size === 0}
          className="rounded border border-white/10 bg-white/5 px-2 py-1 text-ink-100/80 transition hover:bg-white/10 disabled:cursor-not-allowed disabled:opacity-40"
        >
          Clear
        </button>
        <span className="ml-auto text-ink-100/50 tabular-nums">
          {size.toLocaleString()} entries · last flush {lastFlushLabel}
        </span>
      </div>
    </div>
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

// ── Together tab ─────────────────────────────────────────────────────

interface TogetherTabProps {
  summary: TogetherSummary | null;
  moments: SharedMoment[];
  total: number;
  page: number;
  pageSize: number;
  vibeFilter: string | null;
  loading: boolean;
  error: string | null;
  onSetVibeFilter: (vibe: string | null) => void;
  onSetPage: (page: number) => void;
  newOpen: boolean;
  setNewOpen: (open: boolean) => void;
  newDraft: { summary: string; vibe: string; when: string };
  setNewDraft: (
    draft: { summary: string; vibe: string; when: string },
  ) => void;
  onCreate: () => void;
  editingId: number | null;
  setEditingId: (id: number | null) => void;
  editDraft: { summary: string; vibe: string; when: string };
  setEditDraft: (
    draft: { summary: string; vibe: string; when: string },
  ) => void;
  onSaveEdit: () => void;
  onDelete: (moment: SharedMoment) => void;
  onTogglePin: (moment: SharedMoment) => void;
  onRefresh: () => void;
}

export function TogetherTab({
  summary,
  moments,
  total,
  page,
  pageSize,
  vibeFilter,
  loading,
  error,
  onSetVibeFilter,
  onSetPage,
  newOpen,
  setNewOpen,
  newDraft,
  setNewDraft,
  onCreate,
  editingId,
  setEditingId,
  editDraft,
  setEditDraft,
  onSaveEdit,
  onDelete,
  onTogglePin,
  onRefresh,
}: TogetherTabProps) {
  const pageCount = Math.max(1, Math.ceil(total / pageSize));
  return (
    <div className="space-y-4">
      {error ? (
        <div className="rounded-md border border-red-400/30 bg-red-500/10 px-3 py-2 text-[12px] text-red-200">
          {error}
        </div>
      ) : null}

      {/* Header */}
      <Section title="The story so far">
        <div className="flex flex-wrap items-center gap-2 text-[12px] text-ink-100/80">
          {summary ? (
            <>
              <span className="rounded-full border border-pink-400/30 bg-pink-500/10 px-2 py-0.5 text-[11px] uppercase tracking-wide text-pink-200">
                {summary.phase.replace(/_/g, " ")}
              </span>
              <span>·</span>
              <span>
                <b>{summary.days_known}</b> days known
              </span>
              <span>·</span>
              <span>
                <b>{summary.total_turns}</b> turns
              </span>
              <span>·</span>
              <span>
                <b>{summary.total_sessions}</b> sessions
              </span>
            </>
          ) : (
            <span className="text-ink-100/40">{loading ? "Loading…" : "—"}</span>
          )}
          <button
            type="button"
            onClick={onRefresh}
            className="ml-auto rounded-md border border-white/10 px-2 py-1 text-[11px] hover:bg-white/[0.04]"
          >
            Refresh
          </button>
        </div>
      </Section>

      {/* Anniversary card */}
      {summary?.anniversary_today ? (
        <div className="rounded-md border border-amber-400/30 bg-amber-500/10 px-3 py-2 text-[12px] text-amber-100">
          <div className="text-[10px] uppercase tracking-wider text-amber-200/80">
            On your mind today
          </div>
          <div className="mt-1 text-amber-50">
            {summary.anniversary_today.window_label}:{" "}
            {summary.anniversary_today.summary}
          </div>
          <div className="mt-1 text-[10px] uppercase tracking-wide text-amber-200/60">
            vibe · {summary.anniversary_today.vibe}
          </div>
        </div>
      ) : null}

      {/* Axes bars */}
      {summary?.axes ? (
        <Section title="How the relationship feels">
          <div className="space-y-2">
            <AxisBar label="Closeness" value={summary.axes.closeness} />
            <AxisBar label="Humor" value={summary.axes.humor} />
            <AxisBar label="Trust" value={summary.axes.trust} />
            <AxisBar label="Comfort" value={summary.axes.comfort} />
          </div>
        </Section>
      ) : null}

      {/* Milestones */}
      {summary?.milestones?.length ? (
        <Section title="Milestones">
          <ul className="space-y-1 text-[12px]">
            {summary.milestones.map((m) => (
              <li
                key={m.label}
                className={`flex items-center gap-2 rounded-md px-2 py-1 ${
                  m.crossed ? "bg-emerald-500/10 text-emerald-100" : "text-ink-100/55"
                }`}
              >
                <span>{m.crossed ? "✓" : "·"}</span>
                <span>{m.human}</span>
                {m.crossed_at ? (
                  <span className="ml-auto font-mono text-[10px] text-ink-100/40">
                    {new Date(m.crossed_at).toLocaleDateString()}
                  </span>
                ) : null}
              </li>
            ))}
          </ul>
        </Section>
      ) : null}

      {/* Moments timeline */}
      <Section title={`Shared moments (${total})`}>
        <div className="flex flex-wrap items-center gap-2 text-[11px] text-ink-100/60">
          <label className="flex items-center gap-1">
            Filter
            <select
              value={vibeFilter ?? ""}
              onChange={(e) =>
                onSetVibeFilter(e.target.value ? e.target.value : null)
              }
              className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
            >
              <option value="">all vibes</option>
              {SHARED_MOMENT_VIBES.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
          </label>
          <button
            type="button"
            onClick={() => setNewOpen(!newOpen)}
            className="ml-auto rounded-md border border-white/10 px-2 py-1 hover:bg-white/[0.04]"
          >
            {newOpen ? "Cancel" : "+ Add manually"}
          </button>
        </div>

        {newOpen ? (
          <div className="rounded-md border border-white/10 bg-white/[0.03] p-3">
            <div className="space-y-2">
              <label className="block text-[11px] text-ink-100/55">
                Summary
                <textarea
                  value={newDraft.summary}
                  onChange={(e) =>
                    setNewDraft({ ...newDraft, summary: e.target.value })
                  }
                  rows={2}
                  className="mt-1 w-full rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[12px]"
                />
              </label>
              <div className="grid grid-cols-2 gap-2">
                <label className="block text-[11px] text-ink-100/55">
                  Vibe
                  <select
                    value={newDraft.vibe}
                    onChange={(e) =>
                      setNewDraft({ ...newDraft, vibe: e.target.value })
                    }
                    className="mt-1 w-full rounded-md border border-white/10 bg-black/30 px-2 py-1"
                  >
                    {SHARED_MOMENT_VIBES.map((v) => (
                      <option key={v} value={v}>
                        {v}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="block text-[11px] text-ink-100/55">
                  When (ISO, optional)
                  <input
                    value={newDraft.when}
                    onChange={(e) =>
                      setNewDraft({ ...newDraft, when: e.target.value })
                    }
                    placeholder="2025-04-15T14:00:00Z"
                    className="mt-1 w-full rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
                  />
                </label>
              </div>
              <button
                type="button"
                onClick={onCreate}
                disabled={newDraft.summary.trim().length < 4}
                className="rounded-md bg-pink-500/30 px-3 py-1 text-[12px] hover:bg-pink-500/40 disabled:cursor-not-allowed disabled:opacity-40"
              >
                Save moment
              </button>
            </div>
          </div>
        ) : null}

        {loading && moments.length === 0 ? (
          <div className="text-[11px] text-ink-100/40">Loading…</div>
        ) : moments.length === 0 ? (
          <div className="text-[11px] text-ink-100/40">No moments yet.</div>
        ) : (
          <ul className="space-y-2">
            {moments.map((moment) => (
              <MomentCard
                key={moment.id}
                moment={moment}
                editing={editingId === moment.id}
                draft={editDraft}
                setDraft={setEditDraft}
                onStartEdit={() => {
                  setEditingId(moment.id);
                  setEditDraft({
                    summary: moment.summary,
                    vibe: String(moment.vibe),
                    when: moment.when,
                  });
                }}
                onCancelEdit={() => setEditingId(null)}
                onSaveEdit={onSaveEdit}
                onDelete={() => onDelete(moment)}
                onTogglePin={() => onTogglePin(moment)}
              />
            ))}
          </ul>
        )}

        {pageCount > 1 ? (
          <div className="flex items-center justify-between text-[11px] text-ink-100/55">
            <button
              type="button"
              disabled={page <= 0}
              onClick={() => onSetPage(Math.max(0, page - 1))}
              className="rounded-md border border-white/10 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
            >
              ← Prev
            </button>
            <span>
              page {page + 1} / {pageCount}
            </span>
            <button
              type="button"
              disabled={page >= pageCount - 1}
              onClick={() => onSetPage(Math.min(pageCount - 1, page + 1))}
              className="rounded-md border border-white/10 px-2 py-1 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Next →
            </button>
          </div>
        ) : null}
      </Section>
    </div>
  );
}

interface MomentCardProps {
  moment: SharedMoment;
  editing: boolean;
  draft: { summary: string; vibe: string; when: string };
  setDraft: (
    draft: { summary: string; vibe: string; when: string },
  ) => void;
  onStartEdit: () => void;
  onCancelEdit: () => void;
  onSaveEdit: () => void;
  onDelete: () => void;
  onTogglePin: () => void;
}

function MomentCard({
  moment,
  editing,
  draft,
  setDraft,
  onStartEdit,
  onCancelEdit,
  onSaveEdit,
  onDelete,
  onTogglePin,
}: MomentCardProps) {
  const date = (() => {
    try {
      return new Date(moment.when).toLocaleDateString();
    } catch {
      return moment.when;
    }
  })();
  return (
    <li className="rounded-md border border-white/5 bg-white/[0.02] px-3 py-2 text-[12px]">
      <div className="flex flex-wrap items-center gap-2 text-[10px] uppercase tracking-wide text-ink-100/55">
        <span className="rounded-full bg-white/[0.04] px-2 py-0.5 font-mono">
          {date}
        </span>
        <span className="rounded-full border border-white/10 px-2 py-0.5">
          {moment.vibe}
        </span>
        <span className="text-ink-100/40">via {moment.source}</span>
        {moment.pinned ? (
          <span
            className="ml-1 text-amber-200"
            title="Pinned — never decays"
          >
            ★
          </span>
        ) : null}
        <div className="ml-auto flex gap-1">
          {editing ? (
            <>
              <button
                type="button"
                onClick={onSaveEdit}
                className="rounded-md bg-pink-500/30 px-2 py-0.5 text-[10px] hover:bg-pink-500/40"
              >
                Save
              </button>
              <button
                type="button"
                onClick={onCancelEdit}
                className="rounded-md border border-white/10 px-2 py-0.5 text-[10px] hover:bg-white/[0.04]"
              >
                Cancel
              </button>
            </>
          ) : (
            <>
              <button
                type="button"
                onClick={onStartEdit}
                className="rounded-md border border-white/10 px-2 py-0.5 text-[10px] hover:bg-white/[0.04]"
              >
                Edit
              </button>
              <button
                type="button"
                onClick={onTogglePin}
                className="rounded-md border border-white/10 px-2 py-0.5 text-[10px] hover:bg-white/[0.04]"
              >
                {moment.pinned ? "Unpin" : "Pin"}
              </button>
              <button
                type="button"
                onClick={onDelete}
                className="rounded-md border border-red-400/30 px-2 py-0.5 text-[10px] text-red-200 hover:bg-red-500/10"
              >
                Delete
              </button>
            </>
          )}
        </div>
      </div>
      {editing ? (
        <div className="mt-2 space-y-2">
          <textarea
            value={draft.summary}
            onChange={(e) => setDraft({ ...draft, summary: e.target.value })}
            rows={2}
            className="w-full rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[12px]"
          />
          <div className="grid grid-cols-2 gap-2">
            <select
              value={draft.vibe}
              onChange={(e) => setDraft({ ...draft, vibe: e.target.value })}
              className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
            >
              {SHARED_MOMENT_VIBES.map((v) => (
                <option key={v} value={v}>
                  {v}
                </option>
              ))}
            </select>
            <input
              value={draft.when}
              onChange={(e) => setDraft({ ...draft, when: e.target.value })}
              placeholder="ISO datetime"
              className="rounded-md border border-white/10 bg-black/30 px-2 py-1 text-[11px]"
            />
          </div>
        </div>
      ) : (
        <div className="mt-1 text-ink-100/85">{moment.summary}</div>
      )}
    </li>
  );
}

function AxisBar({
  label,
  value,
}: {
  label: string;
  value: number;
}) {
  // Map [-1, 1] to [0, 100]% for the bar. Centre line at 50%.
  const clamped = Math.max(-1, Math.min(1, Number(value) || 0));
  const isPositive = clamped >= 0;
  const halfWidth = Math.abs(clamped) * 50;
  const color = isPositive ? "bg-emerald-400/70" : "bg-rose-400/70";
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between text-[11px]">
        <span className="text-ink-100/65">{label}</span>
        <span className="font-mono text-ink-100/55">
          {clamped >= 0 ? "+" : ""}
          {clamped.toFixed(2)}
        </span>
      </div>
      <div className="relative h-2 overflow-hidden rounded-full bg-white/[0.05]">
        {/* centre line */}
        <div className="absolute left-1/2 top-0 h-full w-px bg-white/15" />
        {/* value bar */}
        <div
          className={`absolute top-0 h-full ${color}`}
          style={{
            width: `${halfWidth}%`,
            left: isPositive ? "50%" : `${50 - halfWidth}%`,
          }}
        />
      </div>
    </div>
  );
}
