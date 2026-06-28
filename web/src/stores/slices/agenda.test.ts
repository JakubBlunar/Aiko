import { beforeEach, describe, expect, it } from "vitest";

import { useAssistantStore } from "../../store";
import type { AgendaItem } from "../../types";

/**
 * I3: agenda store contract.
 *
 *   * ``setAgendaView`` replaces the list + enabled flag from a REST load.
 *   * ``applyAgendaUpdated`` upserts by id (replace in place, else
 *     prepend) so the single ``agenda_updated`` WS event keeps the panel
 *     live across inline tags / grooming worker / REST edits without a
 *     refetch — including a status flip (the row stays, status changes).
 */

function item(id: number, goal: string, status: AgendaItem["status"] = "open"): AgendaItem {
  return {
    id,
    goal,
    status,
    importance: 0.5,
    created_at: "2026-06-28T00:00:00Z",
    due_at: null,
    last_groomed_at: null,
  };
}

beforeEach(() => {
  useAssistantStore.getState().setAgendaView({ items: [], enabled: true });
});

describe("agenda store", () => {
  it("setAgendaView replaces items + enabled", () => {
    useAssistantStore
      .getState()
      .setAgendaView({ items: [item(1, "learn rust")], enabled: false });
    const view = useAssistantStore.getState().agendaView;
    expect(view.items).toHaveLength(1);
    expect(view.enabled).toBe(false);
  });

  it("applyAgendaUpdated prepends a new row", () => {
    useAssistantStore.getState().setAgendaView({ items: [item(1, "a")], enabled: true });
    useAssistantStore.getState().applyAgendaUpdated(item(2, "b"));
    const ids = useAssistantStore.getState().agendaView.items.map((a) => a.id);
    expect(ids).toEqual([2, 1]);
  });

  it("applyAgendaUpdated replaces in place on a status flip", () => {
    useAssistantStore.getState().setAgendaView({ items: [item(1, "a"), item(2, "b")], enabled: true });
    useAssistantStore.getState().applyAgendaUpdated(item(1, "a", "done"));
    const view = useAssistantStore.getState().agendaView;
    expect(view.items).toHaveLength(2);
    const row = view.items.find((a) => a.id === 1);
    expect(row?.status).toBe("done");
  });

  it("applyAgendaUpdated never duplicates an existing id", () => {
    useAssistantStore.getState().setAgendaView({ items: [item(5, "x")], enabled: true });
    useAssistantStore.getState().applyAgendaUpdated(item(5, "x", "dropped"));
    useAssistantStore.getState().applyAgendaUpdated(item(5, "x", "open"));
    expect(useAssistantStore.getState().agendaView.items).toHaveLength(1);
  });
});
