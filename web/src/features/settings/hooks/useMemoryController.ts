import { useCallback, useEffect, useMemo, useState } from "react";
import { api } from "@/api";
import { useMemoryStore } from "@/stores/useMemoryStore";
import type { Memory, MemoryOrder, MemoryTier } from "@/types";

const MEMORY_PAGE_SIZE = 50;

interface MemoryDraft {
  content: string;
  kind: string;
  salience: number;
}

/**
 * Owns all "Memory" tab state + REST handlers for the SettingsDrawer:
 * the paginated list store wiring, the edit / create drafts, the derived
 * page-count / range labels, and the open-on-tab refresh effect.
 * Extracted (phase 4c).
 */
export function useMemoryController(open: boolean, activeTab: string) {
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
  const [memoryDraft, setMemoryDraft] = useState<MemoryDraft>({
    content: "",
    kind: "fact",
    salience: 0.5,
  });
  const [memoryNewOpen, setMemoryNewOpen] = useState(false);
  const [memoryNewDraft, setMemoryNewDraft] = useState<MemoryDraft>({
    content: "",
    kind: "fact",
    salience: 0.6,
  });

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

  return {
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
  };
}
