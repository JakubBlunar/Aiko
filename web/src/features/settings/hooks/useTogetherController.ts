import { useCallback, useEffect, useState } from "react";
import { api } from "@/api";
import { useTogetherStore } from "@/stores/useTogetherStore";
import type { SharedMoment } from "@/types";

interface MomentDraft {
  summary: string;
  vibe: string;
  when: string;
}

const EMPTY_DRAFT: MomentDraft = { summary: "", vibe: "general", when: "" };

/**
 * Owns all "Together" tab state + REST handlers for the SettingsDrawer:
 * the shared-moments store wiring, the create / edit drafts, and the
 * open-on-tab refresh effect. Extracted (phase 4c) so the drawer shell
 * stays a thin tab dispatcher.
 */
export function useTogetherController(open: boolean, activeTab: string) {
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
  const [momentDraft, setMomentDraft] = useState<MomentDraft>(EMPTY_DRAFT);
  const [newMomentOpen, setNewMomentOpen] = useState(false);
  const [newMomentDraft, setNewMomentDraft] =
    useState<MomentDraft>(EMPTY_DRAFT);

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
      setNewMomentDraft(EMPTY_DRAFT);
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

  // Refresh the Together tab whenever it opens or the user changes the
  // vibe filter / page. WS patches handle live moments + axes between
  // refetches so we don't need to poll.
  useEffect(() => {
    if (!open || activeTab !== "together") return;
    void refreshTogether();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, activeTab, togetherView.page, togetherView.vibeFilter]);

  return {
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
  };
}
